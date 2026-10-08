"""Web layer for the Stockroom panel: REST views, static assets, sidebar.

Same architecture as the WHA-State-Scanner panel: a zero-build custom
element served from this integration's ``static/`` directory, loaded by the
HA frontend as a panel_custom module and talking to the REST views below
via ``hass.fetchWithAuth`` (auth rides the user's HA session).

The views are deliberately thin: item mutations stay on the existing
domain services (the panel calls those through ``hass.callService``), so
the REST surface only covers what services cannot do — one-shot aggregate
reads (with per-level filter-life snapshots), slot-group persistence,
history queries and the panel-side photo-scan entry.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
from pathlib import Path
from typing import Any

from aiohttp import ClientError, ClientResponseError, web
from homeassistant.components import frontend
from homeassistant.components.http import (
    HomeAssistantView,
    StaticPathConfig,
    require_admin,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_loaded_integration

from .bambu import parse_tray_entity
from .const import (
    API_BASE,
    BAMBU_DOMAIN,
    CATEGORY_FILAMENT,
    CONF_BAMBU_TRAY_MAP,
    DEFAULT_WARN_THRESHOLD,
    DOMAIN,
    EXTRA_COLOR,
    EXTRA_MATERIAL,
    PANEL_ELEMENT,
    PANEL_SIDEBAR_ICON,
    PANEL_SIDEBAR_TITLE,
    PANEL_URL_PATH,
    SOURCE_TYPE_COUNT,
    URL_BASE,
)
from .filter_engine import get_filter_engine
from .storage import (
    StockroomStore,
    filter_history_records,
    filter_slot_groups,
    query_filter_history,
    schedule_store_flush,
)

_LOGGER = logging.getLogger(__name__)

WEB_DATA_KEY = f"{DOMAIN}_web_registered"

MAX_GROUPS_PER_WAREHOUSE = 32
MAX_LEVELS_PER_GROUP = 8
SOURCE_TYPES = ("duration", SOURCE_TYPE_COUNT)


class _WebError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


async def _parse_json(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, TypeError):
        raise _WebError(400, "请求体不是合法 JSON") from None
    if not isinstance(body, dict):
        raise _WebError(400, "请求体必须是 JSON 对象")
    return body


def _get_domain_data(hass: HomeAssistant) -> dict[str, Any]:
    return hass.data.get(DOMAIN) or {}


def _get_store(hass: HomeAssistant) -> StockroomStore:
    store = _get_domain_data(hass).get("store")
    if store is None:
        raise _WebError(503, "集成未加载")
    return store


def _get_engines(hass: HomeAssistant) -> dict[str, Any]:
    return _get_domain_data(hass).get("entries") or {}


def _warehouse_payload(engine: Any, scan_view: dict[str, Any]) -> dict[str, Any]:
    """Snapshot one warehouse engine for the overview response."""
    return {
        "entry_id": engine.entry_id,
        "name": engine.warehouse_name,
        "items": engine.items,
        "summary": engine.summary_counts(),
        "scan": scan_view,
    }


def _scan_view(engine: Any) -> dict[str, Any]:
    """Scan-tab info for one warehouse: enabled/pending/webhook URL."""
    scan = engine.scan
    pending = sorted(
        _get_store_scan_pending(engine, scan),
        key=lambda sid_entry: sid_entry[1].get("created_at", ""),
    )
    return {
        "enabled": scan is not None,
        "webhook_url": scan.webhook_url if scan is not None else None,
        "pending": [entry for _sid, entry in pending],
    }


def _get_store_scan_pending(engine: Any, scan: Any) -> list[tuple[str, dict[str, Any]]]:
    """This warehouse's pending suggestions as (sid, entry) pairs."""
    if scan is None:
        return []
    bucket = scan.queue()
    return list(bucket.items())


_NO_RFID_UUID = "00000000000000000000000000000000"
_UNUSABLE_SPOOL_STATES = ("", "?", "empty", "unknown", "unavailable")


def _normalize_color(value: Any) -> str | None:
    """#RRGGBB (upper) from a well-formed #RRGGBB[A] string, else None."""
    text = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", text):
        return text[:7].upper()
    return None


# Basic color vocabulary for human-readable spool names: hex codes never
# belong in item names ("Bambu PLA Basic 白色", not "… #FFFFFF").
_COLOR_NAMES: list[tuple[str, tuple[int, int, int]]] = [
    ("白色", (255, 255, 255)),
    ("黑色", (0, 0, 0)),
    ("红色", (226, 44, 44)),
    ("橙色", (255, 128, 0)),
    ("黄色", (255, 221, 0)),
    ("绿色", (0, 176, 80)),
    ("青色", (0, 188, 212)),
    ("浅蓝", (173, 216, 230)),
    ("蓝色", (44, 84, 220)),
    ("紫色", (139, 0, 255)),
    ("淡紫", (179, 157, 219)),
    ("粉色", (255, 128, 192)),
    ("棕色", (139, 69, 19)),
    ("灰色", (128, 128, 128)),
    ("金色", (212, 175, 55)),
    ("银色", (192, 192, 192)),
]
_COLOR_NAME_MAX_DIST = 110  # per-channel Euclidean threshold


def _color_name(hex_color: str | None) -> str | None:
    """Closest basic Chinese color name within tolerance, else None."""
    normalized = _normalize_color(hex_color)
    if normalized is None:
        return None
    rgb = tuple(int(normalized[i : i + 2], 16) for i in (1, 3, 5))
    best_name, best_dist = None, None
    for name, base in _COLOR_NAMES:
        dist = sum((a - b) ** 2 for a, b in zip(rgb, base)) ** 0.5
        if best_dist is None or dist < best_dist:
            best_name, best_dist = name, dist
    if best_dist is None or best_dist > _COLOR_NAME_MAX_DIST:
        return None
    return best_name


def _spool_color_is_trustworthy(uuid: str, spool_color: str) -> bool:
    """False for RFID-less third-party spools: ha-bambulab reports a flat
    #FFFFFF color for them, so "mismatch" against a real item color would be
    a false positive — and syncing would overwrite the true color."""
    normalized = _normalize_color(spool_color)
    if normalized is None:
        return False
    return not (normalized == "#FFFFFF" and str(uuid or "").strip().upper() == _NO_RFID_UUID)


def _spool_params_differ(
    item: dict[str, Any], spool_type: str, spool_color: str, color_trusted: bool
) -> bool:
    """True when syncing would change the bound item.

    Add-backfill semantics (SR-F-066): a present, trusted spool value that is
    *missing or different* on the item side counts as stale, so items bound
    without material/color get the sync button the bind note promises. A
    value the spool does not carry never erases the item's own.
    """
    extra = item.get("extra") or {}
    material = str(extra.get(EXTRA_MATERIAL) or "").strip().upper()
    spool_material = spool_type.strip().upper()
    if spool_material and material != spool_material:
        return True
    if color_trusted:
        item_color = _normalize_color(extra.get(EXTRA_COLOR))
        if item_color != _normalize_color(spool_color):
            return True
    return False


def _bambu_view(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Every Bambu tray spool with its binding state, for the panel block.

    Discovery walks the bambu_lab config entries' registry sensors through
    parse_tray_entity (external spool excluded); every warehouse's
    CONF_BAMBU_TRAY_MAP is overlaid so the panel sees ALL spool ↔ item
    bindings (a spool may be mapped in several warehouses — none is hidden,
    SR-F-055) and whether each bound item's material/color still matches.
    """
    engines = _get_engines(hass)
    tray_maps: dict[str, dict[str, str]] = {
        entry_id: dict(engine.entry.options.get(CONF_BAMBU_TRAY_MAP) or {})
        for entry_id, engine in engines.items()
    }
    registry = er.async_get(hass)
    trays: list[dict[str, Any]] = []
    seen: set[str] = set()
    for config_entry in hass.config_entries.async_entries(BAMBU_DOMAIN):
        for reg_entity in er.async_entries_for_config_entry(
            registry, config_entry.entry_id
        ):
            if reg_entity.domain != "sensor":
                continue
            entity_id = reg_entity.entity_id
            parsed = parse_tray_entity(entity_id)
            if parsed is None or entity_id in seen:
                continue
            seen.add(entity_id)
            prefix, tray_key = parsed
            state = hass.states.get(entity_id)
            attributes = state.attributes if state is not None else {}
            name = str(state.state) if state is not None else ""
            spool_type = str(attributes.get("type") or "")
            spool_color = str(attributes.get("color") or "")
            uuid = str(attributes.get("tray_uuid") or "")
            color_trusted = _spool_color_is_trustworthy(uuid, spool_color)
            # tray_key shape: "ams_<n>_tray_<m>" (parse_tray_entity contract)
            ams_part, _, tray_part = tray_key.partition("_tray_")
            ams_no = int(ams_part.removeprefix("ams_"))
            tray_no = int(tray_part)
            owners: list[dict[str, Any]] = []
            for entry_id, tray_map in tray_maps.items():
                item_id = tray_map.get(entity_id)
                if item_id is None:
                    continue
                engine = engines.get(entry_id)
                item = engine.items.get(item_id) if engine is not None else None
                owners.append(
                    {
                        "entry_id": entry_id,
                        "item_id": item_id,
                        "item_name": item.get("name") if item is not None else None,
                        "needs_update": (
                            item is not None
                            and _spool_params_differ(
                                item, spool_type, spool_color, color_trusted
                            )
                        ),
                    }
                )
            trays.append(
                {
                    "entity_id": entity_id,
                    # Structured numbers; the panel localizes the label.
                    "ams_no": int(ams_no),
                    "tray_no": int(tray_no),
                    "printer": prefix,
                    "name": name,
                    "type": spool_type,
                    "color": spool_color,
                    "color_name": _color_name(spool_color) if color_trusted else None,
                    "color_trusted": color_trusted,
                    "owners": owners,
                }
            )
    # Stable spool order: printer → AMS → tray number (registry iteration
    # order is not sorted and the panel shows rows as-is).
    trays.sort(key=lambda t: (t["printer"], t["ams_no"], t["tray_no"]))
    return trays


class BambuSyncView(HomeAssistantView):
    """POST /api/stockroom/panel/bambu_sync — bind a tray spool to an item.

    ``action=bind`` maps the spool to an existing item; ``action=create``
    additionally creates a filament item prefilled from the spool's live
    parameters (name/type/color). Both rewrite CONF_BAMBU_TRAY_MAP in the
    warehouse's options via async_update_entry, which triggers the regular
    options reload — the deduction engine rebuilds with the new mapping.
    """

    url = API_BASE + "/panel/bambu_sync"
    name = "api:stockroom:panel_bambu_sync"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            body = await _parse_json(request)
            warehouse = str(body.get("entry_id") or "")
            engine = _get_engines(hass).get(warehouse)
            if engine is None:
                raise _WebError(404, f"仓库不存在：{warehouse or '（未指定）'}")
            tray_entity = str(body.get("tray_entity") or "").strip()
            if parse_tray_entity(tray_entity) is None:
                raise _WebError(400, f"不是有效的 AMS 料盘实体：{tray_entity or '（未指定）'}")
            state = hass.states.get(tray_entity)
            if state is None:
                raise _WebError(400, f"料盘实体不存在：{tray_entity}")
            action = str(body.get("action") or "").strip()
            item_id = str(body.get("item_id") or "").strip() or None
            spool_name = str(state.state).strip()
            spool_type = str(state.attributes.get("type") or "").strip()
            spool_color = str(state.attributes.get("color") or "").strip()
            if spool_name.lower() in _UNUSABLE_SPOOL_STATES:
                # "unavailable" included: a disconnected printer must not
                # become a garbage item (SR-F-054).
                raise _WebError(400, "该料盘当前为空、未识别或不可用，无法绑定")
            if action == "create":
                # The panel prefills and lets the user edit the name; an
                # explicit name always wins over the auto-derived one
                # (SR-F-053). RFID-less spools report a flat white — don't
                # bake it into the name/extra (SR-F-056). Names carry the
                # Chinese color word, never a hex code.
                uuid = str(state.attributes.get("tray_uuid") or "")
                color_trusted = _spool_color_is_trustworthy(uuid, spool_color)
                normalized = _normalize_color(spool_color) if color_trusted else None
                color_word = _color_name(spool_color) if color_trusted else None
                requested_name = str(body.get("name") or "").strip()
                name = requested_name or f"{spool_name} {color_word or ''}".strip()
                extra = {
                    key: value
                    for key, value in (
                        (EXTRA_MATERIAL, spool_type or None),
                        (EXTRA_COLOR, normalized),
                    )
                    if value
                }
                try:
                    item = engine.add_item(
                        name=name, category=CATEGORY_FILAMENT, extra=extra
                    )
                except HomeAssistantError as err:
                    # add_item raises StockroomItemError for bad categories
                    # etc. — answer 400 instead of a bare 500 (SR-F-059).
                    raise _WebError(400, str(err)) from None
                item_id = item["id"]
            elif action == "bind":
                if item_id is None:
                    raise _WebError(400, "绑定已有条目必须提供 item_id")
            else:
                raise _WebError(400, f"未知动作：{action}")
            if item_id not in engine.items:
                raise _WebError(400, f"条目不存在：{item_id}")
            entry = engine.entry
            new_map = dict(entry.options.get(CONF_BAMBU_TRAY_MAP) or {})
            new_map[tray_entity] = item_id
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_BAMBU_TRAY_MAP: new_map}
            )
        except _WebError as err:
            return self.json({"ok": False, "error": err.message}, status_code=err.status)
        _LOGGER.info(
            "[%s] 料盘 %s 已绑定条目 %s（映射写入选项，集成将自动重载）",
            engine.warehouse_name, tray_entity, item_id,
        )
        return self.json({"ok": True, "item_id": item_id})


class OverviewView(HomeAssistantView):
    """GET /api/stockroom/panel/overview — everything the panel renders."""

    url = API_BASE + "/panel/overview"
    name = "api:stockroom:panel_overview"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            store = _get_store(hass)
        except _WebError as err:
            return self.json({"ok": False, "error": err.message}, status_code=err.status)
        engines = _get_engines(hass)
        life_engine = get_filter_engine(hass)
        warehouses = [
            _warehouse_payload(engine, _scan_view(engine))
            for engine in engines.values()
        ]
        # Slot groups come back with per-level life snapshots inlined, so the
        # panel renders without a second lookup (manual groups carry life=null).
        slots: dict[str, list[dict[str, Any]]] = {}
        for entry_id, groups in filter_slot_groups(store).items():
            views: list[dict[str, Any]] = []
            for group in groups:
                view = dict(group)
                view["life"] = {
                    str(slot["level"]): life_engine.level_view(group, int(slot["level"]))
                    for slot in group.get("levels", [])
                } if life_engine else {}
                views.append(view)
            slots[entry_id] = views
        history = filter_history_records(store)
        return self.json(
            {
                "ok": True,
                "warehouses": warehouses,
                "filter_slots": slots,
                "bambu_trays": _bambu_view(hass),
                "history_count": len(history),
            }
        )


class ScanView(HomeAssistantView):
    """POST /api/stockroom/panel/scan — panel-side photo recognition.

    Receives {entry_id, image_base64}, runs the same vision pipeline as the
    webhook (extract validation → recognize → queue → companion notify) and
    returns the queued suggestions for in-panel confirmation.
    """

    url = API_BASE + "/panel/scan"
    name = "api:stockroom:panel_scan"
    requires_auth = True

    @require_admin
    async def post(self, request: web.Request) -> web.Response:
        from .scan import validate_image_bytes  # local: avoid an import cycle

        hass = request.app["hass"]
        try:
            body = await _parse_json(request)
            warehouse = str(body.get("entry_id") or "")
            engine = _get_engines(hass).get(warehouse)
            if engine is None:
                raise _WebError(404, f"仓库不存在：{warehouse or '（未指定）'}")
            scan = engine.scan
            if scan is None:
                raise _WebError(
                    409, "拍照扫描未启用（请在集成选项里开启并配置识别引擎）"
                )
            credential_error = scan._missing_credential_error()
            if credential_error:
                # Engine-aware (SR-F-034 semantics kept, SR-F-070: OCR-only
                # setups must not be gated on the vision API key).
                raise _WebError(409, credential_error)
            raw = body.get("image_base64")
            if not raw:
                raise _WebError(400, "缺少 image_base64 字段")
            try:
                image = base64.b64decode(str(raw), validate=False)
            except (binascii.Error, ValueError) as err:
                raise _WebError(400, "image_base64 不是合法 base64") from err
            try:
                validate_image_bytes(image)
            except ValueError as err:
                raise _WebError(400, str(err)) from err
        except _WebError as err:
            return self.json({"ok": False, "error": err.message}, status_code=err.status)

        try:
            suggestions = await scan._recognize(image)
        except ValueError as err:
            _LOGGER.warning("[%s] 识别结果无法解析：%s", engine.warehouse_name, err)
            return self.json({"ok": False, "error": str(err)})
        except (TimeoutError, asyncio.TimeoutError):
            _LOGGER.warning("[%s] 识别请求超时", engine.warehouse_name)
            return self.json({"ok": False, "error": "识别超时"})
        except ClientResponseError as err:
            return self.json(
                {"ok": False, "error": f"识别服务返回 {err.status}：请检查 API key、配额或模型名"}
            )
        except (ClientError, OSError) as err:
            _LOGGER.warning("[%s] 识别服务连接失败：%s", engine.warehouse_name, err)
            return self.json({"ok": False, "error": "识别服务连接失败"})
        except Exception:
            _LOGGER.exception("[%s] 识别失败", engine.warehouse_name)
            return self.json({"ok": False, "error": "识别失败，见日志"})

        queued = scan.queue_suggestions(suggestions)
        _LOGGER.info(
            "[%s] 面板扫描识别到 %d/%d 个条目建议（队列 %d 项）",
            engine.warehouse_name, len(queued), len(suggestions), len(scan.queue()),
        )
        if queued:
            hass.async_create_task(scan._async_notify(queued))
        return self.json({"ok": True, "suggestions": queued})


class SlotsView(HomeAssistantView):
    """PUT /api/stockroom/panel/slots — save one warehouse's slot groups."""

    url = API_BASE + "/panel/slots"
    name = "api:stockroom:panel_slots"
    requires_auth = True

    @require_admin
    async def put(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            store = _get_store(hass)
            body = await _parse_json(request)
            warehouse = str(body.get("entry_id") or "")
            engine = _get_engines(hass).get(warehouse)
            if engine is None:
                raise _WebError(404, f"仓库不存在：{warehouse or '（未指定）'}")
            groups = _validate_groups(hass, engine, body.get("groups"))
            table = filter_slot_groups(store)
            old_ids = {g.get("group_id") for g in table.get(warehouse, [])}
            new_ids = {g["group_id"] for g in groups}
            table[warehouse] = groups
            life_engine = get_filter_engine(hass)
            if life_engine is not None:
                life_engine.sync_runtimes(groups, old_ids - new_ids)
                life_engine.reload_config()
            store.mark_dirty(warehouse)
            schedule_store_flush(hass, store)
        except _WebError as err:
            return self.json({"ok": False, "error": err.message}, status_code=err.status)
        _LOGGER.info(
            "[%s] 滤芯槽位配置已保存（%d 组）", engine.warehouse_name, len(groups)
        )
        return self.json({"ok": True})


class HistoryView(HomeAssistantView):
    """GET /api/stockroom/panel/history?warehouse=&group_id=&limit="""

    url = API_BASE + "/panel/history"
    name = "api:stockroom:panel_history"
    requires_auth = True

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        try:
            store = _get_store(hass)
        except _WebError as err:
            return self.json({"ok": False, "error": err.message}, status_code=err.status)
        warehouse = request.query.get("warehouse") or None
        group_id = request.query.get("group_id") or None
        try:
            limit = int(request.query.get("limit", "200"))
        except ValueError:
            return self.json(
                {"ok": False, "error": "limit 必须是整数"}, status_code=400
            )
        limit = max(1, min(limit, 500))
        return self.json(
            {
                "ok": True,
                "records": query_filter_history(
                    store, warehouse=warehouse, group_id=group_id, limit=limit
                ),
            }
        )


def _validate_groups(hass: HomeAssistant, engine: Any, raw: Any) -> list[dict[str, Any]]:
    """Normalize and validate a slot-group payload; raises _WebError(400).

    Group shape (FLT-model port): optional shared source entity (must exist
    in hass.states at save time; absent state object = unknown entity), its
    integration type (duration/count) and target state, plus 1..8 levels
    each carrying an optional item binding and dual-track ratings. Level
    sets are free-form; manual groups simply have no source entity.
    """
    if not isinstance(raw, list):
        raise _WebError(400, "groups 必须是数组")
    if len(raw) > MAX_GROUPS_PER_WAREHOUSE:
        raise _WebError(400, f"槽位组数量超过上限 {MAX_GROUPS_PER_WAREHOUSE}")

    items = engine.items

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_group in raw:
        if not isinstance(raw_group, dict):
            raise _WebError(400, "槽位组必须是对象")
        group_id = str(raw_group.get("group_id") or "").strip()
        if not group_id:
            raise _WebError(400, "槽位组缺少 group_id")
        if group_id in seen_ids:
            raise _WebError(400, f"槽位组 ID 重复：{group_id}")
        seen_ids.add(group_id)

        name = str(raw_group.get("name") or "").strip() or "滤芯组"

        # --- source entity (组级共用水源) --------------------------------
        source_entity = str(raw_group.get("source_entity") or "").strip() or None
        source_type: str | None = None
        target_state: str | None = None
        raw_debounce = raw_group.get("debounce")
        try:
            debounce = 10 if raw_debounce in (None, "") else int(raw_debounce)
        except (TypeError, ValueError):
            raise _WebError(400, f"去抖秒数不是整数：{raw_debounce!r}") from None
        if source_entity is not None:
            if hass.states.get(source_entity) is None:
                raise _WebError(400, f"水源实体不存在：{source_entity}")
            source_type = str(raw_group.get("source_type") or "").strip()
            if source_type not in SOURCE_TYPES:
                raise _WebError(
                    400, f"积分类型必须是 duration 或 count：{source_type!r}"
                )
            target_state = str(raw_group.get("target_state") or "").strip()
            if not target_state:
                raise _WebError(400, "绑定水源实体时必须填写目标状态")
            if not 1 <= debounce <= 300:
                raise _WebError(400, f"去抖秒数超出范围：{debounce}")

        # --- levels ------------------------------------------------------
        raw_levels = raw_group.get("levels")
        if not isinstance(raw_levels, list) or not raw_levels:
            raise _WebError(400, f"槽位组「{name}」至少需要一个槽位")
        if len(raw_levels) > MAX_LEVELS_PER_GROUP:
            raise _WebError(400, f"槽位组「{name}」级数超过上限 {MAX_LEVELS_PER_GROUP}")
        levels: list[dict[str, Any]] = []
        seen_levels: set[int] = set()

        def _positive(
            slot: dict[str, Any], group_name: str, level: int, key: str
        ) -> float | None:
            value = slot.get(key)
            if value in (None, ""):
                return None
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise _WebError(
                    400, f"槽位组「{group_name}」{level} 级的 {key} 不是数字"
                ) from None
            if value <= 0:
                raise _WebError(
                    400, f"槽位组「{group_name}」{level} 级的 {key} 必须为正数"
                )
            return value

        for position, raw_slot in enumerate(raw_levels):
            if not isinstance(raw_slot, dict):
                raise _WebError(400, f"槽位组「{name}」的槽位必须是对象")
            try:
                level = int(raw_slot.get("level"))
            except (TypeError, ValueError):
                raise _WebError(400, f"槽位组「{name}」存在非法级号") from None
            if not 1 <= level <= MAX_LEVELS_PER_GROUP:
                raise _WebError(400, f"槽位组「{name}」级号超出范围：{level}")
            if level in seen_levels:
                raise _WebError(400, f"槽位组「{name}」级号重复：{level}")
            seen_levels.add(level)

            item_id = raw_slot.get("item_id") or None
            if item_id is not None:
                item_id = str(item_id)
                if item_id not in items:
                    raise _WebError(400, f"条目不存在：{item_id}")

            rated_time_days = _positive(raw_slot, name, level, "rated_time_days")
            rated_usage = _positive(raw_slot, name, level, "rated_usage")
            warn_threshold = raw_slot.get("warn_threshold")
            if warn_threshold in (None, ""):
                warn_threshold = DEFAULT_WARN_THRESHOLD
            else:
                try:
                    warn_threshold = float(warn_threshold)
                except (TypeError, ValueError):
                    raise _WebError(
                        400, f"槽位组「{name}」{level} 级的预警阈值不是数字"
                    ) from None
                if not 0 <= warn_threshold <= 100:
                    raise _WebError(
                        400,
                        f"槽位组「{name}」{level} 级的预警阈值须在 0–100：{warn_threshold}",
                    )
            cascade_factor = raw_slot.get("cascade_factor")
            if cascade_factor in (None, ""):
                cascade_factor = None
            else:
                try:
                    cascade_factor = float(cascade_factor)
                except (TypeError, ValueError):
                    raise _WebError(
                        400, f"槽位组「{name}」{level} 级的级联系数不是数字"
                    ) from None
                if not 1.0 <= cascade_factor <= 5.0:
                    raise _WebError(
                        400,
                        f"槽位组「{name}」{level} 级的级联系数须在 1.0–5.0：{cascade_factor}",
                    )

            level_data: dict[str, Any] = {
                "level": level,
                "item_id": item_id,
                "rated_time_days": rated_time_days,
                "rated_usage": rated_usage,
                "warn_threshold": warn_threshold,
            }
            if position > 0 and cascade_factor is not None:
                level_data["cascade_factor"] = cascade_factor
            levels.append(level_data)
        levels.sort(key=lambda slot: slot["level"])

        group_data: dict[str, Any] = {
            "group_id": group_id,
            "name": name,
            "source_entity": source_entity,
            "source_type": source_type,
            "target_state": target_state,
            "debounce": debounce,
            "levels": levels,
        }
        normalized.append(group_data)
    return normalized


async def async_setup_web(hass: HomeAssistant) -> None:
    """Register static assets, panel and API views (once per HA run)."""
    flags: dict = hass.data.setdefault(WEB_DATA_KEY, {})

    if not flags.get("views"):
        hass.http.register_view(OverviewView())
        hass.http.register_view(SlotsView())
        hass.http.register_view(BambuSyncView())
        hass.http.register_view(ScanView())
        hass.http.register_view(HistoryView())
        flags["views"] = True

    if not flags.get("static"):
        static_dir = Path(__file__).parent / "static"
        await hass.http.async_register_static_paths(
            [StaticPathConfig(URL_BASE, str(static_dir), cache_headers=False)]
        )
        flags["static"] = True

    panels = hass.data.get(frontend.DATA_PANELS) or {}
    if PANEL_URL_PATH not in panels:
        version = "dev"
        try:
            version = async_get_loaded_integration(hass, DOMAIN).version or version
        except Exception:  # noqa: BLE001 — version is only a cache buster
            _LOGGER.debug("Could not resolve integration version for panel URL")
        frontend.async_register_built_in_panel(
            hass,
            component_name="custom",
            frontend_url_path=PANEL_URL_PATH,
            sidebar_title=PANEL_SIDEBAR_TITLE,
            sidebar_icon=PANEL_SIDEBAR_ICON,
            config={
                "_panel_custom": {
                    "name": PANEL_ELEMENT,
                    "embed_iframe": False,
                    "trust_external": False,
                    "module_url": f"{URL_BASE}/panel.js?ver={version}",
                }
            },
            require_admin=True,
        )


@callback
def async_unload_web(hass: HomeAssistant) -> None:
    """Remove the sidebar panel (static paths/views survive entry reloads)."""
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
