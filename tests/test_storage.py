"""Storage layer unit tests: dirty-flag batching, retry after failed save,
item-table sectioning and the identity migration hook."""

from __future__ import annotations

import asyncio

from custom_components.stockroom import storage
from custom_components.stockroom.const import STORAGE_KEY, STORAGE_VERSION
from custom_components.stockroom.storage import StockroomStore


def make_store(monkeypatch) -> tuple[StockroomStore, list, dict]:
    saved: list = []
    fail = {"on": False}

    class _RecordingStore(storage._StockroomStore):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.load_result = None

        async def async_save(self, data):
            if fail["on"]:
                raise OSError("disk full")
            saved.append(data)

        async def async_load(self):
            return self.load_result

    monkeypatch.setattr(storage, "_StockroomStore", _RecordingStore)
    return StockroomStore(hass=None), saved, fail


def test_flush_skipped_when_clean(monkeypatch):
    store, saved, _ = make_store(monkeypatch)
    asyncio.run(store.async_flush())
    assert saved == []


def test_flush_persists_dirty_state(monkeypatch):
    store, saved, _ = make_store(monkeypatch)
    store.get_items("e1")["m3x8"] = {"id": "m3x8", "quantity": 5.0}
    store.mark_dirty("e1")
    asyncio.run(store.async_flush())
    assert len(saved) == 1
    assert saved[0]["items"]["e1"]["m3x8"]["quantity"] == 5.0
    # 落盘成功后 dirty 清空：再 flush 不重复写
    asyncio.run(store.async_flush())
    assert len(saved) == 1


def test_failed_save_keeps_dirty_for_retry(monkeypatch):
    store, saved, fail = make_store(monkeypatch)
    store.get_items("e1")["m3x8"] = {"id": "m3x8", "quantity": 1.0}
    store.mark_dirty("e1")

    fail["on"] = True
    asyncio.run(store.async_flush())
    assert saved == []

    fail["on"] = False
    asyncio.run(store.async_flush())
    assert len(saved) == 1


def test_flush_keeps_dirty_flag_set_during_save(monkeypatch):
    """快照语义：await 写盘期间新产生的 dirty 不得被清掉（否则重启丢数据）。"""
    store, saved, _ = make_store(monkeypatch)
    real_save = store._store.async_save

    async def save_then_dirty(data):
        result = await real_save(data)
        store.mark_dirty("e1")  # mutation lands while the "disk write" runs
        return result

    store._store.async_save = save_then_dirty
    store.mark_dirty("e1")
    asyncio.run(store.async_flush())
    assert len(saved) == 1
    assert store.dirty  # mid-save mutation survived the snapshot clear
    asyncio.run(store.async_flush())
    assert len(saved) == 2


def test_store_version_and_key():
    store = StockroomStore(hass=None)
    assert store._store.version == STORAGE_VERSION
    assert store._store.key == STORAGE_KEY


def test_migrate_hook_contract():
    """三参签名命中 HA 的 3 参调用分支；恒等返回且旧版本显式拒绝。"""
    import inspect

    st = storage._StockroomStore(None, STORAGE_VERSION, STORAGE_KEY)
    assert len(inspect.signature(st._async_migrate_func).parameters) == 3
    assert asyncio.run(st._async_migrate_func(1, 1, {"x": 1})) == {"x": 1}
    try:
        asyncio.run(st._async_migrate_func(2, 0, {"x": 1}))
        raised = False
    except NotImplementedError:
        raised = True
    assert raised, "未实现的旧大版本必须显式拒绝而不是静默透传"


def test_load_keeps_canonical_shape(monkeypatch):
    store, _, _ = make_store(monkeypatch)
    store._store.load_result = {
        "items": {"e1": {"m3x8": {"quantity": 3.0}}},
        "meta": {"last_deduct": {"p1s_x": {"task_name": "a.gcode"}}},
        "legacy_junk": {"should": "vanish"},
    }
    asyncio.run(store.async_load())
    assert store.get_items("e1")["m3x8"]["quantity"] == 3.0
    assert storage.last_deduct_records(store)["p1s_x"]["task_name"] == "a.gcode"
    assert "legacy_junk" not in store.data


def test_remove_entry_items_drops_section(monkeypatch):
    store, saved, _ = make_store(monkeypatch)
    store.get_items("e1")["m3x8"] = {"id": "m3x8"}
    store.remove_entry_items("e1")
    # get_items 会把空表重建回来（占位语义），但落盘内容里 e1 已是空表
    assert store.get_items("e1") == {}
    asyncio.run(store.async_flush())
    assert saved[-1]["items"]["e1"] == {}
