"""Web layer for the Stockroom panel: REST views, static assets, sidebar.

Same architecture as the WHA-State-Scanner panel: a zero-build custom
element served from this integration's ``static/`` directory, loaded by the
HA frontend as a panel_custom module and talking to the REST views below
via ``hass.fetchWithAuth`` (auth rides the user's HA session).

The views are deliberately thin: item mutations stay on the existing
domain services (the panel calls those through ``hass.callService``), so
the REST surface only covers what services cannot do — one-shot aggregate
reads, slot-group persistence and history queries.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.components import frontend
from homeassistant.components.http import (
    HomeAssistantView,
    StaticPathConfig,
    require_admin,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.loader import async_get_loaded_integration

from .const import (
    API_BASE,
    DOMAIN,
    FLT_DOMAIN,
    FLT_ENTRY_TYPE_DEVICE,
    PANEL_ELEMENT,
    PANEL_SIDEBAR_ICON,
    PANEL_SIDEBAR_TITLE,
    PANEL_URL_PATH,
    URL_BASE,
)
from .flt import discover_flt_devices
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
MAX_LEVELS_PER_GROUP = 16


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


def _warehouse_payload(engine: Any) -> dict[str, Any]:
    """Snapshot one warehouse engine for the overview response."""
    return {
        "entry_id": engine.entry_id,
        "name": engine.warehouse_name,
        "items": engine.items,
        "summary": engine.summary_counts(),
    }


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
        warehouses = [_warehouse_payload(engine) for engine in engines.values()]
        history = filter_history_records(store)
        return self.json(
            {
                "ok": True,
                "warehouses": warehouses,
                "filter_slots": filter_slot_groups(store),
                "flt_devices": discover_flt_devices(hass),
                "history_count": len(history),
            }
        )


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
            filter_slot_groups(store)[warehouse] = groups
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

    Validation is structural: group_id uniqueness, level integers in range,
    item references that exist in *this* warehouse, and flt_entry_id values
    that point at a real FLT device-type entry (or None for manual groups).
    Level sets are NOT forced to match FLT's current filters — a binding may
    outlive a temporary FLT unload, and manual levels are free-form.
    """
    if not isinstance(raw, list):
        raise _WebError(400, "groups 必须是数组")
    if len(raw) > MAX_GROUPS_PER_WAREHOUSE:
        raise _WebError(400, f"槽位组数量超过上限 {MAX_GROUPS_PER_WAREHOUSE}")

    flt_entries = {
        entry.entry_id
        for entry in hass.config_entries.async_entries(FLT_DOMAIN)
        if entry.data.get("entry_type") == FLT_ENTRY_TYPE_DEVICE
    }
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
        flt_entry_id = raw_group.get("flt_entry_id") or None
        if flt_entry_id is not None:
            flt_entry_id = str(flt_entry_id)
            if flt_entry_id not in flt_entries:
                raise _WebError(400, f"滤芯设备不存在或不是设备型条目：{flt_entry_id}")

        raw_levels = raw_group.get("levels")
        if not isinstance(raw_levels, list) or not raw_levels:
            raise _WebError(400, f"槽位组「{name}」至少需要一个槽位")
        if len(raw_levels) > MAX_LEVELS_PER_GROUP:
            raise _WebError(400, f"槽位组「{name}」级数超过上限 {MAX_LEVELS_PER_GROUP}")
        levels: list[dict[str, Any]] = []
        seen_levels: set[int] = set()
        for raw_slot in raw_levels:
            if not isinstance(raw_slot, dict):
                raise _WebError(400, f"槽位组「{name}」的槽位必须是对象")
            try:
                level = int(raw_slot.get("level"))
            except (TypeError, ValueError):
                raise _WebError(400, f"槽位组「{name}」存在非法级号") from None
            if not 1 <= level <= 64:
                raise _WebError(400, f"槽位组「{name}」级号超出范围：{level}")
            if level in seen_levels:
                raise _WebError(400, f"槽位组「{name}」级号重复：{level}")
            seen_levels.add(level)
            item_id = raw_slot.get("item_id") or None
            if item_id is not None:
                item_id = str(item_id)
                if item_id not in items:
                    raise _WebError(400, f"条目不存在：{item_id}")
            levels.append({"level": level, "item_id": item_id})
        levels.sort(key=lambda slot: slot["level"])
        normalized.append(
            {
                "group_id": group_id,
                "name": name,
                "flt_entry_id": flt_entry_id,
                "levels": levels,
            }
        )
    return normalized


async def async_setup_web(hass: HomeAssistant) -> None:
    """Register static assets, panel and API views (once per HA run)."""
    flags: dict = hass.data.setdefault(WEB_DATA_KEY, {})

    if not flags.get("views"):
        hass.http.register_view(OverviewView())
        hass.http.register_view(SlotsView())
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
