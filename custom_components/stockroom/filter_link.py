"""Filter-Life-Tracker reset-event linking (滤芯槽位自动扣件).

Listens for FLT's ``filter_life_tracker_filter_reset`` bus event and, for
every slot bound to that (flt_entry_id, level), consumes one unit of the
bound stockroom item and appends a change-history record.

FLT is an optional sibling integration: this module holds no import of it
and the listener simply never fires when FLT is absent. The handler is a
plain ``@callback`` (synchronous) on purpose — ``hass.bus.async_fire`` runs
callback listeners inline, so the FLT main-life sensor is read *before* FLT
resets its entities, giving the pre-replacement life percentage for the
history record. Anything this handler does must therefore stay off the
await path (mutations are in-memory; flushing uses ``schedule_store_flush``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

from .const import (
    DOMAIN,
    FLT_ENTRY_TYPE_DEVICE,
    FLT_EVENT_RESET,
    FLT_NOTE_AUTO_DEDUCT,
)
from .flt import read_life_pct
from .storage import (
    StockroomStore,
    append_filter_history,
    filter_slot_groups,
    schedule_store_flush,
)

_LOGGER = logging.getLogger(__name__)


def setup_filter_link(hass: HomeAssistant, store: StockroomStore) -> Callable[[], None]:
    """Subscribe the reset-event handler; return the unsubscribe callable."""
    store_ref = store

    @callback
    def _on_filter_reset(event: Event) -> None:
        _handle_reset(hass, store_ref, event)

    return hass.bus.async_listen(FLT_EVENT_RESET, _on_filter_reset)


@callback
def _handle_reset(hass: HomeAssistant, store: StockroomStore, event: Event) -> None:
    """Match one FLT reset against slot bindings; never raise into the bus."""
    try:
        _handle_reset_inner(hass, store, event)
    except Exception:
        _LOGGER.exception("处理滤芯更换事件失败（事件数据：%s）", dict(event.data or {}))


@callback
def _handle_reset_inner(hass: HomeAssistant, store: StockroomStore, event: Event) -> None:
    data: dict[str, Any] = event.data or {}
    # Total-prefilter entries are virtual aggregations — resetting them must
    # not consume a physical spare.
    if data.get("entry_type") != FLT_ENTRY_TYPE_DEVICE:
        return
    flt_entry_id = str(data.get("entry_id") or "")
    if not flt_entry_id:
        return
    try:
        level = int(data.get("level"))
    except (TypeError, ValueError):
        _LOGGER.warning("滤芯更换事件缺少合法 level 字段：%s", dict(data))
        return

    matched = 0
    for warehouse_entry_id, groups in filter_slot_groups(store).items():
        engine = (hass.data.get(DOMAIN, {}).get("entries") or {}).get(warehouse_entry_id)
        if engine is None:
            continue
        for group in groups:
            if group.get("flt_entry_id") != flt_entry_id:
                continue
            slot = next(
                (s for s in group.get("levels", []) if _slot_level(s) == level),
                None,
            )
            if slot is None:
                continue
            matched += 1
            _deduct_slot(hass, store, warehouse_entry_id, engine, group, slot, level, flt_entry_id)
    if matched:
        schedule_store_flush(hass, store)
    elif _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "滤芯更换事件无槽位命中（flt_entry=%s level=%s）", flt_entry_id, level
        )


@callback
def _deduct_slot(
    hass: HomeAssistant,
    store: StockroomStore,
    warehouse_entry_id: str,
    engine: Any,
    group: dict[str, Any],
    slot: dict[str, Any],
    level: int,
    flt_entry_id: str,
) -> None:
    """Consume the bound item and record history for one matched slot."""
    label = group.get("name") or group.get("group_id") or "?"
    item_id = slot.get("item_id")
    item = engine.items.get(item_id) if item_id else None
    if item is None:
        _LOGGER.warning(
            "[%s] 槽位组「%s」%d 级未绑定条目或条目已不存在，跳过自动扣件",
            getattr(engine, "warehouse_name", warehouse_entry_id),
            label,
            level,
        )
        return
    life_pct = read_life_pct(hass, flt_entry_id, level)
    engine.consume(item_id, 1.0, note=FLT_NOTE_AUTO_DEDUCT)
    append_filter_history(
        store,
        {
            "ts": datetime.now(UTC).isoformat(),
            "warehouse": warehouse_entry_id,
            "warehouse_name": getattr(engine, "warehouse_name", warehouse_entry_id),
            "group_id": group.get("group_id", ""),
            "group_name": group.get("name", ""),
            "level": level,
            "item_id": item_id,
            "item_name": item.get("name", item_id),
            "mode": "flt",
            "life_pct": life_pct,
            "note": FLT_NOTE_AUTO_DEDUCT,
        },
        warehouse_entry_id,
    )
    _LOGGER.info(
        "[%s] 滤芯更换联动：组「%s」%d 级 → 条目 %s 扣减 1（换下时寿命 %s）",
        getattr(engine, "warehouse_name", warehouse_entry_id),
        label,
        level,
        item_id,
        f"{life_pct:.0f}%" if life_pct is not None else "未知",
    )


def _slot_level(slot: dict[str, Any]) -> int | None:
    """Return a slot's level as int, tolerating string round-trips."""
    try:
        return int(slot.get("level"))
    except (TypeError, ValueError):
        return None
