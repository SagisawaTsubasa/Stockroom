"""Photo scan-to-stock (拍照扫描入库).

A phone shortcut POSTs a photo to the entry webhook; the manager asks a
configurable OpenAI-compatible vision model to extract item suggestions,
queues them as *pending* entries, and (when a companion-app device is
configured) pushes a notification whose action buttons confirm or dismiss
each suggestion. Confirming runs the regular ``add_item`` path.

Transport facts verified against HA 2026.1.3 core source:
- ``webhook.async_register`` endpoints need no auth; ``local_only=True``
  swallows non-LAN requests, which fits at-home stocktaking.
- Companion notification buttons come back as the
  ``mobile_app_notification_action`` event with ``event.data["action"]`` —
  no pre-registration required anywhere.
- ``mobile_app.util`` is imported lazily inside the notify path: importing
  the module at file scope would drag in PyNaCl (mobile_app's own
  requirement), breaking installs where the companion integration has never
  set up.

Photos are used for recognition only and never persisted.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientResponseError, ClientTimeout, web
from homeassistant.components import notify
from homeassistant.components.webhook import (
    async_generate_url,
    async_register,
    async_unregister,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError

from .const import (
    ACTION_CONFIRM_PREFIX,
    ACTION_DISMISS_PREFIX,
    CATEGORIES,
    CATEGORY_OTHER,
    CONF_SCAN_API_KEY,
    CONF_SCAN_BASE_URL,
    CONF_SCAN_DEVICE_ID,
    CONF_SCAN_ENABLED,
    CONF_SCAN_MODEL,
    DEFAULT_SCAN_BASE_URL,
    DEFAULT_SCAN_MODEL,
    DEFAULT_UNITS,
    EXTRA_COLOR,
    EXTRA_FULL_WEIGHT_G,
    EXTRA_MATERIAL,
    META_SCAN_PENDING,
    NOTIFICATION_ACTION_EVENT,
    QUANTITY_MAX,
    SCAN_MAX_IMAGE_BYTES,
    SCAN_MAX_QUEUE,
    SCAN_MAX_SUGGESTIONS,
    SCAN_PROMPT,
    SCAN_TIMEOUT_SECONDS,
    SCAN_WEBHOOK_NAME,
)
from .storage import StockroomStore, schedule_store_flush

if TYPE_CHECKING:
    from .inventory import InventoryEngine

_LOGGER = logging.getLogger(__name__)

# One companion notification per suggestion beyond this is spam; the webhook
# JSON response (and the scan_confirm service) cover the rest.
_MAX_NOTIFICATIONS = 5

def webhook_id_for_entry(entry_id: str) -> str:
    """The stable per-entry webhook id (entry ids are unguessable UUIDs)."""
    return f"stockroom-{entry_id}"


def _strip_fences(text: str) -> str:
    """Remove markdown code fences the model adds despite instructions."""
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _as_number(source: dict[str, Any], key: str) -> float:
    """Read a numeric field defensively; unknown, junk or non-finite means 0."""
    try:
        value = float(source.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    # The model can hallucinate Infinity/1e999 (poisons the store: orjson
    # serializes them as null) and negative thresholds (an item could then
    # never go low-stock) — clamp both to sane values.
    return max(0.0, value) if math.isfinite(value) else 0.0


def sniff_image_kind(image: bytes) -> str | None:
    """Return a MIME image subtype for the container, or None if unknown.

    ``RIFF`` alone would also match WAV/AVI, so WebP requires the magic size.
    """
    if image.startswith(b"\xff\xd8"):
        return "jpeg"
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image.startswith(b"GIF8"):
        return "gif"
    if len(image) >= 12 and image.startswith(b"RIFF") and image[8:12] == b"WEBP":
        return "webp"
    return None


def validate_image_bytes(image: bytes) -> None:
    """Common image validation shared by the webhook and the panel scan view.

    Raises ValueError with a caller-friendly message on empty content, the
    size cap or an unknown container.
    """
    if not image:
        raise ValueError("图片内容为空")
    if len(image) > SCAN_MAX_IMAGE_BYTES:
        raise ValueError(f"图片超过 {SCAN_MAX_IMAGE_BYTES // (1024 * 1024)}MB 上限")
    if sniff_image_kind(image) is None:
        raise ValueError("内容不是可识别的图片（支持 JPEG/PNG/GIF/WebP；iPhone 请先转 JPEG）")


async def extract_image_bytes(request: Any, hass: HomeAssistant | None = None) -> bytes:
    """Pull the photo out of a JSON-base64 or multipart upload.

    Raises ValueError with a caller-friendly message when there is no usable
    image, it exceeds the size limit, or it does not look like an image.
    """
    if request.content_type == "application/json":
        try:
            payload = await request.json()
        except (TypeError, ValueError) as err:
            raise ValueError("请求体不是合法 JSON") from err
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        raw = payload.get("image_base64") or payload.get("image")
        if not raw:
            raise ValueError("缺少 image_base64 字段")
        try:
            image = base64.b64decode(str(raw), validate=False)
        except (binascii.Error, ValueError) as err:
            raise ValueError("image_base64 不是合法 base64") from err
    else:
        try:
            async with asyncio.timeout(SCAN_TIMEOUT_SECONDS):
                data = dict(await request.post())
        except (TypeError, ValueError) as err:
            raise ValueError("无法解析上传内容") from err
        except TimeoutError as err:
            # asyncio.timeout raises TimeoutError (an alias of asyncio.
            # TimeoutError on 3.11+); uncaught it would bubble out of the
            # webhook handler as a bare 500.
            raise ValueError("上传超时，请重试或压缩图片后重试") from err
        field = data.get("image")
        if field is None:
            raise ValueError("multipart 上传缺少 image 字段")
        if hasattr(field, "file"):
            read = field.file.read
            image = await hass.async_add_executor_job(read) if hass else read()
        elif isinstance(field, bytes):
            image = field
        else:
            try:
                image = base64.b64decode(str(field), validate=False)
            except (binascii.Error, ValueError) as err:
                raise ValueError("image 字段既不是文件也不是 base64") from err
    validate_image_bytes(image)
    return image


def parse_suggestions(content: str) -> list[dict[str, Any]]:
    """Parse the model's reply into sanitized suggestion dicts.

    Defensive by design: fenced or fence-less JSON and an object wrapping
    the array under ``items`` are all handled, and individual junk elements
    are dropped. Structural failures (unparseable reply, no item array,
    nothing usable inside it) raise ``ValueError`` so the webhook answers
    ``ok:false`` — a broken recognition run must be distinguishable from
    "the photo genuinely contains nothing".
    """
    try:
        data = json.loads(_strip_fences(content))
    except (TypeError, ValueError) as err:
        _LOGGER.warning("识别结果不是合法 JSON")
        raise ValueError("识别结果不是合法 JSON") from err
    if isinstance(data, dict):
        data = data.get("items")
    if not isinstance(data, list):
        _LOGGER.warning("识别结果不含条目数组")
        # The caller contract is "parse failure ⇒ ValueError" —
        # _handle_webhook turns it into ok:false; TypeError would break it.
        raise ValueError("识别结果不含条目数组")  # noqa: TRY004

    suggestions: list[dict[str, Any]] = []
    for element in data[:SCAN_MAX_SUGGESTIONS]:
        if not isinstance(element, dict):
            continue
        name = str(element.get("name") or "").strip()
        if not name:
            continue
        category = str(element.get("category") or CATEGORY_OTHER)
        if category not in CATEGORIES:
            category = CATEGORY_OTHER

        quantity = _as_number(element, "quantity")
        low_threshold = _as_number(element, "low_threshold")
        full_weight_g = _as_number(element, "full_weight_g")
        unit = str(element.get("unit") or "").strip() or DEFAULT_UNITS.get(category, "个")
        suggestion: dict[str, Any] = {
            "name": name,
            "category": category,
            "quantity": quantity,
            "unit": unit,
            "location": str(element.get("location") or "").strip(),
        }
        # 0 (or a missing/negative key clamped to 0) means "unknown": leave
        # the key out so add_item falls back to the warehouse default —
        # writing 0 would silently pin the threshold and the item would
        # never go low-stock. Hallucinated magnitudes beyond QUANTITY_MAX
        # count as unknown too: the internal confirm→add_item path bypasses
        # the service schemas, so the cap has to be enforced here as well.
        if 0 < low_threshold <= QUANTITY_MAX:
            suggestion["low_threshold"] = low_threshold
        extra: dict[str, Any] = {}
        if element.get("material"):
            extra["material"] = str(element["material"]).strip()
        if element.get("color"):
            extra["color"] = str(element["color"]).strip()
        if 0 < full_weight_g <= QUANTITY_MAX:
            extra["full_weight_g"] = full_weight_g
        if extra:
            suggestion["extra"] = extra
        suggestions.append(suggestion)
    if data and not suggestions:
        # The model replied with an item array but nothing in it was usable:
        # a recognition failure — distinct from an explicit empty array,
        # which is a legitimate "nothing in the photo".
        _LOGGER.warning("识别结果没有可用条目")
        raise ValueError("识别结果没有可用条目")
    return suggestions


class ScanManager:
    """Per-entry scan pipeline: webhook → vision LLM → pending queue → notify."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: Any,
        engine: InventoryEngine,
        store: StockroomStore,
    ) -> None:
        """Initialize; webhook_id derives from the entry id (stable, secret)."""
        self.hass = hass
        self.entry = entry
        self.engine = engine
        self.store = store
        self.webhook_id = webhook_id_for_entry(entry.entry_id)
        self._listeners: list[Any] = []

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------

    def _opt(self, key: str, default: Any = None) -> Any:
        return self.entry.options.get(key, self.entry.data.get(key, default))

    @property
    def webhook_url(self) -> str:
        """Full webhook URL for the phone shortcut — always the LAN address.

        ``local_only=True`` swallows non-LAN requests, so advertising an
        external URL (the ``async_generate_url`` default) would point the
        shortcut at an endpoint that silently returns an empty 200. On
        SSL-only instances with no internal URL configured there is no LAN
        address at all — return an empty string instead of failing setup.
        """
        try:
            return async_generate_url(
                self.hass, self.webhook_id, allow_external=False, prefer_external=False
            )
        except NoURLAvailableError:
            _LOGGER.warning(
                "[%s] 找不到可用的内网地址（use_ssl 且未配 internal_url？），"
                "webhook 地址暂不可展示；拍照扫描功能本身不受影响",
                self.engine.warehouse_name,
            )
            return ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def async_setup(self) -> list[Any]:
        """Register the webhook and the notification-action listener.

        Unregister first: a failed setup leaves the old handler registered
        (HA never unloads a SETUP_ERROR entry), and a plain re-register would
        raise ``ValueError: Handler is already defined!`` forever after.
        """
        async_unregister(self.hass, self.webhook_id)
        async_register(
            self.hass,
            "stockroom",
            f"{SCAN_WEBHOOK_NAME} {self.engine.warehouse_name}",
            self.webhook_id,
            self._handle_webhook,
            local_only=True,
        )
        self._listeners.append(
            self.hass.bus.async_listen(NOTIFICATION_ACTION_EVENT, self._on_notification_action)
        )
        return self._listeners

    @callback
    def async_teardown(self) -> None:
        """Unregister the webhook and listeners."""
        async_unregister(self.hass, self.webhook_id)
        for unsub in self._listeners:
            unsub()
        self._listeners.clear()

    # ------------------------------------------------------------------
    # Webhook
    # ------------------------------------------------------------------

    async def _handle_webhook(
        self, hass: HomeAssistant, webhook_id: str, request: Any
    ) -> web.Response:
        """Receive a photo, recognize it, queue suggestions, notify."""
        try:
            image = await extract_image_bytes(request, hass)
        except ValueError as err:
            return web.json_response({"ok": False, "error": str(err)})
        if not self._opt(CONF_SCAN_ENABLED):
            return web.json_response({"ok": False, "error": "扫描功能未启用"})
        if not self._opt(CONF_SCAN_API_KEY):
            return web.json_response({"ok": False, "error": "未配置识别 API key"})

        try:
            suggestions = await self._recognize(image)
        except ValueError as err:
            # parse_suggestions raising is a recognition failure, not an
            # empty photo — tell the caller instead of ok:true + [].
            _LOGGER.warning(
                "[%s] 识别结果无法解析：%s", self.engine.warehouse_name, err
            )
            return web.json_response({"ok": False, "error": str(err)})
        except (TimeoutError, asyncio.TimeoutError):
            _LOGGER.warning("[%s] 识别请求超时", self.engine.warehouse_name)
            return web.json_response({"ok": False, "error": "识别超时"})
        except ClientResponseError as err:
            _LOGGER.warning(
                "[%s] 识别服务返回 %s（检查 API key、配额与模型名）",
                self.engine.warehouse_name, err.status,
            )
            return web.json_response(
                {"ok": False, "error": f"识别服务返回 {err.status}：请检查 API key、配额或模型名"}
            )
        except (ClientError, OSError) as err:
            _LOGGER.warning("[%s] 识别服务连接失败：%s", self.engine.warehouse_name, err)
            return web.json_response({"ok": False, "error": "识别服务连接失败"})
        except Exception:
            _LOGGER.exception("[%s] 识别失败", self.engine.warehouse_name)
            return web.json_response({"ok": False, "error": "识别失败，见日志"})

        queued = self.queue_suggestions(suggestions)
        _LOGGER.info(
            "[%s] 扫描识别到 %d/%d 个条目建议（队列 %d 项）",
            self.engine.warehouse_name, len(queued), len(suggestions), len(self.queue()),
        )
        if queued:
            self.hass.async_create_task(self._async_notify(queued))
        return web.json_response({"ok": True, "suggestions": queued})

    # ------------------------------------------------------------------
    # Vision LLM
    # ------------------------------------------------------------------

    async def _recognize(self, image: bytes) -> list[dict[str, Any]]:
        """Ask the configured vision model for item suggestions.

        Raises ValueError when the reply cannot be parsed into suggestions;
        the webhook handler answers ok:false in that case.
        """
        session = async_get_clientsession(self.hass)
        base_url = str(
            self._opt(CONF_SCAN_BASE_URL, DEFAULT_SCAN_BASE_URL)
        ).rstrip("/") or DEFAULT_SCAN_BASE_URL
        # Declare the actual container type: strict endpoints validate the
        # declared MIME, and a PNG announced as JPEG decodes to garbage.
        mime_kind = sniff_image_kind(image) or "jpeg"
        payload = {
            "model": self._opt(CONF_SCAN_MODEL, DEFAULT_SCAN_MODEL),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/{mime_kind};base64,{base64.b64encode(image).decode('ascii')}"
                            },
                        },
                        {"type": "text", "text": SCAN_PROMPT},
                    ],
                }
            ],
        }
        headers = {"Authorization": f"Bearer {self._opt(CONF_SCAN_API_KEY)}"}
        async with session.post(
            f"{base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=ClientTimeout(total=SCAN_TIMEOUT_SECONDS),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        content = data["choices"][0]["message"]["content"]
        return parse_suggestions(content)

    # ------------------------------------------------------------------
    # Pending queue
    # ------------------------------------------------------------------

    def queue(self) -> dict[str, dict[str, Any]]:
        """This entry's pending suggestions (sid -> suggestion + meta)."""
        return (
            self.store.get_meta(META_SCAN_PENDING).setdefault(self.entry.entry_id, {})
        )

    def queue_suggestions(self, suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Store suggestions with ids; evict oldest beyond the queue cap."""
        bucket = self.queue()
        now = datetime.now(UTC).isoformat()
        queued: list[dict[str, Any]] = []
        for suggestion in suggestions:
            sid = secrets.token_hex(8)
            entry = {"id": sid, "created_at": now, **suggestion}
            bucket[sid] = entry
            queued.append(entry)
        while len(bucket) > SCAN_MAX_QUEUE:
            oldest = min(bucket, key=lambda sid: bucket[sid].get("created_at", ""))
            del bucket[oldest]
        self.store.mark_dirty(self.entry.entry_id)
        return queued

    def confirm(
        self, sid: str, overrides: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Turn one suggestion into a real item via the regular add path."""
        suggestion = self.queue().pop(sid, None)
        if suggestion is None:
            return None
        fields = {key: value for key, value in suggestion.items() if key not in ("id", "created_at")}
        overrides = dict(overrides or {})
        extra = dict(fields.get("extra") or {})
        for key in (EXTRA_MATERIAL, EXTRA_COLOR, EXTRA_FULL_WEIGHT_G):
            if overrides.get(key) is not None:
                extra[key] = overrides.pop(key)
        fields.update({key: value for key, value in overrides.items() if value is not None})
        if extra:
            fields["extra"] = extra
        else:
            fields.pop("extra", None)
        try:
            item = self.engine.add_item(
                name=fields["name"],
                category=fields.get("category", CATEGORY_OTHER),
                quantity=fields.get("quantity", 0.0),
                unit=fields.get("unit"),
                low_threshold=fields.get("low_threshold"),
                location=fields.get("location"),
                extra=fields.get("extra"),
            )
        except Exception:
            # Put the suggestion back so the user can retry after fixing.
            self.queue()[sid] = suggestion
            raise
        self.store.mark_dirty(self.entry.entry_id)
        schedule_store_flush(self.hass, self.store)
        return item

    def dismiss(self, ref: str) -> int:
        """Drop one suggestion by id, or all with ``all``; returns dropped count."""
        bucket = self.queue()
        if ref == "all":
            count = len(bucket)
            bucket.clear()
        else:
            count = 1 if bucket.pop(ref, None) is not None else 0
        if count:
            self.store.mark_dirty(self.entry.entry_id)
        return count

    # ------------------------------------------------------------------
    # Notification + action routing
    # ------------------------------------------------------------------

    async def _async_notify(self, queued: list[dict[str, Any]]) -> None:
        """Push one action-button notification per suggestion (capped)."""
        device_id = self._opt(CONF_SCAN_DEVICE_ID)
        if not device_id:
            return
        try:
            # Lazy import: the module scope of mobile_app pulls PyNaCl, which
            # only exists when the companion integration has been set up.
            from homeassistant.components.mobile_app.util import (
                get_notify_service,
                webhook_id_from_device_id,
            )
        except ImportError:
            _LOGGER.warning(
                "[%s] mobile_app 集成不可用，跳过确认通知（可用 scan_confirm 服务或捷径返回值确认）",
                self.engine.warehouse_name,
            )
            return
        try:
            webhook_id = webhook_id_from_device_id(self.hass, device_id)
            service_name = get_notify_service(self.hass, webhook_id) if webhook_id else None
        except (KeyError, AttributeError):
            webhook_id = service_name = None
        if not webhook_id or not service_name:
            _LOGGER.warning(
                "[%s] 找不到手机 %s 对应的通知服务，确认请走 scan_confirm 服务或捷径返回值",
                self.engine.warehouse_name, device_id,
            )
            return
        for suggestion in queued[:_MAX_NOTIFICATIONS]:
            summary = f"{suggestion['name']}（{suggestion.get('quantity', 0):g} {suggestion.get('unit', '')}）"
            await self.hass.services.async_call(
                notify.DOMAIN,
                service_name,
                {
                    "title": "Stockroom 扫描入库",
                    "message": f"识别到：{summary}",
                    "target": webhook_id,
                    "data": {
                        "tag": f"stockroom-{suggestion['id']}",
                        "actions": [
                            {"action": f"{ACTION_CONFIRM_PREFIX}{suggestion['id']}", "title": "✓ 入库"},
                            {"action": f"{ACTION_DISMISS_PREFIX}{suggestion['id']}", "title": "✕ 忽略"},
                        ],
                    },
                },
                blocking=True,
            )

    @callback
    def _on_notification_action(self, event: Event) -> None:
        """Route companion notification button taps to confirm/dismiss."""
        action = str(event.data.get("action") or "")
        if action.startswith(ACTION_CONFIRM_PREFIX):
            sid = action[len(ACTION_CONFIRM_PREFIX) :]
            if sid not in self.queue():
                _LOGGER.debug(
                    "[%s] 确认按钮指向不存在/已处理的建议 %s", self.engine.warehouse_name, sid
                )
                return
            item = self.confirm(sid)
            if item is not None:
                _LOGGER.info(
                    "[%s] 通知确认入库 %s（%s %s）",
                    self.engine.warehouse_name, item["id"],
                    item["quantity"], item["unit"],
                )
        elif action.startswith(ACTION_DISMISS_PREFIX):
            sid = action[len(ACTION_DISMISS_PREFIX) :]
            if self.dismiss(sid):
                _LOGGER.info("[%s] 通知忽略建议 %s", self.engine.warehouse_name, sid)
            else:
                _LOGGER.debug(
                    "[%s] 忽略按钮指向不存在/已处理的建议 %s", self.engine.warehouse_name, sid
                )
