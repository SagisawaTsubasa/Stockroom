"""Photo scan pipeline tests: LLM reply parsing, image extraction, the
pending queue and companion notification action routing."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import types

import pytest

from custom_components.stockroom import scan as scan_mod
from custom_components.stockroom.const import (
    ACTION_CONFIRM_PREFIX,
    ACTION_DISMISS_PREFIX,
    SCAN_MAX_QUEUE,
)
from custom_components.stockroom.inventory import InventoryEngine
from custom_components.stockroom.scan import (
    ScanManager,
    extract_image_bytes,
    parse_suggestions,
)
from custom_components.stockroom.storage import StockroomStore
from tests.test_inventory import FakeHass, make_store

# JPEG / PNG 容器魔数（写入测试样本用）
JPEG_SAMPLE = b"\xff\xd8" + b"fake-jpeg-payload"
PNG_SAMPLE = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload"


def asyncio_run(coro):
    return asyncio.run(coro)


def make_manager(monkeypatch, options=None) -> tuple[ScanManager, InventoryEngine, StockroomStore, FakeHass]:
    store = make_store(monkeypatch)
    hass = FakeHass()
    entry = types.SimpleNamespace(
        data={"warehouse_name": "测试仓库"},
        options=options or {},
        entry_id="test_entry",
        title="测试仓库",
    )
    engine = InventoryEngine(hass, entry, store)
    manager = ScanManager(hass, entry, engine, store)
    return manager, engine, store, hass


# ----------------------------------------------------------------------
# parse_suggestions
# ----------------------------------------------------------------------


def test_parse_suggestions_plain_array():
    content = json.dumps(
        [{"name": "M3×8", "category": "screw", "quantity": 100, "unit": "颗"}]
    )
    out = parse_suggestions(content)
    assert out == [
        {"name": "M3×8", "category": "screw", "quantity": 100.0, "unit": "颗",
         "low_threshold": 0.0, "location": ""}
    ]


def test_parse_suggestions_strips_fences_and_items_wrapper():
    fenced = "```json\n" + json.dumps({"items": [{"name": "PLA 白色", "category": "filament"}]}) + "\n```"
    out = parse_suggestions(fenced)
    assert len(out) == 1
    assert out[0]["category"] == "filament"
    assert out[0]["unit"] == "g"  # filament default unit
    assert out[0]["quantity"] == 0.0  # unknown quantity grounded to 0


def test_parse_suggestions_extra_fields():
    content = json.dumps(
        [{"name": "PLA 白色", "category": "filament", "material": "PLA",
          "color": "白色", "full_weight_g": 1000}]
    )
    out = parse_suggestions(content)
    assert out[0]["extra"] == {"material": "PLA", "color": "白色", "full_weight_g": 1000.0}


def test_parse_suggestions_garbage_is_dropped():
    assert parse_suggestions("完全不是 JSON") == []
    assert parse_suggestions('{"no": "array"}') == []
    assert parse_suggestions(json.dumps([{"no_name": True}, "junk", 42])) == []


def test_parse_suggestions_bad_category_defaults_other():
    out = parse_suggestions(json.dumps([{"name": "抹布", "category": "tool"}]))
    assert out[0]["category"] == "other"


def test_parse_suggestions_rejects_non_finite_numbers():
    """模型幻觉出 Infinity/1e999 时按 0 处理，不能把 inf 写进库存。"""
    out = parse_suggestions(
        '{"items": [{"name": "X", "quantity": Infinity, "low_threshold": 1e999}]}'
    )
    assert out[0]["quantity"] == 0.0
    assert out[0]["low_threshold"] == 0.0
    out = parse_suggestions('[{"name": "Y", "quantity": NaN}]')
    assert out[0]["quantity"] == 0.0


# ----------------------------------------------------------------------
# extract_image_bytes
# ----------------------------------------------------------------------


def _json_request(payload, content_type="application/json"):
    class _Req:
        async def json(self):
            if isinstance(payload, Exception):
                raise payload
            return payload

    req = _Req()
    req.content_type = content_type
    return req


def _multipart_request(fields):
    class _Req:
        content_type = "multipart/form-data"

        async def post(self):
            return fields

    return _Req()


def test_extract_image_json_base64():
    req = _json_request({"image_base64": base64.b64encode(JPEG_SAMPLE).decode()})
    assert asyncio_run(extract_image_bytes(req)) == JPEG_SAMPLE


def test_extract_image_errors():
    with pytest.raises(ValueError):
        asyncio_run(extract_image_bytes(_json_request({})))
    with pytest.raises(ValueError):
        asyncio_run(extract_image_bytes(_json_request({"image_base64": "!!!!not-base64!!!"})))
    with pytest.raises(ValueError):
        asyncio_run(extract_image_bytes(_json_request({"image_base64": ""})))
    with pytest.raises(ValueError):
        asyncio_run(extract_image_bytes(_multipart_request({})))


def test_extract_image_rejects_non_object_json():
    req = _json_request([1, 2, 3])
    with pytest.raises(ValueError):
        asyncio_run(extract_image_bytes(req))


def test_extract_image_rejects_non_image_magic():
    raw = base64.b64encode(b"just some text, definitely not an image").decode()
    req = _json_request({"image_base64": raw})
    with pytest.raises(ValueError):
        asyncio_run(extract_image_bytes(req))


def test_extract_image_multipart_file_field():
    field = types.SimpleNamespace(file=io.BytesIO(JPEG_SAMPLE))
    req = _multipart_request({"image": field})
    assert asyncio_run(extract_image_bytes(req)) == JPEG_SAMPLE


def test_extract_image_multipart_file_field_via_executor():
    """传了 hass 时文件读取走 executor（家法：事件循环内不裸读文件）。"""
    field = types.SimpleNamespace(file=io.BytesIO(PNG_SAMPLE))
    reads = []

    class _Hass:
        async def async_add_executor_job(self, func, *args):
            reads.append(func)
            return func(*args)

    req = _multipart_request({"image": field})
    assert asyncio_run(extract_image_bytes(req, _Hass())) == PNG_SAMPLE
    assert len(reads) == 1


def test_extract_image_multipart_base64_field():
    req = _multipart_request({"image": base64.b64encode(PNG_SAMPLE).decode()})
    assert asyncio_run(extract_image_bytes(req)) == PNG_SAMPLE


# ----------------------------------------------------------------------
# Pending queue: queue / confirm / dismiss
# ----------------------------------------------------------------------


def test_queue_roundtrip_and_eviction(monkeypatch):
    manager, _engine, _store, hass = make_manager(monkeypatch)
    first = manager.queue_suggestions([{"name": "M3×8", "category": "screw", "quantity": 100}])
    assert len(first) == 1
    assert first[0]["id"] in manager.queue()

    # fill beyond the cap -> oldest evicted
    manager.queue_suggestions([{"name": f"条目{i}", "category": "other"} for i in range(SCAN_MAX_QUEUE)])
    assert len(manager.queue()) == SCAN_MAX_QUEUE
    assert first[0]["id"] not in manager.queue()
    assert manager.store.dirty
    hass.drain()


def test_confirm_creates_item_and_drops_suggestion(monkeypatch):
    manager, engine, _store, hass = make_manager(monkeypatch)
    (entry,) = manager.queue_suggestions(
        [{"name": "M3×8", "category": "screw", "quantity": 100}]
    )
    item = manager.confirm(entry["id"], overrides={"quantity": 250, "location": "柜2"})
    assert item["quantity"] == 250.0
    assert item["location"] == "柜2"
    assert engine.items["m3x8"]["quantity"] == 250.0
    assert entry["id"] not in manager.queue()
    hass.drain()


def test_confirm_unknown_sid_returns_none(monkeypatch):
    manager, *_ = make_manager(monkeypatch)
    assert manager.confirm("deadbeef") is None


def test_confirm_overrides_merge_into_extra(monkeypatch):
    manager, _engine, _store, hass = make_manager(monkeypatch)
    (entry,) = manager.queue_suggestions(
        [{"name": "PLA 白色", "category": "filament", "material": "PLA"}]
    )
    item = manager.confirm(entry["id"], overrides={"material": "PLA+", "full_weight_g": 1000})
    assert item["extra"] == {"material": "PLA+", "full_weight_g": 1000.0}
    hass.drain()


def test_confirm_failure_restores_suggestion(monkeypatch):
    manager, engine, _store, hass = make_manager(monkeypatch)
    (entry,) = manager.queue_suggestions([{"name": "M3×8", "category": "screw"}])

    def _boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(engine, "add_item", _boom)
    with pytest.raises(RuntimeError):
        manager.confirm(entry["id"])
    assert entry["id"] in manager.queue()  # restored for retry
    hass.drain()


def test_dismiss_single_and_all(monkeypatch):
    manager, _, _, hass = make_manager(monkeypatch)
    queued = manager.queue_suggestions(
        [{"name": f"条目{i}", "category": "other"} for i in range(3)]
    )
    assert manager.dismiss(queued[0]["id"]) == 1
    assert manager.dismiss(queued[0]["id"]) == 0  # already gone
    assert manager.dismiss("all") == 2
    assert manager.queue() == {}
    hass.drain()


# ----------------------------------------------------------------------
# Notification action routing
# ----------------------------------------------------------------------


def test_notification_actions_route(monkeypatch):
    manager, engine, _store, hass = make_manager(monkeypatch)
    (entry,) = manager.queue_suggestions([{"name": "PLA 白色", "category": "filament", "quantity": 900}])

    confirm_event = types.SimpleNamespace(
        data={"action": f"{ACTION_CONFIRM_PREFIX}{entry['id']}"}
    )
    manager._on_notification_action(confirm_event)
    assert "pla-bai-se" in engine.items
    assert entry["id"] not in manager.queue()

    (other,) = manager.queue_suggestions([{"name": "M4×16", "category": "screw"}])
    dismiss_event = types.SimpleNamespace(
        data={"action": f"{ACTION_DISMISS_PREFIX}{other['id']}"}
    )
    manager._on_notification_action(dismiss_event)
    assert other["id"] not in manager.queue()
    hass.drain()


def test_notification_action_ignores_unknown_tokens(monkeypatch):
    manager, engine, _store, hass = make_manager(monkeypatch)
    manager._on_notification_action(types.SimpleNamespace(data={"action": "other_action"}))
    manager._on_notification_action(
        types.SimpleNamespace(data={"action": f"{ACTION_CONFIRM_PREFIX}missing"})
    )
    assert engine.items == {}
    hass.drain()


# ----------------------------------------------------------------------
# Webhook handler
# ----------------------------------------------------------------------


def test_webhandler_rejects_unusable_payloads(monkeypatch):
    manager, _engine, _store, hass = make_manager(
        monkeypatch, options={"scan_enabled": True, "scan_api_key": "k"}
    )
    empty = asyncio.run(manager._handle_webhook(hass, manager.webhook_id, _json_request({})))
    assert empty.status == 200
    hass.drain()


def test_webhandler_requires_configuration(monkeypatch):
    manager, _engine, _store, hass = make_manager(monkeypatch, options={})
    raw = base64.b64encode(JPEG_SAMPLE).decode()
    resp = asyncio.run(manager._handle_webhook(hass, manager.webhook_id, _json_request({"image_base64": raw})))
    assert resp.status == 200
    hass.drain()


def test_webhandler_happy_path_queues_and_returns(monkeypatch):
    manager, _engine, _store, hass = make_manager(
        monkeypatch,
        options={"scan_enabled": True, "scan_api_key": "k", "scan_base_url": "http://llm", "scan_model": "v"},
    )
    raw = base64.b64encode(JPEG_SAMPLE).decode()

    class _Resp:
        def raise_for_status(self):
            pass

        async def json(self):
            return {
                "choices": [
                    {"message": {"content": json.dumps([{"name": "M3×8", "category": "screw", "quantity": 50}])}}
                ]
            }

    class _Session:
        def post(self, url, json=None, headers=None, timeout=None):
            class _Ctx:
                async def __aenter__(self):
                    return _Resp()

                async def __aexit__(self, *exc):
                    return False

            assert json["model"] == "v"
            assert url.endswith("/chat/completions")
            return _Ctx()

    monkeypatch.setattr(scan_mod, "async_get_clientsession", lambda hass: _Session())
    resp = asyncio.run(manager._handle_webhook(hass, manager.webhook_id, _json_request({"image_base64": raw})))
    payload = json.loads(resp.text)
    assert payload["ok"] is True
    assert payload["suggestions"][0]["name"] == "M3×8"
    assert len(manager.queue()) == 1
    hass.drain()


# ----------------------------------------------------------------------
# Lifecycle / notify guards
# ----------------------------------------------------------------------


def test_scan_lifecycle_registers_and_unregisters_webhook(monkeypatch):
    import homeassistant.components.webhook as webhook_stub

    manager, _engine, _store, _hass = make_manager(monkeypatch)
    manager.async_setup()
    assert manager.webhook_id in webhook_stub.registered
    manager.async_setup()  # re-setup (unregister-first) must not raise
    assert webhook_stub.registered.count(manager.webhook_id) == 2
    manager.async_teardown()
    assert ("unregister", manager.webhook_id) in webhook_stub.unregistered


def test_notify_without_device_is_silent(monkeypatch):
    manager, _engine, _store, hass = make_manager(monkeypatch)
    manager.queue_suggestions([{"name": "M3×8", "category": "screw"}])
    asyncio_run(manager._async_notify(list(manager.queue().values())))  # 无 device_id：不发也不炸
    hass.drain()
