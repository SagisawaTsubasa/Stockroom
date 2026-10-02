"""Binary sensor platform for Stockroom — one low-stock flag per item."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, KEY_ITEM_LOW_STOCK, KIND_LOW_STOCK, SIGNAL_ITEMS_UPDATED
from .entity import (
    StockroomEntity,
    async_remove_stockroom_entities,
    diff_item_ids,
)
from .inventory import InventoryEngine


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one low-stock binary sensor per item, kept in sync dynamically."""
    engine: InventoryEngine = hass.data[DOMAIN]["entries"][entry.entry_id]
    known: dict[str, ItemLowStockBinarySensor] = {}

    @callback
    def _async_check_items() -> None:
        """Diff current item ids against live entities, then add/remove."""
        add_ids, remove_ids = diff_item_ids(set(engine.items), known)
        additions = []
        for item_id in add_ids:
            entity = ItemLowStockBinarySensor(engine, item_id)
            known[item_id] = entity  # register before add: aborted adds must not retry
            additions.append(entity)
        removing = [known.pop(item_id) for item_id in remove_ids]
        if additions:
            async_add_entities(additions)
        if removing:
            async_remove_stockroom_entities(hass, removing)

    _async_check_items()
    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_ITEMS_UPDATED.format(entry.entry_id),
            _async_check_items,
        )
    )


class ItemLowStockBinarySensor(StockroomEntity, BinarySensorEntity):
    """Problem-class flag: on while quantity is at or below the threshold."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, engine: InventoryEngine, item_id: str) -> None:
        """Initialize from the engine and current item name."""
        super().__init__(engine, item_id, KIND_LOW_STOCK)
        self._attr_translation_key = KEY_ITEM_LOW_STOCK
        item = engine.items.get(item_id) or {}
        self._attr_translation_placeholders = {"item": str(item.get("name", item_id))}

    @property
    def is_on(self) -> bool | None:
        """Return True while the item is at or under its low-stock line."""
        item = self.item
        if item is None:
            return None
        return self._engine.is_low_stock(item)
