"""Inventory engine for Stockroom — one instance per config entry.

Pure in-memory mutations over the shared store's item table plus change
broadcasting (dispatcher + bus event). All methods are synchronous and run
in the event loop; persistence is dirty-flag based (batched flush), so no
blocking IO ever happens here.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .bambu import BambuDeductor
from .const import (
    CATEGORIES,
    CATEGORY_OTHER,
    CONF_BAMBU_TRAY_MAP,
    CONF_DEFAULT_LOW_THRESHOLD,
    CONF_SCAN_ENABLED,
    CONF_SUMMARY_BY_CATEGORY,
    CONF_WAREHOUSE_NAME,
    DEFAULT_LOW_THRESHOLD,
    DEFAULT_SUMMARY_BY_CATEGORY,
    DEFAULT_UNITS,
    DEFAULT_WAREHOUSE_NAME,
    EVENT_ITEM_CHANGED,
    EXTRA_COLOR,
    EXTRA_FULL_WEIGHT_G,
    EXTRA_MATERIAL,
    ITEM_LAST_STOCKTAKE,
    ITEM_UPDATED_AT,
    QUANTITY_MAX,
    SIGNAL_ITEMS_UPDATED,
)
from .scan import ScanManager
from .storage import StockroomStore

_LOGGER = logging.getLogger(__name__)


class StockroomItemError(HomeAssistantError):
    """Raised for unknown or ambiguous item references and id conflicts."""


# Quantities are stored rounded to 3 decimals; anything that collapses to a
# sub-micro residue (1000 - 65.28 - 934.72 style float dust) becomes 0.
def _round_quantity(value: float) -> float:
    """Normalize a quantity: reject non-finite values, clamp magnitudes
    beyond QUANTITY_MAX to the ceiling *with a log* (restock sums can get
    there legally; a silent 0 would wipe real stock), round float dust to a
    clean 0 and round to 3 dp."""
    if not math.isfinite(value):
        _LOGGER.warning("数量出现非有限值 %r，已按 0 处理", value)
        return 0.0
    if value > QUANTITY_MAX:
        _LOGGER.warning("数量 %s 超过上限 %g，已钳到上限", value, QUANTITY_MAX)
        return QUANTITY_MAX
    rounded = round(value, 3)
    return 0.0 if abs(rounded) < 1e-6 else rounded


def utcnow_iso() -> str:
    """Return the current UTC time as ISO string."""
    return datetime.now(UTC).isoformat()


# Compact pinyin table for common stockroom vocabulary, so CJK item names
# still get a readable ASCII slug ("PLA-白色" -> "pla-bai-se"). Characters not
# listed are dropped; a name that slugifies to nothing falls back to a hash
# id. Users can always pass item_id explicitly.
_PINYIN = {
    "白": "bai", "黑": "hei", "红": "hong", "橙": "cheng", "黄": "huang",
    "绿": "lv", "青": "qing", "蓝": "lan", "紫": "zi", "粉": "fen",
    "棕": "zong", "灰": "hui", "金": "jin", "银": "yin", "铜": "tong",
    "钢": "gang", "铁": "tie", "铝": "lv", "木": "mu", "塑": "su",
    "透": "tou", "明": "ming", "哑": "ya", "光": "guang", "亮": "liang",
    "半": "ban", "原": "yuan", "色": "se", "卷": "juan", "芯": "xin",
    "螺": "luo", "丝": "si", "杆": "gan", "母": "mu", "帽": "mao",
    "垫": "dian", "片": "pian", "钉": "ding", "销": "xiao", "轴": "zhou",
    "承": "cheng", "带": "dai", "管": "guan", "板": "ban", "棒": "bang",
    "块": "kuai", "盒": "he", "包": "bao", "袋": "dai", "箱": "xiang",
    "支": "zhi", "张": "zhang", "米": "mi", "克": "ke", "升": "sheng",
    "号": "hao", "内": "nei", "外": "wai", "六": "liu", "角": "jiao",
    "沉": "chen", "头": "tou", "平": "ping", "圆": "yuan", "十": "shi",
    "字": "zi", "槽": "cao", "自": "zi", "攻": "gong", "机": "ji",
    "用": "yong", "胶": "jiao", "水": "shui", "洗": "xi", "笔": "bi",
    "刀": "dao", "嘴": "zui", "喷": "pen", "热": "re", "电": "dian",
}

_SYMBOL_MAP = {
    "×": "x", "✕": "x", "Ⅹ": "x", "＊": "-", "－": "-", "—": "-",
    "–": "-", "／": "-", "。": "-", "，": "-", "、": "-", "：": "-",
}

_SEPARATOR_CHARS = set(" -_./\\()（）[]【】·")


def slugify_name(name: str) -> str:
    """ASCII slugify: "M3×8" -> "m3x8", "PLA-白色" -> "pla-bai-se".

    ASCII alphanumerics are kept; separators collapse to single hyphens;
    mapped CJK characters become hyphen-bracketed pinyin syllables. An empty
    result falls back to "item-<md5 prefix>".
    """
    text = str(name).strip().lower()
    for src, dst in _SYMBOL_MAP.items():
        text = text.replace(src, dst)

    out: list[str] = []
    for ch in text:
        if ch.isascii() and ch.isalnum():
            out.append(ch)
        elif ch in _SEPARATOR_CHARS:
            out.append("-")
        elif ch in _PINYIN:
            out.append(f"-{_PINYIN[ch]}-")
        # Anything else (unmapped CJK, emoji, ...) is dropped.

    slug = re.sub(r"-{2,}", "-", "".join(out)).strip("-")
    if not slug:
        slug = "item-" + hashlib.md5(name.encode("utf-8")).hexdigest()[:6]
    return slug


class InventoryEngine:
    """Runtime inventory of one warehouse config entry."""

    def __init__(self, hass: HomeAssistant, entry: Any, store: StockroomStore) -> None:
        """Initialize the engine and bind its item-table view."""
        self.hass = hass
        self.entry = entry
        self.entry_id = entry.entry_id
        self.store = store
        self.items: dict[str, dict[str, Any]] = store.get_items(entry.entry_id)
        self._unsubs: list[Any] = []
        self._bambu: BambuDeductor | None = None
        self._scan: ScanManager | None = None

    # ------------------------------------------------------------------
    # Config helpers (options override data)
    # ------------------------------------------------------------------

    def _opt(self, key: str, default: Any = None) -> Any:
        return self.entry.options.get(key, self.entry.data.get(key, default))

    @property
    def warehouse_name(self) -> str:
        """Display name of the warehouse (device name)."""
        return str(self._opt(CONF_WAREHOUSE_NAME, DEFAULT_WAREHOUSE_NAME))

    @property
    def default_low_threshold(self) -> float:
        """Threshold used when add_item does not specify one."""
        return float(self._opt(CONF_DEFAULT_LOW_THRESHOLD, DEFAULT_LOW_THRESHOLD))

    @property
    def summary_by_category(self) -> bool:
        """Whether the summary sensors break counts down by category."""
        return bool(self._opt(CONF_SUMMARY_BY_CATEGORY, DEFAULT_SUMMARY_BY_CATEGORY))

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Start listeners: Bambu AMS deduction and the photo-scan webhook."""
        try:
            tray_map = self._opt(CONF_BAMBU_TRAY_MAP) or {}
            if tray_map:
                self._bambu = BambuDeductor(self.hass, self.entry, self, self.store, tray_map)
                self._unsubs.extend(self._bambu.async_setup())
            if self._opt(CONF_SCAN_ENABLED):
                self._scan = ScanManager(self.hass, self.entry, self, self.store)
                # The scan manager unsubscribes its own listeners in its teardown
                # (its webhook needs an unregister+re-register lifecycle, so it
                # must not share the plain unsub list with the deduction engine).
                self._scan.async_setup()
                _LOGGER.info(
                    "[%s] 拍照扫描已启用，webhook：%s",
                    self.warehouse_name, self._scan.webhook_url,
                )
        except Exception:
            # Roll back partial setup: when this raises the engine never
            # reaches hass.data[DOMAIN]["entries"], so the unload-path
            # teardown never runs and already-registered listeners would
            # leak until the next HA restart.
            self.async_teardown()
            raise

    @callback
    def async_teardown(self) -> None:
        """Stop all listeners."""
        if self._scan is not None:
            self._scan.async_teardown()
            self._scan = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._bambu = None

    @property
    def scan(self) -> ScanManager | None:
        """The scan manager when photo scanning is enabled."""
        return self._scan

    @property
    def scan_webhook_url(self) -> str | None:
        """Webhook URL for the phone shortcut, or None when scan is off."""
        return self._scan.webhook_url if self._scan is not None else None

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @staticmethod
    def is_low_stock(item: dict[str, Any]) -> bool:
        """Low stock = quantity at or below the item threshold."""
        try:
            threshold = float(item.get("low_threshold", 0.0))
        except (TypeError, ValueError):
            threshold = 0.0
        try:
            quantity = float(item.get("quantity", 0.0))
        except (TypeError, ValueError):
            quantity = 0.0
        return quantity <= threshold

    def summary_counts(self) -> dict[str, Any]:
        """Totals for the summary sensors (optionally split by category)."""
        total = len(self.items)
        low = sum(1 for item in self.items.values() if self.is_low_stock(item))
        counts: dict[str, Any] = {"total": total, "low_stock": low}
        if self.summary_by_category:
            by_cat: dict[str, int] = {cat: 0 for cat in CATEGORIES}
            low_by_cat: dict[str, int] = {cat: 0 for cat in CATEGORIES}
            for item in self.items.values():
                cat = item.get("category")
                if cat not in by_cat:
                    by_cat[cat] = 0
                    low_by_cat[cat] = 0
                by_cat[cat] += 1
                if self.is_low_stock(item):
                    low_by_cat[cat] += 1
            counts["by_category"] = by_cat
            counts["low_by_category"] = low_by_cat
        return counts

    # ------------------------------------------------------------------
    # Item resolution
    # ------------------------------------------------------------------

    def resolve_item_id(self, ref: str) -> str:
        """Resolve a service reference: id exact, then unique name match."""
        ref = str(ref).strip()
        if not ref:
            # An empty/blank needle would fuzzy-match every name ("", needle
            # in anything == True) and silently hit a single-item warehouse.
            raise StockroomItemError("条目引用不能为空（请提供条目 id 或名称）")
        if ref in self.items:
            return ref
        exact = [iid for iid, item in self.items.items() if item.get("name") == ref]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise StockroomItemError(
                f"条目名称「{ref}」匹配到 {len(exact)} 个条目（{', '.join(sorted(exact))}），请改用 item id"
            )
        needle = ref.lower()
        fuzzy = [
            iid
            for iid, item in self.items.items()
            if needle in str(item.get("name", "")).lower()
        ]
        if len(fuzzy) == 1:
            return fuzzy[0]
        if len(fuzzy) > 1:
            raise StockroomItemError(
                f"条目名称「{ref}」模糊匹配到 {len(fuzzy)} 个条目（{', '.join(sorted(fuzzy))}），请改用 item id"
            )
        raise StockroomItemError(f"未找到条目：{ref}")

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def add_item(
        self,
        name: str,
        category: str = CATEGORY_OTHER,
        quantity: float = 0.0,
        unit: str | None = None,
        low_threshold: float | None = None,
        location: str | None = None,
        extra: dict[str, Any] | None = None,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        """Create an item; raises StockroomItemError on id conflict."""
        if category not in CATEGORIES:
            raise StockroomItemError(f"未知分类：{category}（可选：{', '.join(CATEGORIES)}）")
        try:
            quantity = max(0.0, float(quantity))
        except (TypeError, ValueError):
            quantity = 0.0
        quantity = _round_quantity(quantity)

        base = slugify_name(item_id) if item_id else slugify_name(name)
        iid = base
        counter = 2
        while iid in self.items:
            if item_id:
                raise StockroomItemError(f"条目 id 已存在：{iid}")
            iid = f"{base}-{counter}"
            counter += 1

        threshold = float(
            low_threshold if low_threshold is not None else self.default_low_threshold
        )
        if threshold > QUANTITY_MAX:
            # Belt and braces: the service schemas cap this at QUANTITY_MAX,
            # but the internal scan-confirm path bypasses them.
            _LOGGER.warning("低库存线 %s 超过上限 %g，已钳到上限", threshold, QUANTITY_MAX)
            threshold = QUANTITY_MAX

        item: dict[str, Any] = {
            "id": iid,
            "name": str(name).strip(),
            "category": category,
            "quantity": quantity,
            "unit": (str(unit).strip() if unit else "") or DEFAULT_UNITS.get(category, "个"),
            "low_threshold": threshold,
            "extra": dict(extra) if extra else {},
            ITEM_LAST_STOCKTAKE: None,
        }
        if location:
            item["location"] = str(location).strip()
        self.items[iid] = item
        self._after_mutation(item, "add")
        _LOGGER.info("[%s] 新增条目 %s（%s %s%s）",
                     self.warehouse_name, iid, item["quantity"], item["unit"],
                     f"，位于 {item['location']}" if item.get("location") else "")
        return item

    def consume(self, ref: str, amount: float, note: str | None = None) -> dict[str, Any]:
        """Deduct from an item, clamping at 0 with a warning log."""
        item = self.items[self.resolve_item_id(ref)]
        amount = float(amount)
        new_quantity = float(item["quantity"]) - amount
        if new_quantity < 0:
            _LOGGER.warning(
                "[%s] 条目 %s 扣减 %.3f %s 超出库存 %.3f，已钳到 0（%s）",
                self.warehouse_name, item["id"], amount, item["unit"],
                item["quantity"], note or "无备注",
            )
            new_quantity = 0.0
        item["quantity"] = _round_quantity(new_quantity)
        self._after_mutation(item, "consume", note=note)
        return item

    def deduct_by_id(self, item_id: str, amount: float, note: str | None = None) -> dict[str, Any] | None:
        """Deduct by exact id (Bambu auto-deduction path); missing id is a no-op."""
        item = self.items.get(item_id)
        if item is None:
            _LOGGER.warning(
                "[%s] AMS 扣料目标条目不存在：%s（映射可能已悬空）",
                self.warehouse_name, item_id,
            )
            return None
        amount = float(amount)
        new_quantity = float(item["quantity"]) - amount
        if new_quantity < 0:
            _LOGGER.warning(
                "[%s] AMS 扣料使条目 %s 超出库存（%.3f → 钳 0）",
                self.warehouse_name, item_id, item["quantity"],
            )
            new_quantity = 0.0
        item["quantity"] = _round_quantity(new_quantity)
        self._after_mutation(item, "consume", note=note)
        return item

    def restock(self, ref: str, amount: float, note: str | None = None) -> dict[str, Any]:
        """Add stock to an item."""
        item = self.items[self.resolve_item_id(ref)]
        item["quantity"] = _round_quantity(float(item["quantity"]) + float(amount))
        self._after_mutation(item, "restock", note=note)
        return item

    def stocktake(self, ref: str, actual: float, note: str | None = None) -> dict[str, Any]:
        """Calibrate an item to the physically counted value."""
        item = self.items[self.resolve_item_id(ref)]
        item["quantity"] = _round_quantity(max(0.0, float(actual)))
        item[ITEM_LAST_STOCKTAKE] = utcnow_iso()
        self._after_mutation(item, "stocktake", note=note)
        return item

    def set_threshold(self, ref: str, threshold: float) -> dict[str, Any]:
        """Update the low-stock threshold of an item."""
        item = self.items[self.resolve_item_id(ref)]
        item["low_threshold"] = max(0.0, float(threshold))
        self._after_mutation(item, "set_threshold")
        return item

    def update_item(self, ref: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Patch an item's metadata; only provided keys change.

        Name/category/unit/location/low_threshold are top-level fields;
        material/color/full_weight_g land in ``extra`` (None clears them).
        Category changes do not retroactively rewrite the unit. Name cannot
        be cleared (blank raises); a blank unit resets to the category
        default (units are mandatory — a silent "" would break displays).
        """
        item = self.items[self.resolve_item_id(ref)]
        if "name" in updates:
            new_name = str(updates["name"]).strip()
            if not new_name:
                raise StockroomItemError(
                    "条目名称不能为空（名称无法清除，请传新名称或不传该字段）"
                )
            item["name"] = new_name
        if "category" in updates and updates["category"] is not None:
            if updates["category"] not in CATEGORIES:
                raise StockroomItemError(
                    f"未知分类：{updates['category']}（可选：{', '.join(CATEGORIES)}）"
                )
            item["category"] = updates["category"]
        if "unit" in updates and updates["unit"] is not None:
            item["unit"] = (
                str(updates["unit"]).strip() or DEFAULT_UNITS.get(item["category"], "个")
            )
        if "location" in updates:
            if updates["location"] is None or not str(updates["location"]).strip():
                item.pop("location", None)
            else:
                item["location"] = str(updates["location"]).strip()
        if "low_threshold" in updates and updates["low_threshold"] is not None:
            item["low_threshold"] = max(0.0, float(updates["low_threshold"]))
        extra = item.setdefault("extra", {})
        for key in (EXTRA_MATERIAL, EXTRA_COLOR):
            if key in updates:
                if updates[key] is None or not str(updates[key]).strip():
                    extra.pop(key, None)
                else:
                    extra[key] = str(updates[key]).strip()
        if EXTRA_FULL_WEIGHT_G in updates:
            value = updates[EXTRA_FULL_WEIGHT_G]
            if value is None or float(value) <= 0:
                extra.pop(EXTRA_FULL_WEIGHT_G, None)
            else:
                extra[EXTRA_FULL_WEIGHT_G] = float(value)
        self._after_mutation(item, "update")
        return item

    def remove_item(self, ref: str) -> dict[str, Any]:
        """Delete an item; entities disappear via the dispatcher diff."""
        iid = self.resolve_item_id(ref)
        item = self.items.pop(iid)
        self._after_mutation(item, "remove", note=None)
        _LOGGER.info("[%s] 移除条目 %s", self.warehouse_name, iid)
        return item

    # ------------------------------------------------------------------
    # Change broadcasting
    # ------------------------------------------------------------------

    @callback
    def _after_mutation(self, item: dict[str, Any], action: str, note: str | None = None) -> None:
        """Stamp update time, flag persistence, dispatch entities, fire event.

        All callers run on the event loop (services, webhook, state-change
        callbacks), so a direct dispatch is both correct and keeps the
        blocking-service semantic "returned ⇒ state already written".
        """
        item[ITEM_UPDATED_AT] = utcnow_iso()
        self.store.mark_dirty(self.entry_id)
        async_dispatcher_send(self.hass, SIGNAL_ITEMS_UPDATED.format(self.entry_id))
        self._fire_event(item["id"], action, item["quantity"], note=note)

    @callback
    def _fire_event(
        self, item_id: str, action: str, new_quantity: float | None, note: str | None
    ) -> None:
        """Fire stockroom_item_changed for user automations (no notifications)."""
        data: dict[str, Any] = {
            "entry_id": self.entry_id,
            "item_id": item_id,
            "action": action,
            "new_quantity": new_quantity,
        }
        if note:
            data["note"] = note
        self.hass.bus.async_fire(EVENT_ITEM_CHANGED, data)
