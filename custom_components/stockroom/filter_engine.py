"""Filter-slot life engine — Filter-Life-Tracker (FLT) model port.

FLT (the user's retired upstream integration) tracked one source entity per
config entry with N filter levels hanging off it; this engine lifts that
model to Stockroom slot groups:

- ONE shared source entity per group (``meta.filter_slots[][source_entity]``),
  consumed as duration (time-in-state) or count (rising edges, debounced)
- One usage increment is booked to *all* levels in parallel; when a level's
  usage track is exhausted the next level books at ``cascade_factor`` speed
  (simulates串联堵塞的负荷转嫁 — ADR-003: usage track only, dynamic check so
  resetting the upstream level releases the cascade automatically, no
  rollback of already-accelerated usage)
- Dual-track life per level = min(time track, usage track), runtime kept in
  ``meta.filter_runtime`` under the store's dirty-flag batched flush
- Reset (via ``log_filter_change``) zeroes the level's usage and refreshes
  its install date; no active blocking — expiry is displayed (panel) and the
  low-stock sensor covers the consumable side

Collector semantics are ported verbatim from FLT ``engine.py`` (state
boundary tracking, debounce with restart-echo suppression), minus the entity
platforms and dispatcher notifications Stockroom does not have.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)

from .const import (
    DEFAULT_CASCADE_FACTOR,
    DEFAULT_DEBOUNCE,
    DEFAULT_WARN_THRESHOLD,
    DOMAIN,
    META_FILTER_RUNTIME,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
    SOURCE_TYPE_COUNT,
)
from .storage import StockroomStore, filter_slot_groups

_LOGGER = logging.getLogger(__name__)

ENGINE_DATA_KEY = "filter_engine"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_dt(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        # Corrupted storage must not crash every refresh; treat as "just now".
        _LOGGER.warning("滤芯安装日期 %r 无法解析——按当前时间重置", value)
        return _utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _levels_of(group: dict[str, Any]) -> list[int]:
    """Sorted level numbers of a group."""
    return sorted(int(slot["level"]) for slot in group.get("levels", []))


def _level_cfg(group: dict[str, Any], level: int) -> dict[str, Any]:
    """The config dict of one level (empty dict when absent)."""
    for slot in group.get("levels", []):
        if int(slot["level"]) == level:
            return slot
    return {}


class FilterEngine:
    """Tracks life for every slot group that binds a source entity.

    One instance per HA run (``hass.data[DOMAIN][ENGINE_DATA_KEY]``); setup is
    idempotent so entry setups/unloads can call it freely. Groups without a
    source entity are manual-only and never reach the collectors.
    """

    def __init__(self, hass: HomeAssistant, store: StockroomStore) -> None:
        """Initialize the engine (no subscriptions until async_setup)."""
        self.hass = hass
        self.store = store
        self._unsubs: list[Callable[[], None]] = []
        # group_id -> warehouse entry_id, for dirty-flagging runtime changes.
        self._group_entry: dict[str, str] = {}
        # Per-group collector state (a group = one source entity).
        self._tracking_since: dict[str, datetime | None] = {}
        self._debounce_cancel: dict[str, Callable[[], None]] = {}
        # Count sources: suppress the first rising edge after (re)setup when
        # the entity is already in the target state, so a cycle that began
        # before a restart is not counted twice (FLT restart recovery).
        self._suppress_next_rise: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def async_setup(self) -> None:
        """(Re)subscribe collectors for all groups with a source entity."""
        self.async_teardown()
        for entry_id, groups in filter_slot_groups(self.store).items():
            for group in groups:
                self._group_entry[group["group_id"]] = entry_id
                self._setup_group(group)

    def _setup_group(self, group: dict[str, Any]) -> None:
        """Subscribe one group's source entity (duration or count collector)."""
        source = group.get("source_entity")
        if not source:
            return
        group_id = group["group_id"]
        target = str(group.get("target_state") or "")
        if group.get("source_type") == SOURCE_TYPE_COUNT:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, source, self._make_count_handler(group)
                )
            )
            state = self.hass.states.get(source)
            if state is not None and state.state == target:
                self._suppress_next_rise[group_id] = True
                _LOGGER.debug("[%s] 重启时源已处于目标状态——下个上升沿将被抑制", group_id)
        else:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, source, self._make_duration_handler(group)
                )
            )
            state = self.hass.states.get(source)
            if state is not None and state.state == target:
                # Short-restart assumption: start tracking from now.
                self._tracking_since[group_id] = _utcnow()

    @callback
    def async_teardown(self) -> None:
        """Stop all listeners, settle in-flight segments, cancel timers."""
        # Settle duration segments in flight before dropping the state:
        # reload_config runs on every slot save, and silently discarding a
        # running segment would under-count usage (SR-F-027/042). A real HA
        # restart skips this entirely — that gap stays covered by the FLT
        # short-restart assumption in _setup_group.
        #
        # Settlement books against the CURRENT config (re-read from the
        # store), not the setup-time snapshot: a group/level pruned by
        # the very save that triggered this reload must not be resurrected
        # in runtime by the settle write (SR-F-043), and a settle failure
        # must never skip the unsub/timer cleanup below (SR-F-044).
        for group_id, since in list(self._tracking_since.items()):
            if since is None:
                continue
            try:
                current = self._find_current_group(group_id)
                if current is None:
                    continue
                if current.get("source_type") == SOURCE_TYPE_COUNT:
                    # The group was switched to count integration while a
                    # duration segment was in flight: booking the accumulated
                    # seconds as counts would silently inflate usage — drop
                    # the segment; the next real count cycle starts clean
                    # (SR-F-048).
                    continue
                delta = max(0.0, (_utcnow() - since).total_seconds())
                if delta > 0:
                    self._apply_increment(current, delta)
            except Exception:
                _LOGGER.exception(
                    "[%s] teardown 结算进行中用量段失败——该段丢弃，继续清理", group_id
                )
        # Per-item try here as well: one throwing unsub/cancel must not stop
        # the rest of the teardown (SR-F-051, same shape as the settle loop).
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                _LOGGER.exception("滤芯槽位引擎注销监听失败——继续清理")
        self._unsubs.clear()
        for group_id, cancel in self._debounce_cancel.items():
            try:
                cancel()
            except Exception:
                _LOGGER.exception("[%s] 滤芯槽位引擎取消去抖定时器失败", group_id)
        self._debounce_cancel.clear()
        self._tracking_since.clear()
        self._suppress_next_rise.clear()
        self._group_entry.clear()

    def _find_current_group(self, group_id: str) -> dict[str, Any] | None:
        """The group's config as it stands NOW in the store (post-prune)."""
        for groups in filter_slot_groups(self.store).values():
            for group in groups:
                if group.get("group_id") == group_id:
                    return group
        return None

    def reload_config(self) -> None:
        """Re-read group config after the panel saved new slot groups."""
        self.async_setup()

    # ------------------------------------------------------------------
    # Collectors (ported from FLT engine.py)
    # ------------------------------------------------------------------

    def _make_duration_handler(self, group: dict[str, Any]):
        @callback
        def _on_duration_event(event: Event[EventStateChangedData]) -> None:
            new = event.data["new_state"]
            target = str(group.get("target_state") or "")
            group_id = group["group_id"]
            if new is None or new.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                # Entity unavailable: discard the current tracking segment.
                self._tracking_since[group_id] = None
                return
            since = self._tracking_since.get(group_id)
            if since is None and new.state == target:
                self._tracking_since[group_id] = _utcnow()
            elif since is not None and new.state != target:
                # Negative delta clamped to 0 (clock rollback protection).
                delta = max(0.0, (_utcnow() - since).total_seconds())
                self._tracking_since[group_id] = None
                if delta > 0:
                    self._apply_increment(group, delta)

        return _on_duration_event

    def _make_count_handler(self, group: dict[str, Any]):
        @callback
        def _on_count_event(event: Event[EventStateChangedData]) -> None:
            old = event.data["old_state"]
            new = event.data["new_state"]
            target = str(group.get("target_state") or "")
            group_id = group["group_id"]
            if new is None or new.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                cancel = self._debounce_cancel.pop(group_id, None)
                if cancel is not None:
                    cancel()
                return
            old_state = old.state if old is not None else None
            if old_state != target and new.state == target:
                if self._suppress_next_rise.get(group_id):
                    # Restart echo of the pre-restart cycle, not a new one.
                    self._suppress_next_rise[group_id] = False
                    _LOGGER.debug("[%s] 已抑制重启后的回声上升沿", group_id)
                    return
                cancel = self._debounce_cancel.pop(group_id, None)
                if cancel is not None:
                    cancel()
                # 0 is a legal debounce (no window); only None falls back.
                raw_debounce = group.get("debounce")
                debounce = float(
                    DEFAULT_DEBOUNCE if raw_debounce is None else raw_debounce
                )
                self._debounce_cancel[group_id] = async_call_later(
                    self.hass, debounce, self._make_count_confirm(group)
                )
            elif old_state == target and new.state != target:
                # Left target within the debounce window: cancel the count.
                cancel = self._debounce_cancel.pop(group_id, None)
                if cancel is not None:
                    cancel()
                # A real departure observed: the suppression has served its
                # purpose — clear it so the next genuine cycle's rising edge
                # is not swallowed as another restart echo.
                self._suppress_next_rise[group_id] = False

        return _on_count_event

    def _make_count_confirm(self, group: dict[str, Any]):
        @callback
        def _confirm_count(_now: datetime) -> None:
            self._debounce_cancel.pop(group["group_id"], None)
            state = self.hass.states.get(group["source_entity"])
            if state is not None and state.state == str(group.get("target_state") or ""):
                self._apply_increment(group, 1.0)

        return _confirm_count

    # ------------------------------------------------------------------
    # Accumulation + cascade (ported from FLT engine.py:266-301)
    # ------------------------------------------------------------------

    @callback
    def _apply_increment(self, group: dict[str, Any], amount: float) -> None:
        """Book one usage increment to all levels, cascading past exhausted ones."""
        if amount <= 0:
            return
        levels = _levels_of(group)
        for position, level in enumerate(levels):
            increment = amount
            # Cascade is triggered only by the *usage track* exhaustion of
            # the previous level; time-track expiry never cascades.
            if position > 0 and self.usage_exhausted(group, levels[position - 1]):
                raw_factor = _level_cfg(group, level).get("cascade_factor")
                factor = float(
                    DEFAULT_CASCADE_FACTOR if raw_factor is None else raw_factor
                )
                increment = amount * factor
            runtime = self._level_runtime(group, level)
            runtime["usage"] = float(runtime.get("usage") or 0.0) + increment
        # Runtime lives in the shared meta dict — the dirty flag is enough,
        # the store's batched flush persists it.
        self.store.mark_dirty(self._group_entry.get(group["group_id"]))

    # ------------------------------------------------------------------
    # Runtime state
    # ------------------------------------------------------------------

    def _runtime_table(self) -> dict[str, Any]:
        return self.store.get_meta(META_FILTER_RUNTIME)

    def _group_runtime(self, group_id: str) -> dict[str, Any]:
        return self._runtime_table().setdefault(group_id, {})

    def _level_runtime(self, group: dict[str, Any], level: int) -> dict[str, Any]:
        group_runtime = self._group_runtime(group["group_id"])
        key = str(level)
        if key not in group_runtime:
            # First touch creates the record AND flags it dirty immediately:
            # the install date must reach disk, or a restart re-creates it
            # "now" and the time track silently resets to 100% (SR-F-040).
            group_runtime[key] = {
                "usage": 0.0,
                "installed": _utcnow().isoformat(),
            }
            self.store.mark_dirty(self._group_entry.get(group["group_id"]))
        return group_runtime[key]

    # ------------------------------------------------------------------
    # Dual-track life (ported from FLT engine.py:344-381)
    # ------------------------------------------------------------------

    def time_remaining_pct(self, group: dict[str, Any], level: int) -> float | None:
        """Time-track remaining percentage, or None when no time rating set."""
        rated_days = _level_cfg(group, level).get("rated_time_days")
        if rated_days is None:
            return None
        rated = float(rated_days) * SECONDS_PER_DAY
        if rated <= 0:
            return None
        installed = _parse_dt(str(self._level_runtime(group, level).get("installed")))
        elapsed = max(0.0, (_utcnow() - installed).total_seconds())
        return max(0.0, min(100.0, (rated - elapsed) / rated * 100.0))

    def usage_remaining_pct(self, group: dict[str, Any], level: int) -> float | None:
        """Usage-track remaining percentage, or None when no usage rating set."""
        rated_usage = _level_cfg(group, level).get("rated_usage")
        if rated_usage is None:
            return None
        rated = float(rated_usage)
        if group.get("source_type") != SOURCE_TYPE_COUNT:
            rated *= SECONDS_PER_HOUR  # duration type is configured in hours
        if rated <= 0:
            return None
        usage = float(self._level_runtime(group, level).get("usage") or 0.0)
        return max(0.0, min(100.0, (rated - usage) / rated * 100.0))

    def usage_exhausted(self, group: dict[str, Any], level: int) -> bool:
        """Unclamped usage-track exhaustion check (cascade trigger)."""
        cfg = _level_cfg(group, level)
        rated_usage = cfg.get("rated_usage")
        if rated_usage is None:
            return False
        rated = float(rated_usage)
        if group.get("source_type") != SOURCE_TYPE_COUNT:
            rated *= SECONDS_PER_HOUR
        return rated > 0 and float(
            self._level_runtime(group, level).get("usage") or 0.0
        ) >= rated

    def life_pct(self, group: dict[str, Any], level: int) -> float | None:
        """Main life percentage = min(time, usage); None = not trackable."""
        if not group.get("source_entity"):
            return None
        tracks = [
            track
            for track in (
                self.time_remaining_pct(group, level),
                self.usage_remaining_pct(group, level),
            )
            if track is not None
        ]
        return min(tracks) if tracks else 100.0

    def level_view(self, group: dict[str, Any], level: int) -> dict[str, Any]:
        """Per-level life snapshot for the panel overview."""
        pct = self.life_pct(group, level)
        # 0 is a legal threshold (warn never shows); only None falls back.
        raw_warn = _level_cfg(group, level).get("warn_threshold")
        warn_at = float(
            DEFAULT_WARN_THRESHOLD if raw_warn is None else raw_warn
        )
        return {
            "pct": pct,
            "warn": pct is not None and pct < warn_at,
            "expired": pct is not None and pct <= 0.0,
        }

    # ------------------------------------------------------------------
    # Reset (panel 换芯 / log_filter_change)
    # ------------------------------------------------------------------

    @callback
    def reset_level(self, group: dict[str, Any], level: int) -> bool:
        """Zero one level's usage and refresh its install date.

        Returns True when a runtime record exists or the group tracks a
        source (manual groups without any rating have nothing to reset).
        """
        tracked = bool(group.get("source_entity"))
        runtime = self._group_runtime(group["group_id"]).get(str(level))
        if not tracked and runtime is None:
            return False
        self._level_runtime(group, level).update(
            {"usage": 0.0, "installed": _utcnow().isoformat()}
        )
        self.store.mark_dirty(self._group_entry.get(group["group_id"]))
        return True

    @callback
    def sync_runtimes(
        self, kept_groups: list[dict[str, Any]], removed_group_ids: set[str]
    ) -> None:
        """Align runtime state with a fresh slot-group config (slots save).

        Drops removed groups entirely and prunes levels that no longer exist
        inside kept groups; flags the store dirty when anything actually went
        away so the shrink is persisted too (SR-F-035/041). Callers mark
        their own warehouse dirty as well — this covers cross-warehouse
        bookkeeping only.
        """
        table = self._runtime_table()
        for group_id in removed_group_ids:
            table.pop(group_id, None)
        for group in kept_groups:
            group_runtime = table.get(group["group_id"])
            if not group_runtime:
                continue
            valid = {str(slot["level"]) for slot in group.get("levels", [])}
            stale = [key for key in group_runtime if key not in valid]
            for key in stale:
                del group_runtime[key]
            if stale:
                self.store.mark_dirty(self._group_entry.get(group["group_id"]))


def get_filter_engine(hass: HomeAssistant) -> FilterEngine | None:
    """The engine when the integration is loaded, else None."""
    return hass.data.get(DOMAIN, {}).get(ENGINE_DATA_KEY)
