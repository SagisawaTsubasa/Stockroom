"""Sensor platform for Stockroom.

Per-item quantity sensors are created/removed dynamically by diffing the
engine's item table on every dispatcher broadcast; the two summary sensors
are fixed per entry.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    ITEM_LAST_STOCKTAKE,
    ITEM_LOW_THRESHOLD,
    ITEM_UPDATED_AT,
    KEY_ITEM_QUANTITY,
    KEY_SUMMARY_LOW_STOCK,
    KEY_SUMMARY_TOTAL,
    KIND_QUANTITY,
    SIGNAL_ITEMS_UPDATED,
)
from .entity import (
    StockroomEntity,
    StockroomSummaryEntity,
    async_remove_stockroom_entities,
    diff_item_ids,
)
from .inventory import InventoryEngine


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up summary sensors plus one quantity sensor per item."""
    engine: InventoryEngine = hass.data[DOMAIN]["entries"][entry.entry_id]
    known: dict[str, StockroomItemSensor] = {}

    @callback
    def _async_check_items() -> None:
        """Diff current item ids against live entities, then add/remove."""
        add_ids, remove_ids = diff_item_ids(set(engine.items), known)
        additions = []
        for item_id in add_ids:
            entity = StockroomItemSensor(engine, item_id)
            known[item_id] = entity  # register before add: aborted adds must not retry
            additions.append(entity)
        removing = [known.pop(item_id) for item_id in remove_ids]
        if additions:
            async_add_entities(additions)
        if removing:
            async_remove_stockroom_entities(hass, removing)

    # Initial population, then keep the table in sync via the engine signal.
    _async_check_items()
    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_ITEMS_UPDATED.format(entry.entry_id),
            _async_check_items,
        )
    )
    async_add_entities([SummaryTotalSensor(engine), SummaryLowStockSensor(engine)])


class StockroomItemSensor(StockroomEntity, SensorEntity):
    """Quantity sensor for one inventory item."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:package-variant-closed"

    def __init__(self, engine: InventoryEngine, item_id: str) -> None:
        """Initialize from the engine and current item name."""
        super().__init__(engine, item_id, KIND_QUANTITY)
        self._attr_translation_key = KEY_ITEM_QUANTITY
        item = engine.items.get(item_id) or {}
        self._attr_translation_placeholders = {"item": str(item.get("name", item_id))}

    @property
    def native_value(self) -> float | None:
        """Return the current quantity."""
        item = self.item
        if item is None:
            return None
        try:
            return float(item.get("quantity", 0.0))
        except (TypeError, ValueError):
            return None

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the item's own unit (颗/个/卷/kg/g/...)."""
        item = self.item
        if item is None:
            return None
        return str(item.get("unit", "")) or None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the item model: category/name/location/extra expansion."""
        item = self.item
        if item is None:
            return None
        attributes: dict[str, Any] = {
            "id": item.get("id"),
            "name": item.get("name"),
            "category": item.get("category"),
            "unit": item.get("unit"),
            ITEM_LOW_THRESHOLD: item.get("low_threshold"),
            ITEM_LAST_STOCKTAKE: item.get(ITEM_LAST_STOCKTAKE),
            ITEM_UPDATED_AT: item.get(ITEM_UPDATED_AT),
        }
        if item.get("location"):
            attributes["location"] = item["location"]
        extra = item.get("extra")
        if isinstance(extra, dict):
            attributes.update(extra)
        return attributes


class _SummarySensor(StockroomSummaryEntity, SensorEntity):
    """Common bits of the two summary sensors."""

    _attr_icon = "mdi:counter"

    def __init__(self, engine: InventoryEngine) -> None:
        """Initialize."""
        super().__init__(engine, self._summary_key())

    @staticmethod
    def _summary_key() -> str:
        """Translation key of the concrete summary sensor."""
        raise NotImplementedError

    def _counts(self) -> dict[str, Any]:
        """Summary numbers from the engine."""
        return self._engine.summary_counts()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Category breakdown when the option is enabled."""
        counts = self._counts()
        if "by_category" not in counts:
            return None
        return {
            "by_category": counts["by_category"],
            "low_by_category": counts.get("low_by_category", {}),
        }


class SummaryTotalSensor(_SummarySensor):
    """Total number of items in the warehouse."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    @staticmethod
    def _summary_key() -> str:
        """Return the translation key."""
        return KEY_SUMMARY_TOTAL

    @property
    def native_value(self) -> int:
        """Return the item count."""
        return int(self._counts()["total"])


class SummaryLowStockSensor(_SummarySensor):
    """Number of items currently at or below their low-stock threshold."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:package-down"

    @staticmethod
    def _summary_key() -> str:
        """Return the translation key."""
        return KEY_SUMMARY_LOW_STOCK

    @property
    def native_value(self) -> int:
        """Return the low-stock item count."""
        return int(self._counts()["low_stock"])
