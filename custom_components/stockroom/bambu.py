"""Bambu Lab AMS automatic filament deduction.

Watches ``sensor.<prefix>_print_status`` of every printer whose tray entities
appear in the entry's ``bambu_tray_map`` option. When a print finishes
(state becomes ``finish``), the per-tray weights are read from the attributes
of ``sensor.<prefix>_print_weight`` (keys like ``"AMS 1 Tray 2": 65.28``),
normalized to ``ams_<n>_tray_<m>`` and matched against the mapped tray
entities by their entity-id tail. Each hit deducts its grams from the mapped
item.

Verified against greghesp ha-bambulab on HA 2026.1.3 (P1S):
- ``..._print_status`` enum contains ``finish``; a deduction only happens on
  a transition from a running-ish state (``running``/``pause``/``prepare``/
  ``slicing``/``init``) — replayed transitions (``None→finish`` after an HA
  restart) and reconnect transitions (``offline→finish``) are ignored.
- ``..._print_weight`` attributes carry one grams key per used AMS tray
  (``"AMS 1 Tray 2": 65.28``). AMS HT (``"AMS HT 1"``) and external spool
  keys do not match the tray pattern and are reported as unmapped (V1 does
  not support them).
- ``..._task_name`` / ``..._start_time`` keep the last task after finish and
  drive the idempotency record, stored per ``"{entry_id}|{printer_prefix}"``
  so parallel warehouses never shadow each other. When the task identity is
  unreadable the deduction is skipped with a warning — a missed deduction
  can be repaired with a stocktake, a double deduction silently corrupts
  the ledger.

All exceptions in the deduction path are logged, never raised — a broken
third-party integration must not turn into errors in ours.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    BAMBU_RUN_STATES,
    BAMBU_START_TIME_ENTITY_SUFFIX,
    BAMBU_STATUS_ENTITY_SUFFIX,
    BAMBU_STATUS_FINISH,
    BAMBU_TASK_NAME_ENTITY_SUFFIX,
    BAMBU_WEIGHT_ENTITY_SUFFIX,
)
from .storage import last_deduct_records, schedule_store_flush

_LOGGER = logging.getLogger(__name__)

# print_weight attribute key: "AMS 1 Tray 2" -> ams_1_tray_2
_ATTR_TRAY_RE = re.compile(r"^ams\s*(\d+)\s*tray\s*(\d+)$", re.IGNORECASE)

# Tray entity_id: sensor.p1s_01p00c490500648_ams_1_tray_2. The greedy prefix
# binds to the *last* tray segment, so device names that themselves contain
# "_ams_" ("p1s_ams_kit_01_ams_1_tray_2") still resolve to "p1s_ams_kit_01".
_TRAY_ENTITY_RE = re.compile(r"^(.*)_ams_(\d+)_tray_(\d+)$")


def parse_tray_entity(tray_entity_id: str) -> tuple[str, str] | None:
    """Split a tray entity_id into ``(printer prefix, normalized tray key)``.

    ``sensor.p1s_01p00c490500648_ams_1_tray_2`` ->
    ``("p1s_01p00c490500648", "ams_1_tray_2")``; returns None for entities
    that are not AMS tray sensors (e.g. the external spool).
    """
    object_id = tray_entity_id.split(".")[-1]
    match = _TRAY_ENTITY_RE.match(object_id)
    if not match:
        return None
    return match.group(1), f"ams_{match.group(2)}_tray_{match.group(3)}"


def normalize_tray_attr(key: str) -> str | None:
    """Normalize a print_weight attribute key to ``ams_<n>_tray_<m>``."""
    match = _ATTR_TRAY_RE.match(str(key).strip())
    if not match:
        return None
    return f"ams_{match.group(1)}_tray_{match.group(2)}"


def extract_tray_weights(attributes: dict[str, Any]) -> dict[str, float]:
    """Pull numeric per-AMS-tray grams out of print_weight attributes."""
    weights: dict[str, float] = {}
    for key, value in attributes.items():
        normalized = normalize_tray_attr(key)
        if normalized is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value < 0:
            continue
        weights[normalized] = float(value)
    return weights


def should_deduct(
    record: dict[str, Any],
    task_name: str | None,
    start_time: str | None,
) -> tuple[bool, str]:
    """Decide whether a finish event must deduct; returns ``(deduct, why)``.

    Deduct only when the task identity (task_name AND start_time — both must
    be readable, otherwise "same task" matching silently weakens) differs
    from the recorded deduction. With an unreadable identity we never fall
    back to a time window: the print_weight attributes still hold the
    *previous* task's grams in that state, so deducting would silently
    double-count. A missed deduction can be repaired with a stocktake (and
    is flagged with a warning).
    """
    if not task_name or not start_time:
        return False, "task_identity_unreadable"
    previous_task = str(record.get("task_name") or "")
    previous_start = str(record.get("start_time") or "")
    if previous_task == task_name and previous_start == (start_time or ""):
        return False, "same_task"
    return True, "new_task"


class BambuDeductor:
    """Per-entry AMS deduction listener driven by print_status transitions."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: Any,
        engine: Any,
        store: Any,
        tray_map: dict[str, str],
    ) -> None:
        """Build prefix -> (tray key -> item_id) tables from the option map."""
        self.hass = hass
        self.entry = entry
        self.engine = engine
        self.store = store
        self._unsubs: list[Any] = []
        # printer prefix -> {status entity_id: prefix} for event routing
        self._status_entities: dict[str, str] = {}
        # printer prefix -> {normalized tray key: item_id}
        self._prefix_map: dict[str, dict[str, str]] = {}

        for entity_id, item_id in tray_map.items():
            parsed = parse_tray_entity(str(entity_id))
            if parsed is None:
                _LOGGER.warning(
                    "无法从 %s 识别 AMS 料盘实体（V1 仅支持 <打印机>_ams_<n>_tray_<m>，"
                    "外挂盘与 AMS HT 暂不支持），忽略该映射",
                    entity_id,
                )
                continue
            prefix, tray_key = parsed
            self._prefix_map.setdefault(prefix, {})[tray_key] = str(item_id)
            self._status_entities[f"sensor.{prefix}{BAMBU_STATUS_ENTITY_SUFFIX}"] = prefix

        self._warn_non_gram_items()

    def _warn_non_gram_items(self) -> None:
        """AMS deducts grams; mapped filament tracked in other units misleads."""
        for item_id in set(self._prefix_map_bare_item_ids()):
            item = self.engine.items.get(item_id)
            if item is not None and str(item.get("unit", "")).lower() not in ("g", "克"):
                _LOGGER.warning(
                    "[%s] 条目 %s 的单位是 %s，而 AMS 按克（g）扣料——建议耗材按 g 记账",
                    self.engine.warehouse_name, item_id, item.get("unit"),
                )

    def _prefix_map_bare_item_ids(self) -> list[str]:
        return [
            item_id
            for mapping in self._prefix_map.values()
            for item_id in mapping.values()
        ]

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def async_setup(self) -> list[Any]:
        """Register one print_status listener per mapped printer."""
        for status_entity_id, prefix in self._status_entities.items():
            if self.hass.states.get(status_entity_id) is None:
                _LOGGER.warning(
                    "[%s] 找不到 %s（打印机集成未就绪或实体已改名），"
                    "该实体出现前 AMS 自动扣料不会触发",
                    prefix, status_entity_id,
                )
            _LOGGER.debug("[%s] 监听 %s（AMS 自动扣料）", prefix, status_entity_id)
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, [status_entity_id], self._make_handler(prefix)
                )
            )
        return list(self._unsubs)

    def _make_handler(self, prefix: str):
        """Bind the printer prefix into the state-change callback."""

        @callback
        def _on_status_event(event: Event) -> None:
            self._handle_status_event(prefix, event)

        return _on_status_event

    # ------------------------------------------------------------------
    # Deduction
    # ------------------------------------------------------------------

    @callback
    def _handle_status_event(self, prefix: str, event: Event) -> None:
        """React to print_status; everything is guarded, nothing raises."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        try:
            if new_state is None or new_state.state != BAMBU_STATUS_FINISH:
                return
            if old_state is None or old_state.state not in BAMBU_RUN_STATES:
                # None→finish (HA restart replay) and offline→finish
                # (printer reconnect) are not completions; deducting on
                # them would double-count the already-deducted task.
                _LOGGER.debug(
                    "[%s] finish 前状态为 %s，视为重放/重连，不扣料",
                    prefix, old_state.state if old_state else None,
                )
                return
            self._handle_finish(prefix)
        except Exception:
            _LOGGER.exception(
                "[%s] AMS 自动扣料失败（不影响打印机集成）", prefix
            )

    @callback
    def _handle_finish(self, prefix: str) -> None:
        """Deduct one finished print, guarded by the idempotency record."""
        now = datetime.now(UTC)
        records = last_deduct_records(self.store)
        # Per-entry keys: two warehouses mapping the same printer each get
        # their own idempotency record and both deduct their own items.
        record_key = f"{self.entry.entry_id}|{prefix}"
        record = records.get(record_key) or {}

        task_name = self._entity_state(f"sensor.{prefix}{BAMBU_TASK_NAME_ENTITY_SUFFIX}")
        start_time = self._entity_state(f"sensor.{prefix}{BAMBU_START_TIME_ENTITY_SUFFIX}")
        ok, reason = should_deduct(record, task_name, start_time)
        if not ok:
            if reason == "same_task":
                _LOGGER.debug(
                    "[%s] 打印任务 %s 已扣过料（幂等跳过）", prefix, task_name
                )
            else:
                _LOGGER.warning(
                    "[%s] 打印完成但任务身份不可读（task_name/start_time 均不可用），"
                    "宁漏勿重、不做扣减；如需对账请用 stocktake 校准",
                    prefix,
                )
            return

        weight_state: State | None = self.hass.states.get(
            f"sensor.{prefix}{BAMBU_WEIGHT_ENTITY_SUFFIX}"
        )
        weights: dict[str, float] = {}
        if weight_state is None:
            _LOGGER.warning(
                "[%s] 找不到 sensor.%s%s，无法读取本次用量",
                prefix, prefix, BAMBU_WEIGHT_ENTITY_SUFFIX,
            )
        else:
            weights = extract_tray_weights(weight_state.attributes)
            unrecognized = sorted(
                str(key)
                for key in weight_state.attributes
                if ("ams" in str(key).lower() or "spool" in str(key).lower())
                and normalize_tray_attr(key) is None
            )
            if unrecognized:
                _LOGGER.warning(
                    "[%s] print_weight 分盘键无法映射（AMS HT/外挂盘等，V1 不支持自动扣料）：%s",
                    prefix, ", ".join(unrecognized),
                )
        if not weights:
            _LOGGER.debug(
                "[%s] 无可用的 AMS 分盘克数，本次不扣减", prefix
            )

        mapping = self._prefix_map.get(prefix, {})
        deducted = 0
        for tray_key, grams in sorted(weights.items()):
            item_id = mapping.get(tray_key)
            if item_id is None:
                _LOGGER.debug("[%s] 分盘 %s（%.3f g）未映射条目，跳过", prefix, tray_key, grams)
                continue
            note = f"AMS 自动扣料：{task_name or '未知任务'}"
            if self.engine.deduct_by_id(item_id, grams, note=note) is not None:
                deducted += 1
            else:
                _LOGGER.warning(
                    "[%s] AMS 映射指向的条目 %s 已不存在，"
                    "请到集成选项里更新料盘映射",
                    prefix, item_id,
                )

        records[record_key] = {
            "task_name": task_name or "",
            "start_time": start_time or "",
            "deducted_at": now.isoformat(),
            "trays": weights,
        }
        self.store.mark_dirty(self.entry.entry_id)
        if deducted:
            _LOGGER.info(
                "[%s] 打印完成，已按分盘克数自动扣减 %d 个条目（任务：%s）",
                prefix, deducted, task_name or "未知",
            )
            schedule_store_flush(self.hass, self.store)

    def _entity_state(self, entity_id: str) -> str | None:
        """Return an entity state string, or None for missing/unavailable."""
        state: State | None = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        return state.state
