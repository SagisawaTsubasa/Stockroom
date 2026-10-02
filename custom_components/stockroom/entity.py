"""Shared entity base classes for Stockroom.

One virtual device per config entry (the warehouse); item entities bind to a
single item id and refresh via the per-entry dispatcher signal.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SIGNAL_ITEMS_UPDATED
from .inventory import InventoryEngine


@callback
def async_remove_stockroom_entities(
    hass: HomeAssistant, entities: Iterable[Entity]
) -> None:
    """Remove landed entities through the entity registry.

    HA core has no public bulk-removal helper (``async_remove_entities`` on
    ``entity_platform`` does not exist) — this mirrors the group integration
    approach: a registered entity is dropped via the registry (which removes
    the state-machine entity too), an unregistered one via direct removal.
    Entities that never landed (``hass is None`` — e.g. a disabled registry
    entry aborted their add) have nothing to remove.
    """
    registry = er.async_get(hass)
    for entity in entities:
        if entity.hass is None:
            continue
        if entity.registry_entry:
            registry.async_remove(entity.entity_id)
        else:
            hass.async_create_task(entity.async_remove())


def diff_item_ids(
    current: set[str], known: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Compute ``(ids to add, ids to remove)`` for the platform entity diff.

    Shared by both platforms so the add/remove bookkeeping has exactly one
    implementation; callers must register new entities into ``known``
    *before* handing them to ``async_add_entities`` — an add aborted by HA
    (e.g. disabled registry entry) must not be retried on every broadcast.
    """
    return sorted(current - set(known)), sorted(set(known) - current)


class StockroomEntity(Entity):
    """Base for per-item entities bound to one warehouse entry."""

    _attr_has_entity_name = True
    _attr_should_poll = False  # values are dispatcher-pushed, polling is a no-op

    def __init__(self, engine: InventoryEngine, item_id: str, kind: str) -> None:
        """Initialize unique_id / device binding."""
        self._engine = engine
        self._item_id = item_id
        self._attr_unique_id = f"{engine.entry_id}_{item_id}_{kind}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, engine.entry_id)},
            name=engine.warehouse_name,
            manufacturer="Stockroom",
            model="virtual inventory",
        )

    @property
    def item(self) -> dict[str, Any] | None:
        """Return the live item dict, or None once it has been removed."""
        return self._engine.items.get(self._item_id)

    @property
    def available(self) -> bool:
        """Unavailable while the underlying item is gone (pre-removal gap)."""
        return self.item is not None

    async def async_added_to_hass(self) -> None:
        """Subscribe to engine updates.

        If the underlying item was removed between platform diff and add,
        the entity still lands once (``available=False``) and the next
        dispatcher diff removes it — calling ``add_to_platform_abort`` here
        is not supported: the platform would overwrite the REMOVED state and
        then crash writing a hass-less entity.
        """
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_ITEMS_UPDATED.format(self._engine.entry_id),
                self._handle_update,
            )
        )

    def _handle_update(self) -> None:
        """Refresh state on engine broadcasts."""
        self.async_write_ha_state()


class StockroomSummaryEntity(Entity):
    """Base for the two per-entry summary sensors."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, engine: InventoryEngine, key: str) -> None:
        """Initialize the summary entity."""
        self._engine = engine
        self._attr_translation_key = key
        self._attr_unique_id = f"{engine.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, engine.entry_id)},
            name=engine.warehouse_name,
            manufacturer="Stockroom",
            model="virtual inventory",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to engine updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_ITEMS_UPDATED.format(self._engine.entry_id),
                self._handle_update,
            )
        )

    def _handle_update(self) -> None:
        """Refresh state on engine broadcasts."""
        self.async_write_ha_state()
