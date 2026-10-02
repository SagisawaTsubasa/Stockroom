"""Stockroom — FDM 3D printing consumable & hardware inventory integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    ALL_SERVICES,
    CATEGORIES,
    CATEGORY_OTHER,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    EXTRA_COLOR,
    EXTRA_FULL_WEIGHT_G,
    EXTRA_MATERIAL,
    FLUSH_INTERVAL,
    SERVICE_ADD_ITEM,
    SERVICE_CONSUME,
    SERVICE_REMOVE_ITEM,
    SERVICE_RESTOCK,
    SERVICE_SET_THRESHOLD,
    SERVICE_STOCKTAKE,
)
from .inventory import InventoryEngine
from .storage import StockroomStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

# consume/restock must move real stock: 0 passes cv.positive_float (Range
# min=0) but is a meaningless mutation, so clamp it at the schema level.
_QUANTITY = vol.All(cv.positive_float, vol.Range(min=0.001))

ADD_ITEM_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Required("name"): cv.string,
        vol.Optional("category", default=CATEGORY_OTHER): vol.In(CATEGORIES),
        vol.Optional("quantity", default=0.0): cv.positive_float,
        vol.Optional("unit"): cv.string,
        vol.Optional("low_threshold"): cv.positive_float,
        vol.Optional("location"): cv.string,
        vol.Optional("item_id"): cv.string,
        vol.Optional("material"): cv.string,
        vol.Optional("color"): cv.string,
        vol.Optional("full_weight_g"): cv.positive_float,
    }
)
REMOVE_ITEM_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Required("item"): cv.string,
    }
)
CONSUME_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Required("item"): cv.string,
        vol.Required("quantity"): _QUANTITY,
        vol.Optional("note"): cv.string,
    }
)
RESTOCK_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Required("item"): cv.string,
        vol.Required("quantity"): _QUANTITY,
        vol.Optional("note"): cv.string,
    }
)
STOCKTAKE_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Required("item"): cv.string,
        vol.Required("actual"): cv.positive_float,
        vol.Optional("note"): cv.string,
    }
)
SET_THRESHOLD_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Required("item"): cv.string,
        vol.Required("threshold"): cv.positive_float,
    }
)


async def _async_get_store(hass: HomeAssistant) -> StockroomStore:
    """Return the shared store, loading and scheduling flushes once.

    The store instance is placed into hass.data *synchronously* so that
    parallel entry setups share a single store; every caller then awaits the
    same load task (Filter-Life-Tracker precedent). Timer/shutdown handles
    are (re-)registered whenever they are missing, so a released-but-kept
    store instance is revived correctly on the next setup.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    store = domain_data.get("store")
    if store is None:
        store = StockroomStore(hass)
        domain_data["store"] = store
        domain_data["store_task"] = hass.async_create_task(store.async_load())
    if domain_data.get("flush_unsub") is None:
        # Batched flush every 10 min + forced flush on HA stop; handles are
        # cancelled when the last entry unloads (the instance is kept).
        domain_data["flush_unsub"] = async_track_time_interval(
            hass, store.async_flush, FLUSH_INTERVAL
        )
    if domain_data.get("stop_unsub") is None:
        domain_data["stop_unsub"] = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, store.async_flush
        )
    task = domain_data.get("store_task")
    if task is not None:
        try:
            await task
        except Exception:
            # Load failed (disk error / future storage schema): drop the
            # placeholder so a retry starts clean, and kill the handles —
            # flushing a never-loaded store would overwrite the on-disk
            # state with an empty table.
            if domain_data.get("store") is store:
                domain_data.pop("store", None)
            for key in ("flush_unsub", "stop_unsub"):
                unsub = domain_data.pop(key, None)
                if unsub is not None:
                    unsub()
            raise
        finally:
            if domain_data.get("store_task") is task:
                domain_data.pop("store_task", None)
    return store


def _maybe_release_store(hass: HomeAssistant) -> None:
    """Cancel the shared flush timer/listener when the last entry unloads.

    The store instance itself is kept: if the final flush failed, the
    in-memory state stays authoritative for a later reload instead of
    silently regressing to the last successful on-disk write. A later setup
    re-registers the handles (they are registered whenever missing).
    """
    domain_data = hass.data.get(DOMAIN) or {}
    if domain_data.get("entries"):
        return
    for key in ("flush_unsub", "stop_unsub"):
        unsub = domain_data.pop(key, None)
        if unsub is not None:
            unsub()


def _async_get_engine(hass: HomeAssistant, entry_id: str | None) -> InventoryEngine:
    """Resolve which warehouse engine a service call targets."""
    entries = hass.data.get(DOMAIN, {}).get("entries", {})
    if entry_id:
        engine = entries.get(entry_id)
        if not isinstance(engine, InventoryEngine):
            raise HomeAssistantError(f"stockroom: 未找到配置项 {entry_id} 对应的仓库")
        return engine
    engines = [value for value in entries.values() if isinstance(value, InventoryEngine)]
    if len(engines) == 1:
        return engines[0]
    if not engines:
        raise HomeAssistantError("stockroom: 尚未配置任何仓库")
    raise HomeAssistantError(
        "stockroom: 存在多个仓库，请在服务调用中提供 entry_id 指定目标仓库"
    )


def _schedule_flush(hass: HomeAssistant, engine: InventoryEngine) -> None:
    """Persist soon after a mutation (dirty-flag makes extra flushes cheap)."""
    hass.async_create_task(engine.store.async_flush())


def _async_register_services(hass: HomeAssistant) -> None:
    """Register domain services once (guarded, engine resolved per call)."""
    if hass.services.has_service(DOMAIN, SERVICE_ADD_ITEM):
        return

    async def handle_add_item(call: ServiceCall) -> None:
        """Create an item; category extras only apply to filament semantics."""
        engine = _async_get_engine(hass, call.data.get("entry_id"))
        extra = {
            key: call.data[key]
            for key in (EXTRA_MATERIAL, EXTRA_COLOR, EXTRA_FULL_WEIGHT_G)
            if call.data.get(key) not in (None, "")
        }
        engine.add_item(
            name=call.data["name"],
            category=call.data.get("category", CATEGORY_OTHER),
            quantity=call.data.get("quantity", 0.0),
            unit=call.data.get("unit"),
            low_threshold=call.data.get("low_threshold"),
            location=call.data.get("location"),
            extra=extra,
            item_id=call.data.get("item_id"),
        )
        _schedule_flush(hass, engine)

    async def handle_remove_item(call: ServiceCall) -> None:
        """Remove an item (by id or unique name)."""
        engine = _async_get_engine(hass, call.data.get("entry_id"))
        engine.remove_item(call.data["item"])
        _schedule_flush(hass, engine)

    async def handle_consume(call: ServiceCall) -> None:
        """Deduct stock, clamping at zero."""
        engine = _async_get_engine(hass, call.data.get("entry_id"))
        engine.consume(call.data["item"], call.data["quantity"], call.data.get("note"))
        _schedule_flush(hass, engine)

    async def handle_restock(call: ServiceCall) -> None:
        """Add stock back."""
        engine = _async_get_engine(hass, call.data.get("entry_id"))
        engine.restock(call.data["item"], call.data["quantity"], call.data.get("note"))
        _schedule_flush(hass, engine)

    async def handle_stocktake(call: ServiceCall) -> None:
        """Calibrate to the physically counted value and stamp the time."""
        engine = _async_get_engine(hass, call.data.get("entry_id"))
        engine.stocktake(call.data["item"], call.data["actual"], call.data.get("note"))
        _schedule_flush(hass, engine)

    async def handle_set_threshold(call: ServiceCall) -> None:
        """Update the low-stock threshold."""
        engine = _async_get_engine(hass, call.data.get("entry_id"))
        engine.set_threshold(call.data["item"], call.data["threshold"])
        _schedule_flush(hass, engine)

    hass.services.async_register(
        DOMAIN, SERVICE_ADD_ITEM, handle_add_item, schema=ADD_ITEM_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_ITEM, handle_remove_item, schema=REMOVE_ITEM_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CONSUME, handle_consume, schema=CONSUME_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESTOCK, handle_restock, schema=RESTOCK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_STOCKTAKE, handle_stocktake, schema=STOCKTAKE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_THRESHOLD, handle_set_threshold, schema=SET_THRESHOLD_SCHEMA
    )


def _async_remove_services(hass: HomeAssistant) -> None:
    """Drop domain services once the last entry is gone."""
    for service in ALL_SERVICES:
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration domain data."""
    hass.data.setdefault(DOMAIN, {}).setdefault("entries", {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a warehouse from a config entry."""
    store = await _async_get_store(hass)
    engine = InventoryEngine(hass, entry, store)
    await engine.async_setup()
    hass.data.setdefault(DOMAIN, {}).setdefault("entries", {})[entry.entry_id] = engine

    _async_register_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        engine = hass.data[DOMAIN].get("entries", {}).pop(entry.entry_id, None)
        if engine is not None:
            engine.async_teardown()
        store = hass.data[DOMAIN].get("store")
        if store is not None:
            await store.async_flush()
        _maybe_release_store(hass)
        if not hass.data.get(DOMAIN, {}).get("entries"):
            _async_remove_services(hass)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up persisted state when an entry is removed."""
    store = hass.data.get(DOMAIN, {}).get("store")
    if store is None:
        # Store instance not in memory (entry removed without a prior setup):
        # spin up a temporary one so the item table still leaves the file.
        store = StockroomStore(hass)
        await store.async_load()
    store.remove_entry_items(entry.entry_id)
    await store.async_flush()


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry data schema across versions (identity at v1)."""
    if entry.version > CONFIG_ENTRY_VERSION:
        return False
    if entry.version < CONFIG_ENTRY_VERSION:
        hass.config_entries.async_update_entry(
            entry,
            version=CONFIG_ENTRY_VERSION,
            minor_version=CONFIG_ENTRY_MINOR_VERSION,
        )
    return True
