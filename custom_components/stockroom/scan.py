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
import hashlib
import hmac
import json
import logging
import math
import re
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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError

from .const import (
    ACTION_CONFIRM_PREFIX,
    ACTION_DISMISS_PREFIX,
    CATEGORIES,
    CATEGORY_FILAMENT,
    CATEGORY_OTHER,
    CONF_OCR_SECRET_ID,
    CONF_OCR_SECRET_KEY,
    CONF_SCAN_API_KEY,
    CONF_SCAN_BASE_URL,
    CONF_SCAN_DEVICE_ID,
    CONF_SCAN_ENABLED,
    CONF_SCAN_ENGINE,
    CONF_SCAN_LOCAL_URL,
    CONF_SCAN_MODEL,
    DEFAULT_SCAN_BASE_URL,
    DEFAULT_SCAN_ENGINE,
    DEFAULT_SCAN_MODEL,
    DEFAULT_UNITS,
    EXTRA_COLOR,
    EXTRA_FULL_WEIGHT_G,
    EXTRA_MATERIAL,
    FULL_WEIGHT_MAX,
    META_SCAN_PENDING,
    NOTIFICATION_ACTION_EVENT,
    QUANTITY_MAX,
    SCAN_ENGINE_LOCAL,
    SCAN_ENGINE_OCR,
    SCAN_ENGINES,
    SCAN_MAX_IMAGE_BYTES,
    SCAN_MAX_QUEUE,
    SCAN_MAX_SUGGESTIONS,
    SCAN_PROMPT,
    SCAN_TIMEOUT_SECONDS,
    SCAN_WEBHOOK_NAME,
    TC_OCR_ACTION,
    TC_OCR_HOST,
    TC_OCR_SERVICE,
    TC_OCR_URL,
    TC_OCR_VERSION,
)
from .storage import StockroomStore, schedule_store_flush

if TYPE_CHECKING:
    from .inventory import InventoryEngine

_LOGGER = logging.getLogger(__name__)

# One companion notification per suggestion beyond this is spam; the webhook
# JSON response (and the scan_confirm service) cover the rest.
_MAX_NOTIFICATIONS = 5

# 「缺键」与「值为 None」的分离哨兵（SR-F-163）：队列公开可写+落盘持久
# 化（META_SCAN_PENDING 跨版本/手工改库可达），桶里可存 null——pop 的
# default 用 None 会把「无此 sid」与「None 条目」混为一谈（None 条目被
# 静默当不存在：confirm 删条目不报错、dismiss 计 0 不落盘）。
_MISSING = object()


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
        # full_weight_g has its own tighter cap (service-schema parity,
        # SR-F-101): the scan-confirm path bypasses the service schemas.
        if 0 < full_weight_g <= FULL_WEIGHT_MAX:
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


# ----------------------------------------------------------------------
# OCR engine (腾讯云通用印刷体识别, TC3-HMAC-SHA256 signed, no SDK)
# ----------------------------------------------------------------------

_TC_OCR_MAX_IMAGE = 7_500_000  # raw bytes; base64 inflates ×4/3 → <10 MB
_OCR_MATERIALS = (
    "PLA", "PETG", "ABS", "ASA", "TPU", "PC", "PVA", "HIPS", "NYLON", "PA",
)
_OCR_COLOR_WORDS = {
    "白色": "#FFFFFF", "黑色": "#000000", "红色": "#E22C2C",
    "橙色": "#FF8000", "黄色": "#FFDD00", "绿色": "#00B050",
    "青色": "#00BCD4", "蓝色": "#2C54DC", "紫色": "#8B00FF",
    "粉色": "#FF80C0", "棕色": "#8B4513", "灰色": "#808080",
    "金色": "#D4AF37", "银色": "#C0C0C0", "透明": "#E8F4F8",
}
# 质量单位词换算表（基准=filament 默认单位 g；DEFAULT_UNITS 全值域实证
# =g/颗/个）：kg/千克/公斤×1000、斤×500、g/克×1（SR-F-129；克重提取与
# 数量族核算共用，下方正则交替与此表同源维护）
_OCR_UNIT_MASS_SCALE = {
    "kg": 1000.0, "千克": 1000.0, "公斤": 1000.0,
    "g": 1.0, "克": 1.0, "斤": 500.0,
}
# 计数族互认词（颗/个=DEFAULT_UNITS 计数值；只/件/枚=标签常见计量词）
# 与包装/计量其它族（卷/包/盒/米，SR-F-124 钉过卷）：非 g 组照填、对
# g 组冲突（SR-F-129）
_OCR_UNIT_COUNT_WORDS = ("颗", "个", "只", "件", "枚")
_OCR_UNIT_PACKAGE_WORDS = ("卷", "包", "盒", "米")
# 克重：中文质量词入交替（「净重 1公斤」要能提 full_weight_g 且同材质
# 拆组判据随之正确，SR-F-129）；数字+单位后不能跟 ASCII 字母数字（挡
# PLA2 类粘连）；斜杠守卫收窄到长度/密度单位（SR-F-143）：只排除斜杠
# 后以 cm/mm/m 开头的密度/线密度形态（g/cm³、kg/m³、g/m² 及上标/全角/
# ASCII 数字幂次变体全挡，SR-F-072/081），「净重 1kg/卷」「250g/包」
# 按件规格是合法克重不再被无差别挡掉（修前静默漏提无日志；分母全为
# 中文故无漏挡面）。守卫尾不用 \b：Python \b 把 unicode 数字（含上标
# ³²）与 CJK 全当词字符，「cm³」的 m 后 \b 不成立、密度反格反漏挡
# （本机实跑现录，SR-F-132 同族教训）；\b 换 (?![A-Za-z0-9])：Python
# \b 把 CJK 当词字符，「1kg装」被挡致满卷克重漏提+名称残留（SR-F-132）
_OCR_WEIGHT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(千克|公斤|kg|克|斤|g)(?![A-Za-z0-9])"
    r"(?!\s*[/\uFF0F]\s*(?:cm|mm|m))",
    re.IGNORECASE,
)
# 兼容清单行的「适用(于)/兼容/支持/适配」开头特征——仅此形态（或一行列多个
# 材质词）才不作分组锚；营销语「PLA 支持高速打印」仍是合法锚（ocr-ux-R2 #2）
_OCR_COMPAT_RE = re.compile(r"^\s*(适用|兼容|支持|适配)")
# 商家标签关键字字段行（真实标签实证 2026-11-09：全/半角冒号，一行可含
# 多字段——数量必须行内锚定，「排序：25-1 数量：100」只抓 100；名称取
# 冒号后整段，右边界用「字段词+冒号」前瞻截断（OCR 合并行时名称不粘
# 「库位：…」，边界词只截断不解析，SR-F-123）。取证：
# debug/real-device-20261109/real-lines-20261109.md）
_OCR_FIELD_WORDS = "库位|排序|数量|公司|电话|地址|执行标准|生产日期"
_OCR_NAME_RE = re.compile(
    rf"名称[:：]\s*(.+?)(?=\s+(?:{_OCR_FIELD_WORDS})[:：]|$)"
)
# 克重字段词（净重/毛重/重量）单一来源（SR-F-144）：strip 剥取、数量哨
# 兵排除表、净重优先前缀、拆组判据四消费点同源拼接/引用，禁手抄第四份
# （对账闸门 test_ocr_weight_field_wordlist_mechanical_reconciliation 钉）。
# 优先表=显式差集（毛重不入优先：毛重是包装重，净重优先语义 SR-F-137）
_OCR_WEIGHT_FIELDS = ("净重", "毛重", "重量")
_OCR_WEIGHT_PRIORITY_FIELDS = ("净重", "重量")
# 数量哨兵专用排除表（SR-F-142）：名称边界表不含克重字段词——「数量：1
# 净重：1kg」的「净」会被哨兵当族外计量词捕获（数量置 0+遮蔽吞净重首
# 字），数量哨兵的排除表需含克重字段词族；名称边界表保持原表不动
# （克重字段段由 strip RE 单独剥，名称边界不截）。SR-F-162 补「名称」：
# 合行「数量：100 名称：X」修前哨兵在「名」处前瞻通过当族外计量词捕获
# （100 静默置 0，SR-F-142 同形换最常用字段词）——「名称」只进本拼接
# 层，_OCR_FIELD_WORDS 本体不动（它同时喂 _OCR_NAME_RE 右边界，截断
# 语义不变）；对账闸门以「名称」字面量独立对账（删词必红，改词=决策点）
_OCR_QTY_FIELD_WORDS = _OCR_FIELD_WORDS + "|名称|" + "|".join(_OCR_WEIGHT_FIELDS)
# 数量词表=三族全集（质量换算表+计数族+其它族，SR-F-129 扩中文词）；
# 尾随哨兵组捕获数字后紧跟的族外词首字符（unicode 字母）——词表外计量
# 词（如「箱」）按未知 0 诚实缺省，不再裸数字误填（SR-F-129）。哨兵是
# 「字段词排除前瞻+字母」的守卫式可选组：排除判据放哨兵组内侧，数字
# 捕获组先贪婪吃完整个数字串、可选单位组照常匹配族内词，二者均不因
# 前瞻失败被拖入回溯（位次错误的强制前瞻曾把「100库位：」回溯成 10、
# 把「100kg库位：」的单位丢成哨兵 0——SR-F-133，症状格钉在
# test_parse_ocr_text_qty_unit_family_matrix）。捕获词与组默认单位的族
# 核算见 _ocr_suggestion（SR-F-124/129）
_OCR_QTY_RE = re.compile(
    rf"数量[:：]\s*(\d+(?:\.\d+)?)\s*"
    r"(千克|公斤|kg|克|斤|g|颗|个|只|件|枚|卷|包|盒|米)?"
    rf"(?:(?!(?:{_OCR_QTY_FIELD_WORDS})[:：])([^\W\d_]))?",
    re.IGNORECASE,
)
# 克重 strip 分母白名单（SR-F-148）：按件分母超集=数量其它族词表+盘/袋/轴
# ——只喂 strip 可选段，不动 _OCR_UNIT_PACKAGE_WORDS 本体（它联动
# _OCR_QTY_RE 单位交替与数量族核算，「数量：1卷」等既有数量语义保持；
# 盘/袋/轴仅进 strip 不进数量族，「数量：1盘」走族外哨兵按未知 0，
# SR-F-151 注释对齐；对账闸门
# test_ocr_weight_denom_wordlist_mechanical_reconciliation 钉）
_OCR_WEIGHT_DENOM_WORDS = _OCR_UNIT_PACKAGE_WORDS + ("盘", "袋", "轴")
# 名称剥取用（回退路径+名称关键字路径同口径，SR-F-147）：克重字段段（可选
# 「净重/毛重/重量」字段词+可选冒号，字段词缺省时裸克重 token 照剥；
# 「净重：1kg」整段剥净 SR-F-126）+按件分母可选段（「净重 1kg/卷」随
# SR-F-143 守卫收窄入合法克重，整段含「/卷」「/盘」剥净不残留斜杠碎片，
# 词表=上方按件超集 SR-F-148）+包装后缀「装」（「250g装/1kg装」连装剥净
# 不残留，SR-F-132）。复用 _OCR_WEIGHT_RE 的 pattern 继承中文质量词与
# 密度排除（SR-F-129/072/081）；材质/颜色是商品名的自然组成不剥，克重是
# 纯规格数值才剥（2026-11-09 用户返修定案）
_OCR_WEIGHT_STRIP_RE = re.compile(
    r"(?:(?:" + "|".join(_OCR_WEIGHT_FIELDS) + r")[:：]?)?\s*"
    + _OCR_WEIGHT_RE.pattern
    + r"(?:\s*[/／]\s*(?:" + "|".join(_OCR_WEIGHT_DENOM_WORDS) + r"))?装?",
    re.IGNORECASE,
)
# 字段行判别（两层关键字识别第一层，SR-F-155/156 用户拍板重做
# 2026-11-10）：含任意「字段词[:：]」的行=商家标签字段行，豁免清单过滤
# 整行作证。字段词全集=_OCR_QTY_FIELD_WORDS 同源引用（名称关键字∪名称
# 边界表∪克重字段词，SR-F-162 起「名称」在常量拼接层，此处不再重复
# 字面量防两处漂移；_OCR_NAME_RE 的捕获/边界语义仍不共用本常量）。冒号
# 必须：克重字段词带可选冒号会把「净重 1kg」裸克重行也豁免，「适用于…
# 白色 净重 1kg」营销行重新入证（SR-F-158 格红）；名称/数量/库位等真实
# 字段行恒带冒号（真实标签取证 2026-11-09）
_OCR_FIELD_LINE_RE = re.compile(rf"(?:{_OCR_QTY_FIELD_WORDS})[:：]")


def _material_pattern(material: str) -> str:
    """ASCII-boundary pattern：中文相邻可命中（Python \\b 把 CJK 当词字符，
    「白色PLA」无边框会静默退回单条，ocr-ux-R1 #5），PLA2/PA12 仍被挡。"""
    return rf"(?<![A-Za-z0-9]){re.escape(material)}(?![A-Za-z0-9])"


def _is_compat_listing(line: str) -> bool:
    """兼容清单行判定：兼容关键词开头，或一行列出 ≥2 个不同材质词。"""
    if _OCR_COMPAT_RE.search(line):
        return True
    words = {
        m
        for m in _OCR_MATERIALS
        if re.search(_material_pattern(m), line, re.IGNORECASE)
    }
    return len(words) >= 2


def _tc3_canonical_request(host: str, action: str, hashed_payload: str) -> str:
    """Canonical request for a Tencent Cloud API POST (TC3 doc 213/30654).

    content-type carries "; charset=utf-8" — the signed header value must
    match the sent header exactly or the server-side signature check fails.
    """
    canonical_headers = (
        f"content-type:application/json; charset=utf-8\nhost:{host}\n"
        f"x-tc-action:{action.lower()}\n"
    )
    signed_headers = "content-type;host;x-tc-action"
    return f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"


def _tc3_headers(
    secret_id: str, secret_key: str, payload: str, region: str | None = None
) -> dict[str, str]:
    """TC3-HMAC-SHA256 auth headers for a Tencent Cloud API POST.

    Algorithm per cloud.tencent.com/document/api/213/30654: canonical
    request (POST, /, empty query, lowercased sorted headers) → string to
    sign (`Date/ocr/tc3_request`) → derived signing key chain.
    """
    timestamp = int(datetime.now(UTC).timestamp())
    date = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")
    hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canonical_request = _tc3_canonical_request(
        TC_OCR_HOST, TC_OCR_ACTION, hashed_payload
    )
    credential_scope = f"{date}/{TC_OCR_SERVICE}/tc3_request"
    # StringToSign's last line is the hash of the CANONICAL REQUEST (which
    # itself ends with the payload hash) — not the payload hash directly.
    hashed_canonical_request = hashlib.sha256(
        canonical_request.encode("utf-8")
    ).hexdigest()
    signed_headers = "content-type;host;x-tc-action"
    string_to_sign = (
        f"TC3-HMAC-SHA256\n{timestamp}\n{credential_scope}\n"
        f"{hashed_canonical_request}"
    )

    def _hmac(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

    k_signing = _hmac(
        _hmac(
            _hmac(("TC3" + secret_key).encode("utf-8"), date), TC_OCR_SERVICE
        ),
        "tc3_request",
    )
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    headers = {
        "Authorization": (
            f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "X-TC-Action": TC_OCR_ACTION,
        "X-TC-Version": TC_OCR_VERSION,
        "X-TC-Timestamp": str(timestamp),
        "Content-Type": "application/json; charset=utf-8",
    }
    if region:
        headers["X-TC-Region"] = region
    return headers


def _ocr_material_word(line: str) -> str | None:
    """The material keyword in one line (ASCII-boundary), or None."""
    return next(
        (m for m in _OCR_MATERIALS if re.search(_material_pattern(m), line, re.IGNORECASE)),
        None,
    )


def _ocr_suggestion(
    cleaned: list[str], material_hint: str | None = None
) -> dict[str, Any]:
    """Build one suggestion from one group of lines (single-item rules).

    ``material_hint`` is the group's anchor material — compatibility lines
    listing other materials must not override it (ocr-ux-R1 #4). Honest
    scope: keyword-prefixed fields (名称/数量，hardware-store labels),
    material keywords, spool weight and Chinese color words are
    extractable; anything requiring understanding of the label stays empty
    for the user to fill at confirm time. Output matches
    parse_suggestions' suggestion shape so the rest of the pipeline is
    engine-agnostic; ``ocr_lines`` carries the group's raw OCR lines for
    the pending card to show what was actually recognized.
    """
    # 两层关键字识别（SR-F-155/156 用户拍板重做 2026-11-10，整体替换 R7
    # 的 evidence 五处全过滤机制不留双机制）。第一层行分类：字段行（含
    # 「字段词[:：]」，_OCR_FIELD_LINE_RE）豁免清单过滤整行作证；自由行
    # 按清单过滤——非清单自由行作证，清单行（适用/兼容开头或一行 ≥2 材
    # 质词）不作证据。第二层按消费点组装扫描输入：材质/颜色/克重吃
    # scan_lines（保持 cleaned 顺序）；名称关键字/数量/ocr_lines 原文展示
    # 仍走 cleaned（字段行非清单形态、数量与展示语义独立，§1 决策 2）；
    # 名称回退候选同 scan_lines，空则回落 cleaned 全行保名称（全兼容页
    # 名称非空，材质/分类/extra 不回落——SR-F-155 修法收口，营销行不得
    # 定义材质/分类）。重做理由：R7「五处全过滤+or cleaned 回落」在 R8
    # 被证边界震荡——回落把营销行重新喂回全部扫描（SR-F-155 营销页被判
    # filament）、清单判据不区分字段行误杀「名称：PLA/PETG 双色线材」
    # 致克重/材质静默丢失（SR-F-156 格 A）；字段行豁免后格 A 恢复 R7 前
    # 行为，「支持高速打印的 PLA 线材」类前缀商品行仍被排除=既有口径
    # 显式钉格（不在本批扩）
    scan_lines = [
        line
        for line in cleaned
        if _OCR_FIELD_LINE_RE.search(line) or not _is_compat_listing(line)
    ]
    text_all = " ".join(scan_lines)
    material = material_hint or next(
        (m for m in _OCR_MATERIALS if re.search(_material_pattern(m), text_all, re.IGNORECASE)),
        None,
    )
    # 克重取值字段感知（SR-F-137）：先逐行遮蔽数量字段段（「数量：500g」
    # 是库存量，不作满卷克重），再给净重/重量字段词优先权——优先前缀在
    # 未遮蔽原文上搜（SR-F-142）：前缀是强约束，数量字段不会误命中
    # 「净重/重量」字样；遮蔽文本上搜会在「数量：1 净重：1kg」处吞掉
    # 净重首字（数量哨兵把「净」当族外计量词捕获，行残成「重：1kg」）
    # 致优先失效、回落静默取毛重。遮蔽文本只做无前缀回落（数量段不作
    # 满卷克重的语义不变）。拆组判据（parse_ocr_text 的
    # seg_has_weight/anchor_self_weight）共用克重正则，数量行仍参与「独
    # 立克重行」判据：拆条逻辑用户已定决策不动，现状快照钉在
    # test_parse_ocr_text_qty_field_masked_from_full_weight
    weight_text = " ".join(_OCR_QTY_RE.sub(" ", line) for line in scan_lines)
    prefixed = re.search(
        r"(?:" + "|".join(_OCR_WEIGHT_PRIORITY_FIELDS) + r")[:：]?\s*"
        + _OCR_WEIGHT_RE.pattern,
        text_all,
        re.IGNORECASE,
    )
    weight_match = prefixed or _OCR_WEIGHT_RE.search(weight_text)
    full_weight_g = None
    if weight_match:
        value = float(weight_match.group(1))
        # 换算表直索引（正则交替=表键同源，漂移即 KeyError 炸测试，
        # SR-F-140 机械对账闸门钉住）
        full_weight_g = value * _OCR_UNIT_MASS_SCALE[weight_match.group(2).lower()]
        full_weight_g = min(full_weight_g, FULL_WEIGHT_MAX)
    color_word = next(
        (key for line in scan_lines for key in _OCR_COLOR_WORDS if key in line),
        None,
    )
    # 「名称：」关键字行优先（冒号后整段即名称，真实商家标签实证）；
    # 无关键字回退现行选取，只剥克重 token——材质/颜色是商品名的自然
    # 组成不剥，克重是纯规格数值才剥（2026-11-09 用户返修定案）
    name_keyword = next(
        (m.group(1).strip() for line in cleaned if (m := _OCR_NAME_RE.search(line))),
        None,
    )
    if name_keyword:
        # 名称关键字行同过克重 strip（SR-F-147）：「名称：X 净重：80g」
        # 合行名不残留克重段，与回退路径同口径（材质/颜色保留不剥，
        # 2026-11-09 返修定案）；剥空保原名（名称不得为空）
        name_line = (
            re.sub(r"\s+", " ", _OCR_WEIGHT_STRIP_RE.sub("", name_keyword)).strip(" ·-—")
            or name_keyword
        )
    else:
        # 名称行与材质同口径（锚材质优先），避免子串误命中（SR-F-073）；
        # 候选面走 scan_lines（两层分类：清单行不抢名称），整页只有清单
        # 行时回落 cleaned 全行保名称（SR-F-155：仅名称回落，材质/分类
        # 不回落）
        name_pool = scan_lines or cleaned
        name_line = next(
            (
                line
                for line in name_pool
                if material and re.search(_material_pattern(material), line, re.IGNORECASE)
            ),
            max(name_pool, key=len),
        )
        fallback_line = name_line
        # 剥空（整行只有克重）则保原行，名称不得为空
        name_line = (
            re.sub(r"\s+", " ", _OCR_WEIGHT_STRIP_RE.sub("", name_line)).strip(" ·-—")
            or fallback_line
        )
    # 「数量：」行内锚定提取（一行可含多字段，「排序：25-1 数量：100」
    # 只抓 100）；越界按未知 0 处理（SR-F-012 同型闸，引擎层
    # add_item/_round_quantity 对确认入库的值另有兜底钳制）。
    # 单位族核算（SR-F-124/129）：数量自带单位词时按族对组默认单位
    # （确认入库即落 DEFAULT_UNITS[category]）核对——质量族对 g 组按换
    # 算表折算（kg/千克/公斤×1000、斤×500）、对计数组冲突；计数族互认
    # （「100 颗」对个组不再误判清零）、其它族非 g 组照填；族外词（哨
    # 兵捕获）一律按未知 0 诚实缺省，确认时手补，禁静默把 5 公斤填成
    # 5 g
    category = CATEGORY_FILAMENT if material else CATEGORY_OTHER
    is_g_group = DEFAULT_UNITS.get(category, "个") == "g"
    qty_match = next(
        (m for line in cleaned if (m := _OCR_QTY_RE.search(line))),
        None,
    )
    quantity = 0.0
    # unit 契约补齐（SR-F-157）：docstring 声称与 parse_suggestions 的
    # suggestion 形状一致而缺 unit 键——下游面板待确认卡/编辑框、通知
    # summary、confirm→add_item 三处都读它（修前面板单位空白、通知
    # 「（100 ）」、「数量：2盒」入库 2 个）。g 组恒用分类默认 g（质量族
    # 折算后基准不变、冲突词数量已置 0）；非 g 组捕获计数族/包装族单位
    # 词时随 quantity 照填捕获词，质量族/族外词/裸数字落分类默认
    unit = DEFAULT_UNITS.get(category, "个")
    if qty_match:
        quantity = float(qty_match.group(1))
        word = (qty_match.group(2) or "").lower()
        if word:
            scale = _OCR_UNIT_MASS_SCALE.get(word)
            if scale is not None:
                quantity = quantity * scale if is_g_group else 0.0
            elif word in _OCR_UNIT_COUNT_WORDS or word in _OCR_UNIT_PACKAGE_WORDS:
                if not is_g_group:
                    unit = word
                quantity = 0.0 if is_g_group else quantity
            else:
                quantity = 0.0
        elif qty_match.group(3):
            quantity = 0.0
    if not 0 < quantity <= QUANTITY_MAX:
        quantity = 0.0
    extra: dict[str, Any] = {}
    if material:
        extra[EXTRA_MATERIAL] = material
    if color_word:
        extra[EXTRA_COLOR] = _OCR_COLOR_WORDS[color_word]
    if full_weight_g:
        extra[EXTRA_FULL_WEIGHT_G] = full_weight_g
    suggestion: dict[str, Any] = {
        "name": name_line[:60],
        "category": category,
        "quantity": quantity,
        "unit": unit,
        "location": "",
        # 识别原文随行存储（面板待确认卡展示，防「以为只识别出地址」）；
        # additive 键：旧队列数据无此键，面板按缺省不展示
        "ocr_lines": list(cleaned),
    }
    if extra:
        suggestion["extra"] = extra
    return suggestion


def parse_ocr_text(lines: list[str]) -> list[dict[str, Any]]:
    """Turn OCR text lines into item suggestions via keyword rules.

    Multi-item pages: each material-keyword line starts a new group (a
    spool/label), lines before the first anchor join the first group, and
    per-group extraction follows the single-item rules. A trailing anchor
    group WITHOUT its own weight merges into the previous group — labels
    repeat "颜色 材质" on a closing line as attribute continuation, not as
    a new spool; an independent weight line is what marks a new item. A
    page without any material anchor falls back to one suggestion over
    all lines.
    """
    # 非 str 行先 str() 归一再剥空白（SR-F-154：过滤器 str() 判真、原对象
    # .strip() 会 AttributeError/TypeError——公开函数对持久化队列跨版本/
    # 手工数据的防御面；生产引擎链路已保证 str，此处补契约缺口）
    cleaned = [text for line in lines if (text := str(line).strip())]
    if not cleaned:
        return []
    anchors = [
        (i, m)
        for i, line in enumerate(cleaned)
        if (m := _ocr_material_word(line)) and not _is_compat_listing(line)
    ]
    if not anchors:
        return [_ocr_suggestion(cleaned)]
    bounds = [a for a, _m in anchors[1:]] + [len(cleaned)]
    segments = [
        cleaned[0 if i == 0 else anchor : bounds[i]]
        for i, (anchor, _m) in enumerate(anchors)
    ]
    groups = [segments[0]]
    hints = [anchors[0][1]]
    prev_has_weight = _OCR_WEIGHT_RE.search(" ".join(groups[0])) is not None
    for i, segment in enumerate(segments[1:], 1):
        seg_has_weight = _OCR_WEIGHT_RE.search(" ".join(segment)) is not None
        anchor_line = cleaned[anchors[i][0]]
        anchor_self_weight = (
            _OCR_WEIGHT_RE.search(anchor_line) is not None
            or any(w in anchor_line for w in _OCR_WEIGHT_PRIORITY_FIELDS)
        )
        # 开新项要求锚段有独立克重；但「复述行在净重行之前」的同材质卷
        # （锚行本身无克重、前组尚无克重）仍是同一卷的属性续行（ocr-ux-R2 #1）
        starts_new = seg_has_weight and not (
            anchors[i][1] == hints[-1]
            and not prev_has_weight
            and not anchor_self_weight
        )
        if starts_new:
            groups.append(segment)
            hints.append(anchors[i][1])
        else:
            groups[-1].extend(segment)
        prev_has_weight = prev_has_weight or seg_has_weight
    return [_ocr_suggestion(group, hint) for group, hint in zip(groups, hints)]


async def _recognize_ocr(
    manager: ScanManager, image: bytes
) -> list[dict[str, Any]]:
    """Recognize via Tencent Cloud GeneralBasicOCR (在线免费 OCR)."""
    secret_id = str(manager._opt(CONF_OCR_SECRET_ID) or "").strip()
    secret_key = str(manager._opt(CONF_OCR_SECRET_KEY) or "").strip()
    if not secret_id or not secret_key:
        raise ValueError("未配置腾讯云 OCR 的 SecretId/SecretKey（请在集成选项里填写）")
    if len(image) > _TC_OCR_MAX_IMAGE:
        raise ValueError(
            f"图片超过腾讯云 OCR 约 7MB 的原始大小限制（{len(image) // (1024 * 1024)}MB），"
            "请压缩后重试"
        )
    payload = json.dumps({"ImageBase64": base64.b64encode(image).decode("ascii")})
    headers = _tc3_headers(secret_id, secret_key, payload)
    session = async_get_clientsession(manager.hass)
    async with session.post(
        TC_OCR_URL,
        data=payload.encode("utf-8"),
        headers=headers,
        timeout=ClientTimeout(total=SCAN_TIMEOUT_SECONDS),
    ) as resp:
        # 先读 body 再判状态（SR-F-095，同 SR-F-084 形状）：HTTP 错误体里的
        # Response.Error.Message 直达用户，而不是只剩一个状态码。
        raw = await resp.text()
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        if resp.status >= 400:
            detail = ""
            if isinstance(data, dict):
                # Response 非 dict（反代返回非信封错误体）同守不崩
                # （SR-F-130），detail 尽力取 Error.Message/message
                envelope = data.get("Response")
                error = envelope.get("Error") if isinstance(envelope, dict) else None
                if isinstance(error, dict):
                    detail = str(error.get("Message") or error.get("Code") or "")
                if not detail:
                    detail = str(data.get("message") or "")
            _LOGGER.warning("腾讯 OCR 返回 HTTP %s：%s", resp.status, detail)
            raise ValueError(
                f"腾讯 OCR 服务返回 {resp.status}：{detail or manager.service_error_hint()}"
            )
        if not isinstance(data, dict):
            # 200 + 非 JSON（反代错误页/网关异常）按服务故障响，不得落进
            # 「未识别到有效文字」把故障伪装成空照片（SR-F-127，错误必须响）；
            # TRY004 的 noqa 需贴在 raise 判定行（同 parse_suggestions 先例）
            _LOGGER.warning("腾讯 OCR 返回 200 但回复不是 JSON（服务/反代异常）")
            raise ValueError("腾讯 OCR 服务返回了非 JSON 回复（服务/反代异常）")  # noqa: TRY004
    # 200 但缺 Response 信封（如反代错误体 {"error":"upstream timeout"}）：
    # 腾讯契约 200 必有 Response（成功带 TextDetections、失败带 Error），
    # 缺失=服务/反代异常，不得伪装成空照片（SR-F-130，同 SR-F-127 口径）
    response = data.get("Response")
    if not isinstance(response, dict):
        _LOGGER.warning("腾讯 OCR 返回 200 但缺少 Response 信封（服务/反代异常）")
        raise ValueError("腾讯 OCR 服务返回了非预期回复（服务/反代异常）")  # noqa: TRY004
    error = response.get("Error")
    if "Error" in response and (not isinstance(error, dict) or not error):
        # 200 + Response.Error 非对象（标量/null 等）或空 dict：腾讯契约
        # 失败信封必为含 Code/Message 的对象，畸形/空壳=服务/反代异常——
        # 明确响，不静默跳过落「未识别到有效文字」伪装空照片
        # （SR-F-134/139，同 127/130 口径）
        _LOGGER.warning("腾讯 OCR 返回 200 但 Response.Error 不是对象或为空（服务/反代异常）")
        raise ValueError("腾讯 OCR 服务返回了非预期回复（服务/反代异常）")
    if error:
        # Code/Message 归一字符串（SR-F-138）：反代常见数字错误码（401）
        # 走「in」判断不再 TypeError，null 同归空串；非空 dict 缺键/键值
        # 均空时文案兜底（SR-F-145）——失败信封不带详情仍是服务/反代异
        # 常，不得落「腾讯 OCR 错误 ：」零信息文案装正常
        code = str(error.get("Code") or "")
        message = str(error.get("Message") or "")
        if not code and not message:
            _LOGGER.warning(
                "腾讯 OCR 返回 200 但 Response.Error 未携带 Code/Message（服务/反代异常）"
            )
            raise ValueError(
                "腾讯 OCR 返回了未携带错误详情的失败信封（服务/反代异常）"
            )
        _LOGGER.warning("腾讯 OCR 返回错误 %s：%s", code, message)
        if "AuthFailure" in code:
            raise ValueError("腾讯 OCR 鉴权失败：请检查 SecretId/SecretKey")
        if "LimitExceeded" in code:
            raise ValueError(f"腾讯 OCR 调用受限（免费额度/频率）：{message or code}")
        raise ValueError(f"腾讯 OCR 错误 {code}：{message}")
    # TextDetections 缺失=合法空照片（腾讯空白图形状），走下方空行路径
    # 保留「未识别到有效文字」；存在但非数组/元素非对象=畸形信封
    # （SR-F-130，错误必须响）
    detections = response.get("TextDetections")
    if detections is not None and not isinstance(detections, list):
        _LOGGER.warning("腾讯 OCR 返回 200 但 TextDetections 不是数组（服务/反代异常）")
        raise ValueError("腾讯 OCR 服务返回了非预期回复（服务/反代异常）")
    lines = []
    for item in detections or []:
        if not isinstance(item, dict):
            _LOGGER.warning(
                "腾讯 OCR 返回 200 但 TextDetections 元素不是对象（服务/反代异常）"
            )
            raise ValueError("腾讯 OCR 服务返回了非预期回复（服务/反代异常）")  # noqa: TRY004
        lines.append(str(item.get("DetectedText") or ""))
    suggestions = parse_ocr_text(lines)
    if not suggestions:
        raise ValueError("OCR 未识别到有效文字")
    return suggestions


async def _recognize_local(
    manager: ScanManager, image: bytes
) -> list[dict[str, Any]]:
    """Recognize via the self-hosted RapidOCR sidecar (本地引擎).

    Contract with the Mac-side service (D:/project/plans/pending/计划-本地OCR接入-20261018.md): POST
    the raw image bytes, reply is ``{"ok": true, "lines": [...]}`` in
    reading order; HTTP error replies carry the same body with ``ok:false``.
    Recognition failures must surface — never fall through as an "empty
    photo".
    """
    url = str(manager._opt(CONF_SCAN_LOCAL_URL) or "").strip()
    if not url:
        raise ValueError("未配置本地 OCR 服务地址 scan_local_url（请在集成选项里填写）")
    session = async_get_clientsession(manager.hass)
    async with session.post(
        url,
        data=image,
        headers={"Content-Type": "application/octet-stream"},
        timeout=ClientTimeout(total=SCAN_TIMEOUT_SECONDS),
    ) as resp:
        # 先读 body 再判状态（SR-F-084）：sidecar 的 4xx/5xx 也带结构化错误体
        # （{"ok":false,"message":...}），先 raise_for_status 会把它丢掉、把
        # 推理失败误诊为地址错误；200 + 非 JSON（反代错误页）也在这里归一成
        # 明确失败而非 ContentTypeError。
        raw = await resp.text()
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        if resp.status >= 400:
            detail = ""
            if isinstance(data, dict):
                detail = str(data.get("message") or data.get("error") or "")
            _LOGGER.warning("本地 OCR 服务返回 %s：%s", resp.status, detail)
            raise ValueError(
                f"本地 OCR 服务返回 {resp.status}：{detail or '请检查服务地址与端口'}"
            )
    if not isinstance(data, dict) or data.get("ok") is not True:
        message = (
            str(data.get("message") or data.get("error") or "未知错误")
            if isinstance(data, dict)
            else "回复不是 JSON 对象"
        )
        _LOGGER.warning("本地 OCR 返回失败：%s", message)
        raise ValueError(f"本地 OCR 失败：{message}")
    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list) or not all(
        isinstance(line, str) for line in raw_lines
    ):
        # 非字符串数组会被逐字符/逐键迭代成垃圾建议静默入库（SR-F-086）
        raise ValueError("本地 OCR 返回的 lines 必须是字符串数组")
    suggestions = parse_ocr_text(raw_lines)
    if not suggestions:
        raise ValueError("OCR 未识别到有效文字")
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
        self._webhook_url_cache: str | None = None
        self._webhook_url_warned = False
        self._listeners: list[Any] = []

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------

    def _opt(self, key: str, default: Any = None) -> Any:
        return self.entry.options.get(key, self.entry.data.get(key, default))

    def _engine_name(self) -> str:
        """Normalized scan engine id; unknown values fall back to default."""
        engine = str(self._opt(CONF_SCAN_ENGINE) or DEFAULT_SCAN_ENGINE)
        return engine if engine in SCAN_ENGINES else DEFAULT_SCAN_ENGINE

    def _missing_credential_error(self) -> str | None:
        """Engine-aware credential check; None = ready to recognize.

        OCR-only setups (engine=ocr + Tencent keys, no LLM key) were locked
        out by the engine-blind vision-api-key check (SR-F-070). The local
        engine needs no credentials, only the sidecar URL.
        """
        engine = self._engine_name()
        if engine == SCAN_ENGINE_OCR:
            if not self._opt(CONF_OCR_SECRET_ID) or not self._opt(CONF_OCR_SECRET_KEY):
                return "未配置腾讯云 OCR 的 SecretId/SecretKey（请在集成选项里填写）"
            return None
        if engine == SCAN_ENGINE_LOCAL:
            if not str(self._opt(CONF_SCAN_LOCAL_URL) or "").strip():
                return "未配置本地 OCR 服务地址 scan_local_url（请在集成选项里填写）"
            return None
        if not self._opt(CONF_SCAN_API_KEY):
            return "未配置识别 API key（请在集成选项里填写）"
        return None

    def service_error_hint(self) -> str:
        """Engine-aware hint for HTTP-level recognition errors.

        Single source shared by the webhook and the panel ScanView so the
        two entries can't drift apart (SR-F-085).
        """
        engine = self._engine_name()
        if engine == SCAN_ENGINE_LOCAL:
            return "请检查本地 OCR 服务地址与端口"
        if engine == SCAN_ENGINE_OCR:
            return "请检查 SecretId/SecretKey、免费额度与网络"
        return "请检查 API key、配额或模型名"

    @property
    def webhook_url(self) -> str:
        """Full webhook URL for the phone shortcut — always the LAN address.

        ``local_only=True`` swallows non-LAN requests, so advertising an
        external URL (the ``async_generate_url`` default) would point the
        shortcut at an endpoint that silently returns an empty 200. On
        SSL-only instances with no internal URL configured there is no LAN
        address at all — return an empty string instead of failing setup.
        Resolved once and cached: the panel polls this attribute every 5 s
        (and sensor attributes re-read it), so an uncached miss would log a
        WARNING on every poll (SR-F-096's noise amplification). Only the
        SUCCESS result is cached — a failure (SSL-only, no internal_url)
        warns once and re-resolves on later reads so that configuring
        internal_url recovers without an integration reload (SR-F-100).
        """
        if self._webhook_url_cache:
            return self._webhook_url_cache
        try:
            self._webhook_url_cache = async_generate_url(
                self.hass, self.webhook_id,
                allow_external=False, prefer_external=False,
            )
        except NoURLAvailableError:
            if not self._webhook_url_warned:
                _LOGGER.warning(
                    "[%s] 找不到可用的内网地址（use_ssl 且未配 internal_url？），"
                    "webhook 地址暂不可展示；拍照扫描功能本身不受影响",
                    self.engine.warehouse_name,
                )
                self._webhook_url_warned = True
            return ""
        return self._webhook_url_cache

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def async_setup(self) -> list[Any]:
        """Register the webhook and the notification-action listener.

        Idempotent: a failed setup leaves the old handler registered (HA
        never unloads a SETUP_ERROR entry), and a plain re-register would
        raise ``ValueError: Handler is already defined!`` forever after;
        the bus-listener side unsubscribes first too — listeners stacked by
        a repeated setup would route one button tap N times (SR-F-128,
        FilterEngine.async_setup's teardown-first precedent).
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
        # 幂等（同 FilterEngine.async_setup 的 teardown-first 惯例）：重复
        # setup 先摘旧 bus 监听再登记——webhook 有 unregister-first 而监听
        # 此前没有，叠加监听会把一次确认按钮重复入库（SR-F-128）
        for unsub in self._listeners:
            unsub()
        self._listeners.clear()
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
        credential_error = self._missing_credential_error()
        if credential_error:
            return web.json_response({"ok": False, "error": credential_error})

        try:
            suggestions = await self._recognize(image)
            # SR-F-164：queue_suggestions 的整批形状校验失败 raise
            # ValueError——纳入本 try 复用既有 ValueError→ok:false 通道
            # （旧位置在 try 外，校验失败冒 500 通用诊断）。
            queued = self.queue_suggestions(suggestions)
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
            hint = self.service_error_hint()
            _LOGGER.warning(
                "[%s] 识别服务返回 %s（%s）",
                self.engine.warehouse_name, err.status, hint,
            )
            return web.json_response(
                {"ok": False, "error": f"识别服务返回 {err.status}：{hint}"}
            )
        except (ClientError, OSError) as err:
            _LOGGER.warning("[%s] 识别服务连接失败：%s", self.engine.warehouse_name, err)
            return web.json_response({"ok": False, "error": "识别服务连接失败"})
        except Exception:
            _LOGGER.exception("[%s] 识别失败", self.engine.warehouse_name)
            return web.json_response({"ok": False, "error": "识别失败，见日志"})

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
        """Ask the configured recognition engine for item suggestions.

        Raises ValueError when the reply cannot be turned into suggestions;
        the webhook handler answers ok:false in that case.
        """
        engine = self._engine_name()
        if engine == SCAN_ENGINE_OCR:
            return await _recognize_ocr(self, image)
        if engine == SCAN_ENGINE_LOCAL:
            return await _recognize_local(self, image)
        return await self._recognize_llm(image)

    async def _recognize_llm(self, image: bytes) -> list[dict[str, Any]]:
        """Vision-model path (OpenAI-compatible chat/completions)."""
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
            # 先读 body 再判状态（SR-F-095）：OpenAI 兼容端点的 4xx 错误体
            # （{"error":{"message":...}}）直达用户，而不是只剩状态码。
            raw = await resp.text()
            try:
                data = json.loads(raw) if raw else None
            except ValueError:
                data = None
            if resp.status >= 400:
                detail = ""
                if isinstance(data, dict):
                    error = data.get("error")
                    if isinstance(error, dict):
                        detail = str(error.get("message") or error.get("code") or "")
                    elif error is not None:
                        detail = str(error)
                    else:
                        detail = str(data.get("message") or "")
                _LOGGER.warning(
                    "[%s] 视觉模型服务返回 HTTP %s：%s",
                    self.engine.warehouse_name, resp.status, detail,
                )
                raise ValueError(
                    f"识别服务返回 {resp.status}：{detail or self.service_error_hint()}"
                )
            if not isinstance(data, dict):
                # 200 + 非 JSON（HTML/空体）：按服务故障响（SR-F-131，与
                # local/腾讯信封口径统一），不得落 KeyError 通用「识别失败」
                _LOGGER.warning(
                    "[%s] 视觉模型服务返回 200 但回复不是 JSON（服务/反代异常）",
                    self.engine.warehouse_name,
                )
                raise ValueError("识别服务返回了非 JSON 回复（服务/反代异常）")  # noqa: TRY004
        # choices/message.content 结构校验（SR-F-131）：畸形信封（200 错
        # 误体/空 choices/message 缺失/content 非字符串）明确响，不落
        # KeyError 通用「识别失败」；4xx error.message 路径在上方分支保持
        choices = data.get("choices")
        message = (
            choices[0].get("message")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict)
            else None
        )
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            _LOGGER.warning(
                "[%s] 视觉模型服务返回 200 但回复缺少 choices/message.content",
                self.engine.warehouse_name,
            )
            # TRY004 的 noqa 标记须贴多行 raise 首行（同 parse_suggestions 先例）；
            # 必须 ValueError：webhook 只捕 ValueError 转 ok:false，TypeError 会
            # 落通用「识别失败」诊断退化
            raise ValueError(  # noqa: TRY004
                "识别服务返回了非预期回复（缺少 choices/message.content）"
            )
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
        # SR-F-164：入参形状契约校验前置到写入循环之前（公开方法+队列落
        # 盘持久化，与 confirm 同一威胁模型）——坏元素整批拒绝，零写入，
        # 不留「前面几条已进 bucket、mark_dirty 未执行」的半批内存态；
        # 旧实现非 dict 元素的 ``**suggestion`` 展开在循环中段 TypeError。
        # 必须 ValueError：两个调用方（webhook/_handle_webhook、面板
        # ScanView）只捕 ValueError 转 ok:false（1172 行先例注释同构），
        # 其他异常类型落通用「识别失败」诊断退化。
        bad = next((s for s in suggestions if not isinstance(s, dict)), None)
        if bad is not None:
            raise ValueError(
                f"扫描建议数据损坏（非对象：{type(bad).__name__}），整批未入库"
            )
        now = datetime.now(UTC).isoformat()
        queued: list[dict[str, Any]] = []
        for suggestion in suggestions:
            sid = secrets.token_hex(8)
            # SR-F-164：生成键写在对入参展开之后——suggestion 自带的 id/
            # created_at 不得覆盖新生成值（旧顺序 {"id":…, **suggestion}
            # PEP 448 后者胜，入参 id 覆盖生成 sid → entry["id"] 与桶键
            # 失配，通知 tag/确认/忽略 action/面板 suggestion.id 全部落空）
            entry = {**suggestion, "id": sid, "created_at": now}
            bucket[sid] = entry
            queued.append(entry)
        while len(bucket) > SCAN_MAX_QUEUE:
            # SR-F-163：排序 key 对非 dict 条目兜底空串——坏条目（None 等）
            # 在此 AttributeError 会让插入之后、mark_dirty 之前的队列半批
            # 进内存未标脏，webhook 侧冒 500。
            oldest = min(
                bucket,
                key=lambda sid: (
                    bucket[sid].get("created_at", "") if isinstance(bucket[sid], dict) else ""
                ),
            )
            del bucket[oldest]
        self.store.mark_dirty(self.entry.entry_id)
        return queued

    def confirm(
        self, sid: str, overrides: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Turn one suggestion into a real item via the regular add path."""
        # SR-F-163：pop 的 default 用 _MISSING 哨兵——None 值条目与「无此
        # sid」不再不可区分，None 落入下方 isinstance 守卫走「数据损坏已
        # 留队」明确报错（旧实现 pop(sid, None) 把 None 条目静默当不存在
        # 删除，与 SR-F-161 语义相反）。
        suggestion = self.queue().pop(sid, _MISSING)
        if suggestion is _MISSING:
            return None
        # SR-F-161：前置归一（fields/overrides/extra 构造与坏形态检查）全
        # 部纳入 restore try——队列公开可写+持久化跨版本，坏形态条目 pop
        # 后必须能回队（旧实现归一在 try 外：坏 extra 的 dict()/非 dict
        # 建议的 .items() 抛错即建议已丢+scan_confirm 服务无信息 500，
        # SR-F-159 同款兄弟面）。可救则救（坏 extra 置空告警继续），不可
        # 救按坏条目拒绝+留队+明确错误（与 add_item 的 StockroomItemError
        # 同走 except 回队通道）
        try:
            if not isinstance(suggestion, dict):
                raise HomeAssistantError(
                    f"扫描建议数据损坏（非对象：{type(suggestion).__name__}），"
                    "已留队，可忽略该条目或重新识别"
                )
            fields = {key: value for key, value in suggestion.items() if key not in ("id", "created_at")}
            overrides = dict(overrides or {})
            raw_extra = fields.get("extra")
            if not raw_extra:
                extra = {}
            elif isinstance(raw_extra, dict):
                extra = dict(raw_extra)
            else:
                # 坏 extra（非 dict 真值）可救则救：WARNING 在案（错误必须
                # 响不静默）+置空丢弃该字段继续入库
                _LOGGER.warning(
                    "[%s] 扫描建议 extra 字段损坏（非对象：%r），丢弃该字段继续入库",
                    self.engine.warehouse_name,
                    raw_extra,
                )
                extra = {}
            # 可清空识别属性的空值语义与 update_item 一致（ocr-ux-R1 #3）：面板编辑
            # 对话框总是发送 trim 后的值——空串/0 = 清除该属性，缺键 = 保持识别值。
            for key in (EXTRA_MATERIAL, EXTRA_COLOR):
                if overrides.get(key) is not None:
                    value = str(overrides.pop(key)).strip()
                    if value:
                        extra[key] = value
                    else:
                        extra.pop(key, None)
            if overrides.get(EXTRA_FULL_WEIGHT_G) is not None:
                weight = overrides.pop(EXTRA_FULL_WEIGHT_G)
                try:
                    weight_number = float(weight)
                except (TypeError, ValueError):
                    weight_number = 0.0
                if 0 < weight_number <= FULL_WEIGHT_MAX:
                    extra[EXTRA_FULL_WEIGHT_G] = weight_number
                else:
                    extra.pop(EXTRA_FULL_WEIGHT_G, None)
            fields.update({key: value for key, value in overrides.items() if value is not None})
            # 空名称覆盖没有意义（add_item 会 slugify 出哈希 id）——保留识别名。
            # 右值走 .get 兜底链收空串（SR-F-159 坏条目防线）：旧实现 fields
            # ["name"] 直索引，缺 name 的坏条目在此 KeyError 且位于 add_item
            # 的 restore try 之外——建议已 pop 丢失+scan_confirm 服务 500；
            # "name": None 旧实现经 add_item 的 str(name).strip() 变「None」
            # 字面名条目。收空串后坏条目滑入 add_item 既有空名拒绝
            # （StockroomItemError 在 restore try 内：建议留队、错误明确、
            # 服务干净失败），override 带有效名称的编辑补名修复流不受影响
            if not str(fields.get("name") or "").strip():
                fields["name"] = suggestion.get("name") or fields.get("name") or ""
            if extra:
                fields["extra"] = extra
            else:
                fields.pop("extra", None)
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
            # SR-F-163：哨兵判定——None 条目也计 1 并 mark_dirty 落盘
            # （旧实现 pop(ref, None) 把 None 条目 pop 掉却计 0：条目被删
            # 但删除不落盘，scan_dismiss 还报「不存在」）。
            count = 1 if bucket.pop(ref, _MISSING) is not _MISSING else 0
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
            # SR-F-152：summary 构造与 async_call 同入逐条 try——缺 name/
            # quantity 非数值的坏条目（queue_suggestions 是公开方法，队列
            # 落 META_SCAN_PENDING 持久化，跨版本/手工数据可缺键）单条跳
            # 过落日志，不再在 try 外 KeyError/:g 异常整批中止（SR-F-146
            # 同型邻界面）
            try:
                summary = f"{suggestion['name']}（{suggestion.get('quantity', 0):g} {suggestion.get('unit', '')}）"
                # SR-F-146：target 不塞 service_data——服务名已是设备专属
                # notify.mobile_app_<device>，实证 HA 2026.1 容器源码
                # notify/legacy.py:248-249 设备专属分支用
                # registered_targets[service.service] 自行注入 target，塞进
                # service_data 是被覆盖的死重键（NOTIFY_SERVICE_SCHEMA 虽声明
                # target 合法，语义上定向应走 async_call 的 target= 关键字参数）
                await self.hass.services.async_call(
                    notify.DOMAIN,
                    service_name,
                    {
                        "title": "Stockroom 扫描入库",
                        "message": f"识别到：{summary}",
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
            except Exception:
                # 单条失败不炸整批剩余通知（本函数走 async_create_task 的
                # fire-and-forget 任务，旧实现一条异常即整批跳过）；落日志
                # 收口（异步不吞异常纪律）。name/id 兜底取值防异常处理自身
                # 对坏条目再抛（SR-F-152）
                _LOGGER.exception(
                    "[%s] 确认通知发送失败，跳过 %s 继续其余通知",
                    self.engine.warehouse_name,
                    suggestion.get("name") or suggestion.get("id", "?"),
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
