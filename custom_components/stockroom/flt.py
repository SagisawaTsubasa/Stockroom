"""Filter-Life-Tracker lookup helpers (optional-integration queries).

Everything here degrades gracefully when FLT is not installed, not loaded,
or its entities are missing: lookups return ``None``/empty lists and the
callers decide how to present that. No module in this integration imports
FLT directly — the domain, unique_id patterns and event name mirror FLT's
const.py as literals (see the note in const.py).

FLT unique_id shapes (from its entity platforms):
  sensor         f"{entry_id}_life_{level}"        — main life %
                 f"{entry_id}_life_time_{level}"
                 f"{entry_id}_life_usage_{level}"
  button         f"{entry_id}_reset_{level}"
  binary_sensor  f"{entry_id}_warn_{level}"
                 f"{entry_id}_expired_{level}"
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import FLT_DOMAIN, FLT_ENTRY_TYPE_DEVICE

_LOGGER = logging.getLogger(__name__)

_STATE_UNAVAILABLE = "unavailable"
_STATE_UNKNOWN = "unknown"


def _life_unique_id(flt_entry_id: str, level: int) -> str:
    return f"{flt_entry_id}_life_{level}"


def resolve_entity_id(hass: HomeAssistant, platform: str, unique_id: str) -> str | None:
    """Resolve an entity_id from the entity registry, or None when missing."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    if registry is None:
        return None
    return registry.async_get_entity_id(platform, FLT_DOMAIN, unique_id)


def read_life_pct(hass: HomeAssistant, flt_entry_id: str, level: int) -> float | None:
    """Read the main-life sensor's current percentage, None when unreadable.

    Best-effort by design: called from the reset-event path where the value
    may already have been reset to 100 — callers record it as-is.
    """
    entity_id = resolve_entity_id(hass, "sensor", _life_unique_id(flt_entry_id, level))
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (_STATE_UNAVAILABLE, _STATE_UNKNOWN):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def discover_flt_devices(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Describe every FLT device-type entry for the panel's binding pickers.

    Returns a list of ``{"entry_id", "title", "levels": [...]}`` where each
    level carries the resolved entity_ids and their live values. Empty list
    when FLT is absent — the panel then only offers manual slots.
    """
    out: list[dict[str, Any]] = []
    for entry in hass.config_entries.async_entries(FLT_DOMAIN):
        if entry.data.get("entry_type") != FLT_ENTRY_TYPE_DEVICE:
            continue
        levels: list[dict[str, Any]] = []
        for level_str in entry.data.get("filters", {}):
            try:
                level = int(level_str)
            except (TypeError, ValueError):
                continue
            life_entity = resolve_entity_id(
                hass, "sensor", _life_unique_id(entry.entry_id, level)
            )
            reset_entity = resolve_entity_id(
                hass, "button", f"{entry.entry_id}_reset_{level}"
            )
            expired_entity = resolve_entity_id(
                hass, "binary_sensor", f"{entry.entry_id}_expired_{level}"
            )
            life_state = hass.states.get(life_entity) if life_entity else None
            life_pct: float | None = None
            if life_state is not None and life_state.state not in (
                _STATE_UNAVAILABLE,
                _STATE_UNKNOWN,
            ):
                try:
                    life_pct = float(life_state.state)
                except (TypeError, ValueError):
                    life_pct = None
            expired_state = hass.states.get(expired_entity) if expired_entity else None
            levels.append(
                {
                    "level": level,
                    "life_entity_id": life_entity,
                    "life_pct": life_pct,
                    "reset_entity_id": reset_entity,
                    "expired": bool(expired_state and expired_state.state == "on"),
                }
            )
        levels.sort(key=lambda item: item["level"])
        out.append({"entry_id": entry.entry_id, "title": entry.title, "levels": levels})
    return out
