"""Inventory engine unit tests: item CRUD, consume clamp, stocktake stamps,
low-stock judgement, change events and Bambu AMS idempotent deduction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.stockroom import bambu as bambu_mod
from custom_components.stockroom import inventory as inventory_mod
from custom_components.stockroom import storage as storage_mod
from custom_components.stockroom.const import EVENT_ITEM_CHANGED
from custom_components.stockroom.inventory import (
    InventoryEngine,
    StockroomItemError,
    slugify_name,
)
from custom_components.stockroom.storage import StockroomStore

# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.listeners: list[tuple[str, object]] = []

    def async_fire(self, event_type, data=None):
        self.events.append((event_type, dict(data or {})))

    def async_listen(self, event_type, callback):
        self.listeners.append((event_type, callback))
        return lambda: None


class FakeStates:
    def __init__(self, mapping=None) -> None:
        self.mapping = mapping or {}

    def get(self, entity_id):
        return self.mapping.get(entity_id)


class FakeState:
    def __init__(self, state, attributes=None) -> None:
        self.state = state
        self.attributes = attributes or {}


class FakeHass:
    def __init__(self, states=None) -> None:
        self.bus = FakeBus()
        self.states = FakeStates(states)
        self.tasks: list = []

    def async_create_task(self, coro):
        self.tasks.append(coro)

    def drain(self):
        """Close never-awaited flush coroutines quietly."""
        for coro in self.tasks:
            coro.close()
        self.tasks.clear()


class FakeEntry:
    def __init__(self, data, options=None) -> None:
        self.data = data
        self.options = options or {}
        self.entry_id = "test_entry"
        self.title = data.get("warehouse_name", "仓库")


def make_store(monkeypatch) -> StockroomStore:
    class _RecordingStore(storage_mod._StockroomStore):
        async def async_save(self, data):
            self.saved.append(data)

    _RecordingStore.saved = []
    monkeypatch.setattr(storage_mod, "_StockroomStore", _RecordingStore)
    return StockroomStore(hass=None)


def make_engine(monkeypatch, options=None, states=None):
    store = make_store(monkeypatch)
    hass = FakeHass(states)
    entry = FakeEntry({"warehouse_name": "测试仓库"}, options)
    sent: list[str] = []
    monkeypatch.setattr(
        inventory_mod, "async_dispatcher_send",
        lambda hass_, signal: sent.append(signal),
    )
    engine = InventoryEngine(hass, entry, store)
    return engine, store, hass, sent


# ----------------------------------------------------------------------
# Slugify
# ----------------------------------------------------------------------


def test_slugify_ascii_symbols():
    assert slugify_name("M3×8") == "m3x8"


def test_slugify_cjk_pinyin():
    assert slugify_name("PLA-白色") == "pla-bai-se"


def test_slugify_fallback_hash_for_unknown():
    slug = slugify_name("Ω≈ç√")
    assert slug.startswith("item-")


# ----------------------------------------------------------------------
# Item CRUD
# ----------------------------------------------------------------------


def test_add_item_auto_slug_and_defaults(monkeypatch):
    engine, store, hass, sent = make_engine(monkeypatch)
    item = engine.add_item(name="M3×8", category="screw", quantity=100)
    assert item["id"] == "m3x8"
    assert item["unit"] == "颗"  # screw default unit
    assert item["low_threshold"] == 1.0  # default threshold
    assert engine.items["m3x8"] is item
    assert sent == ["stockroom_items_updated_test_entry"]
    assert store.dirty
    # event with action=add
    fired = [e for e in hass.bus.events if e[0] == EVENT_ITEM_CHANGED]
    assert fired[-1][1]["item_id"] == "m3x8"
    assert fired[-1][1]["action"] == "add"
    assert fired[-1][1]["new_quantity"] == 100.0


def test_add_item_collision_appends_suffix(monkeypatch):
    engine, *_ = make_engine(monkeypatch)
    first = engine.add_item(name="M3×8", category="screw")
    second = engine.add_item(name="M3×8", category="screw")
    assert first["id"] == "m3x8"
    assert second["id"] == "m3x8-2"


def test_add_item_explicit_id_conflict_raises(monkeypatch):
    engine, *_ = make_engine(monkeypatch)
    engine.add_item(name="M3×8", item_id="m3x8")
    with pytest.raises(StockroomItemError):
        engine.add_item(name="another", item_id="m3x8")


def test_add_item_negative_quantity_clamped(monkeypatch):
    engine, *_ = make_engine(monkeypatch)
    item = engine.add_item(name="垫片", quantity=-5)
    assert item["quantity"] == 0.0


def test_add_item_filament_extra_and_unit(monkeypatch):
    engine, *_ = make_engine(monkeypatch)
    item = engine.add_item(
        name="PLA 白色",
        category="filament",
        quantity=1000,
        extra={"material": "PLA", "color": "#FFFFFF", "full_weight_g": 1000},
    )
    assert item["unit"] == "g"
    assert item["extra"] == {"material": "PLA", "color": "#FFFFFF", "full_weight_g": 1000}


def test_remove_item_by_id_name_and_ambiguity(monkeypatch):
    engine, _, hass, _ = make_engine(monkeypatch)
    engine.add_item(name="M3×8 螺丝", category="screw")
    engine.add_item(name="PLA 白色", category="filament")
    # by id
    removed = engine.remove_item("m3x8-luo-si")
    assert removed["id"] == "m3x8-luo-si"
    # by exact name
    removed = engine.remove_item("PLA 白色")
    assert removed["category"] == "filament"
    assert not engine.items
    # remove event fired with action=remove
    fired = [e for e in hass.bus.events if e[0] == EVENT_ITEM_CHANGED]
    assert fired[-1][1]["action"] == "remove"
    assert fired[-1][1]["new_quantity"] is None


def test_resolve_ambiguous_and_unknown(monkeypatch):
    engine, *_ = make_engine(monkeypatch)
    engine.add_item(name="M3×8 螺丝", category="screw")
    engine.add_item(name="M3×8 螺母", category="screw")
    # exact id wins despite name overlap
    assert engine.resolve_item_id("m3x8-luo-mu") == "m3x8-luo-mu"
    with pytest.raises(StockroomItemError):
        engine.resolve_item_id("M3×8")  # fuzzy hits two names
    with pytest.raises(StockroomItemError):
        engine.resolve_item_id("不存在")


def test_updated_at_stamped_on_mutation(monkeypatch):
    engine, *_ = make_engine(monkeypatch)
    item = engine.add_item(name="垫片")
    before = item["updated_at"]
    engine.restock("垫片", 1)
    assert item["updated_at"] >= before


# ----------------------------------------------------------------------
# Consume / restock / stocktake / threshold
# ----------------------------------------------------------------------


def test_consume_clamps_at_zero(monkeypatch):
    engine, _, hass, _ = make_engine(monkeypatch)
    engine.add_item(name="M3×8", category="screw", quantity=3)
    item = engine.consume("M3×8", 5, note="装配件")
    assert item["quantity"] == 0.0
    fired = [e for e in hass.bus.events if e[0] == EVENT_ITEM_CHANGED]
    assert fired[-1][1]["new_quantity"] == 0.0
    assert fired[-1][1]["note"] == "装配件"


def test_restock_adds(monkeypatch):
    engine, *_ = make_engine(monkeypatch)
    engine.add_item(name="垫片", quantity=1)
    item = engine.restock("垫片", 9)
    assert item["quantity"] == 10.0


def test_stocktake_sets_quantity_and_timestamp(monkeypatch):
    engine, _, hass, _ = make_engine(monkeypatch)
    engine.add_item(name="PLA 白色", category="filament", quantity=1000)
    before = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=1)
    item = engine.stocktake("PLA 白色", 850)
    assert item["quantity"] == 850.0
    stamped = datetime.fromisoformat(item["last_stocktake"])
    assert before <= stamped.replace(microsecond=0) <= datetime.now(UTC)
    fired = [e for e in hass.bus.events if e[0] == EVENT_ITEM_CHANGED]
    assert fired[-1][1]["action"] == "stocktake"


def test_set_threshold_and_low_stock(monkeypatch):
    engine, *_ = make_engine(monkeypatch)
    item = engine.add_item(name="M3×8", category="screw", quantity=10, low_threshold=2)
    engine.set_threshold("M3×8", 10)
    assert item["low_threshold"] == 10.0
    assert InventoryEngine.is_low_stock(item) is True  # <= threshold
    item["quantity"] = 10.1
    assert InventoryEngine.is_low_stock(item) is False


def test_summary_counts_with_category_breakdown(monkeypatch):
    engine, *_ = make_engine(monkeypatch, options={"summary_by_category": True})
    engine.add_item(name="PLA 白色", category="filament", quantity=5, low_threshold=10)
    engine.add_item(name="PLA 黑色", category="filament", quantity=50, low_threshold=10)
    engine.add_item(name="M3×8", category="screw", quantity=1, low_threshold=5)
    counts = engine.summary_counts()
    assert counts["total"] == 3
    assert counts["low_stock"] == 2
    assert counts["by_category"] == {"filament": 2, "screw": 1, "other": 0}
    assert counts["low_by_category"] == {"filament": 1, "screw": 1, "other": 0}


# ----------------------------------------------------------------------
# Bambu AMS auto-deduction
# ----------------------------------------------------------------------


def test_parse_tray_entity():
    assert bambu_mod.parse_tray_entity("sensor.p1s_x_ams_1_tray_2") == (
        "p1s_x",
        "ams_1_tray_2",
    )
    # greedy prefix: device names containing _ams_ themselves still resolve
    assert bambu_mod.parse_tray_entity("sensor.p1s_ams_kit_01_ams_12_tray_1") == (
        "p1s_ams_kit_01",
        "ams_12_tray_1",
    )
    # external spool / unrelated entities are not tray sensors
    assert bambu_mod.parse_tray_entity("sensor.p1s_x_externalspool_external_spool") is None
    assert bambu_mod.parse_tray_entity("sensor.unrelated") is None


def test_bambu_extract_tray_weights():
    weights = bambu_mod.extract_tray_weights(
        {"AMS 1 Tray 2": 65.28, "AMS 1 Tray 1": 10, "weight": 75.28, "AMS 2 Tray 1": "bad"}
    )
    assert weights == {"ams_1_tray_1": 10.0, "ams_1_tray_2": 65.28}


def test_bambu_should_deduct_idempotent_by_task():
    record = {"task_name": "job.gcode", "start_time": "10:00", "deducted_at": "t0"}
    assert bambu_mod.should_deduct(record, "job.gcode", "10:00") == (False, "same_task")
    assert bambu_mod.should_deduct(record, "job.gcode", "10:05") == (True, "new_task")
    assert bambu_mod.should_deduct(record, "other.gcode", "10:00") == (True, "new_task")
    assert bambu_mod.should_deduct({}, "job.gcode", "10:00") == (True, "new_task")


def test_bambu_should_deduct_never_without_task_identity():
    """读不到任务身份（任一字段缺失）时宁可漏扣（可 stocktake 补），绝不盲扣。"""
    record = {"task_name": "old.gcode", "start_time": "09:00", "deducted_at": "t0"}
    assert bambu_mod.should_deduct(record, None, None) == (False, "task_identity_unreadable")
    assert bambu_mod.should_deduct({}, None, "10:00") == (False, "task_identity_unreadable")
    # task_name readable but start_time missing -> identity incomplete, no deduct
    assert bambu_mod.should_deduct({}, "job.gcode", None) == (False, "task_identity_unreadable")


def test_diff_item_ids_computes_add_and_remove():
    """平台 diff 纯函数：add 取差集、remove 取消失集，且不改 known。"""
    from custom_components.stockroom.entity import diff_item_ids

    known = {"a": object(), "b": object()}
    add_ids, remove_ids = diff_item_ids({"b", "c", "d"}, known)
    assert add_ids == ["c", "d"]
    assert remove_ids == ["a"]
    assert set(known) == {"a", "b"}  # known untouched; caller registers additions


def test_diff_item_ids_empty_sides():
    from custom_components.stockroom.entity import diff_item_ids

    assert diff_item_ids(set(), {}) == ([], [])
    assert diff_item_ids({"x"}, {}) == (["x"], [])
    assert diff_item_ids(set(), {"y": object()}) == ([], ["y"])


def _make_deductor(monkeypatch, states, tray_map=None):
    engine, store, hass, _ = make_engine(monkeypatch, states=states)
    engine.add_item(name="PLA 白色", category="filament", quantity=1000, unit="g")
    deductor = bambu_mod.BambuDeductor(
        hass, engine.entry, engine, store,
        tray_map or {"sensor.p1s_x_ams_1_tray_2": engine.items["pla-bai-se"]["id"]},
    )
    return engine, store, hass, deductor


def _status_event(new_state, old_state):
    class _Event:
        def __init__(self) -> None:
            self.data = {"new_state": new_state, "old_state": old_state}

    return _Event()


def test_bambu_finish_deducts_once(monkeypatch):
    states = {
        "sensor.p1s_x_print_status": FakeState("finish"),
        "sensor.p1s_x_print_weight": FakeState(
            "75.28", {"AMS 1 Tray 2": 65.28, "AMS 1 Tray 1": 10.0, "weight": 75.28}
        ),
        "sensor.p1s_x_task_name": FakeState("benchy.gcode.3mf"),
        "sensor.p1s_x_start_time": FakeState("2026-10-02 10:00:00"),
    }
    engine, store, hass, deductor = _make_deductor(monkeypatch, states)
    deductor._handle_finish("p1s_x")
    assert engine.items["pla-bai-se"]["quantity"] == pytest.approx(1000 - 65.28)
    # tray 1 not mapped -> untouched; record keyed per entry|printer
    record = store.data["meta"]["last_deduct"]["test_entry|p1s_x"]
    assert record["task_name"] == "benchy.gcode.3mf"
    # replay of the same finish (HA reload) must not double-deduct
    deductor._handle_finish("p1s_x")
    assert engine.items["pla-bai-se"]["quantity"] == pytest.approx(1000 - 65.28)
    # one consume event per actual deduction (the "add" event also fired)
    fired = [
        e for e in hass.bus.events
        if e[0] == EVENT_ITEM_CHANGED and e[1]["action"] == "consume"
    ]
    assert len(fired) == 1
    assert "AMS" in fired[0][1]["note"]
    hass.drain()


def test_bambu_replayed_finish_does_not_deduct(monkeypatch):
    """None→finish（HA 重启重放）与 offline→finish（重连）不得扣料。"""
    states = {
        "sensor.p1s_x_print_status": FakeState("finish"),
        "sensor.p1s_x_print_weight": FakeState("65.28", {"AMS 1 Tray 2": 65.28}),
        "sensor.p1s_x_task_name": FakeState("benchy.gcode.3mf"),
        "sensor.p1s_x_start_time": FakeState("2026-10-02 10:00:00"),
    }
    engine, _, hass, deductor = _make_deductor(monkeypatch, states)
    # HA restart: no old_state at all
    deductor._handle_status_event("p1s_x", _status_event(FakeState("finish"), None))
    # printer reconnect: offline is not a running state
    deductor._handle_status_event(
        "p1s_x", _status_event(FakeState("finish"), FakeState("offline"))
    )
    assert engine.items["pla-bai-se"]["quantity"] == 1000.0
    # a real completion from a running state deducts exactly once
    deductor._handle_status_event(
        "p1s_x", _status_event(FakeState("finish"), FakeState("running"))
    )
    assert engine.items["pla-bai-se"]["quantity"] == pytest.approx(1000 - 65.28)
    hass.drain()


def test_bambu_unreadable_task_identity_skips(monkeypatch):
    """task_name/start_time 不可读时宁漏勿重：不扣减、不落幂等记录。"""
    states = {
        "sensor.p1s_x_print_weight": FakeState("65.28", {"AMS 1 Tray 2": 65.28}),
        # task_name / start_time entities unknown -> identity unreadable
        "sensor.p1s_x_task_name": FakeState("unknown"),
        "sensor.p1s_x_start_time": FakeState("unknown"),
    }
    engine, store, hass, deductor = _make_deductor(monkeypatch, states)
    deductor._handle_status_event(
        "p1s_x", _status_event(FakeState("finish"), FakeState("running"))
    )
    assert engine.items["pla-bai-se"]["quantity"] == 1000.0
    assert not store.data["meta"]["last_deduct"]
    hass.drain()


def test_bambu_finish_without_tray_keys_skips(monkeypatch):
    states = {
        "sensor.p1s_x_print_status": FakeState("finish"),
        "sensor.p1s_x_print_weight": FakeState("75.28", {"weight": 75.28}),
        "sensor.p1s_x_task_name": FakeState("benchy.gcode.3mf"),
        "sensor.p1s_x_start_time": FakeState("2026-10-02 10:00:00"),
    }
    engine, store, hass, deductor = _make_deductor(monkeypatch, states)
    deductor._handle_finish("p1s_x")
    assert engine.items["pla-bai-se"]["quantity"] == 1000.0
    # finish is recorded as consumed anyway (no retry loop)
    assert "test_entry|p1s_x" in store.data["meta"]["last_deduct"]
    hass.drain()


def test_bambu_finish_missing_item_is_noop(monkeypatch):
    states = {
        "sensor.p1s_x_print_weight": FakeState("1", {"AMS 1 Tray 2": 5}),
        "sensor.p1s_x_task_name": FakeState("t"),
    }
    engine, _store, hass, deductor = _make_deductor(
        monkeypatch, states, {"sensor.p1s_x_ams_1_tray_2": "gone"}
    )
    deductor._handle_finish("p1s_x")
    assert "gone" not in engine.items
    hass.drain()


# ----------------------------------------------------------------------
# Whole-package import smoke test (catches import-time NameErrors)
# ----------------------------------------------------------------------


def test_all_modules_import():
    import importlib

    for name in (
        "const",
        "storage",
        "bambu",
        "inventory",
        "entity",
        "sensor",
        "binary_sensor",
        "config_flow",
        "scan",
    ):
        importlib.import_module(f"custom_components.stockroom.{name}")
    importlib.import_module("custom_components.stockroom")
