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

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


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
    raw = b"\xff\xd8fakejpeg"
    req = _json_request({"image_base64": base64.b64encode(raw).decode()})
    assert asyncio_run(extract_image_bytes(req)) == raw


def asyncio_run(coro):
    return asyncio.run(coro)


def test_extract_image_errors():
    with pytest.raises(ValueError):
        asyncio_run(extract_image_bytes(_json_request({})))
    with pytest.raises(ValueError):
        asyncio_run(extract_image_bytes(_json_request({"image_base64": "!!!!not-base64!!!"})))
    with pytest.raises(ValueError):
        asyncio_run(extract_image_bytes(_json_request({"image_base64": ""})))
    with pytest.raises(ValueError):
        asyncio_run(extract_image_bytes(_multipart_request({})))


def test_extract_image_multipart_file_field():
    raw = b"binary-image-bytes"
    field = types.SimpleNamespace(file=io.BytesIO(raw))
    req = _multipart_request({"image": field})
    assert asyncio_run(extract_image_bytes(req)) == raw


def test_extract_image_multipart_base64_field():
    raw = b"raw-bytes-field"
    req = _multipart_request({"image": base64.b64encode(raw).decode()})
    assert asyncio_run(extract_image_bytes(req)) == raw


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
    manager, _engine, _store, hass = make_manager(monkeypatch)
    manager._on_notification_action(types.SimpleNamespace(data={"action": "other_action"}))
    manager._on_notification_action(
        types.SimpleNamespace(data={"action": f"{ACTION_CONFIRM_PREFIX}missing"})
    )
    assert manager.engine.items == {}
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
    raw = base64.b64encode(b"img").decode()
    resp = asyncio.run(manager._handle_webhook(hass, manager.webhook_id, _json_request({"image_base64": raw})))
    assert resp.status == 200
    hass.drain()


def test_webhandler_happy_path_queues_and_returns(monkeypatch):
    manager, _engine, _store, hass = make_manager(
        monkeypatch,
        options={"scan_enabled": True, "scan_api_key": "k", "scan_base_url": "http://llm", "scan_model": "v"},
    )
    raw = base64.b64encode(b"img").decode()

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
        def __init__(self, *a, **k):
            pass

        def post(self, url, json=None, headers=None):
            class _Ctx:
                async def __aenter__(self):
                    return _Resp()

                async def __aexit__(self, *exc):
                    return False

            assert json["model"] == "v"
            assert url.endswith("/chat/completions")
            return _Ctx()

        @property
        def closed(self):
            return False

        async def close(self):
            pass

    monkeypatch.setattr(scan_mod, "ClientSession", _Session)
    resp = asyncio.run(manager._handle_webhook(hass, manager.webhook_id, _json_request({"image_base64": raw})))
    payload = json.loads(resp.text)
    assert payload["ok"] is True
    assert payload["suggestions"][0]["name"] == "M3×8"
    assert len(manager.queue()) == 1
    hass.drain()
