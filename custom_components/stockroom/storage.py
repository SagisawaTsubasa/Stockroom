"""Persistence layer for Stockroom (仓储盘点).

Inventory data never lives in the Config Entry — it is runtime state flushed
to a shared JSON store (``stockroom.storage``). Layout::

    {
        "items": {entry_id: {item_id: item_dict}},
        "meta": {"last_deduct": {printer_prefix: {...}}},
    }

``meta`` is shared across entries; it currently only holds the Bambu AMS
deduction idempotency records keyed ``"{entry_id}|{printer_prefix}"`` so that
multiple warehouses mapping the same printer never shadow each other.

Schema migrations follow the Filter-Life-Tracker precedent: HA's ``Store``
wraps the payload with its own version envelope and recent HA removed the
``migrate_func=`` constructor argument — migration is done by subclassing and
overriding ``_async_migrate_func``. STORAGE_VERSION starts at 1, so the hook
is an identity; raise STORAGE_VERSION and convert ``old_data`` there when the
payload ever changes shape.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import META_LAST_DEDUCT, STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


class _StockroomStore(Store[dict[str, Any]]):
    """Store with an explicit (identity) migration hook for future versions."""

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        """STORAGE_VERSION has always been 1 — nothing to convert yet.

        Guard: a future STORAGE_VERSION bump without updating this hook must
        fail loudly instead of silently treating old-shaped data as current.
        """
        if old_major_version != 1:
            raise NotImplementedError(
                f"no migration from stockroom storage v{old_major_version}"
            )
        return old_data


class StockroomStore:
    """Shared JSON store with batched (10 min) flushing."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the store."""
        self._store: Store[dict[str, Any]] = _StockroomStore(
            hass, STORAGE_VERSION, STORAGE_KEY
        )
        self.data: dict[str, Any] = {"items": {}, "meta": {}}
        self._dirty_entries: set[str] = set()
        self._flush_lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Load stored data, keeping the canonical top-level shape."""
        data = await self._store.async_load()
        if not data:
            return
        self.data["items"] = data.get("items", {})
        self.data["meta"] = data.get("meta", {})

    def get_items(self, entry_id: str) -> dict[str, dict[str, Any]]:
        """Return the (mutable) item table of one warehouse entry."""
        return self.data["items"].setdefault(entry_id, {})

    def remove_entry_items(self, entry_id: str) -> None:
        """Drop the item table of a removed entry."""
        self.data["items"].pop(entry_id, None)
        self.mark_dirty(entry_id)

    def get_meta(self, key: str) -> dict[str, Any]:
        """Return a mutable shared metadata section."""
        return self.data["meta"].setdefault(key, {})

    def mark_dirty(self, entry_id: str | None = None) -> None:
        """Mark the store as needing a flush (entry_id kept for logging)."""
        if entry_id:
            self._dirty_entries.add(entry_id)

    @property
    def dirty(self) -> bool:
        """Return True when unsaved changes are pending."""
        return bool(self._dirty_entries)

    async def async_flush(self, *_: Any) -> bool:
        """Flush to disk if anything is dirty; return True once persisted.

        The dirty set is snapshotted and cleared *before* the await: during
        ``async_save`` the event loop stays free, so a service call landing
        in that gap must not lose its dirty flag when the save finishes
        (the flag set after the snapshot is simply left for the next flush).
        A failed save merges the snapshot back so the next flush retries;
        concurrent triggers (timer / service / unload / shutdown) are
        serialized by the lock to avoid double writes.
        """
        async with self._flush_lock:
            if not self._dirty_entries:
                return False
            pending, self._dirty_entries = self._dirty_entries, set()
            try:
                await self._store.async_save(self.data)
            except Exception:
                self._dirty_entries |= pending
                _LOGGER.exception("Failed to persist stockroom state — will retry on next flush")
                return False
            return True


def last_deduct_records(store: StockroomStore) -> dict[str, Any]:
    """Return the shared per-printer idempotency record table."""
    return store.get_meta(META_LAST_DEDUCT)


def schedule_store_flush(hass: HomeAssistant, store: StockroomStore) -> None:
    """Ask for a flush from any thread.

    On the event loop this is a plain task; from executor threads
    (``async_create_task`` would raise there) the task creation is forwarded
    onto the loop instead. The coroutine is created only in the branch that
    consumes it, so a failed scheduling can't leak it.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        hass.loop.call_soon_threadsafe(hass.async_create_task, store.async_flush())
    else:
        hass.async_create_task(store.async_flush())
