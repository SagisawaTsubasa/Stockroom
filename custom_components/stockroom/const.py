"""Constants for Stockroom (仓储盘点)."""

from datetime import timedelta

DOMAIN = "stockroom"

# ----------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------

STORAGE_KEY = f"{DOMAIN}.storage"
STORAGE_VERSION = 1

# ADR: batched flush every 10 minutes + forced flush on HA stop.
FLUSH_INTERVAL = timedelta(minutes=10)

# Config entry schema version (identity migration hook, see FLT precedent).
CONFIG_ENTRY_VERSION = 1
CONFIG_ENTRY_MINOR_VERSION = 1

# ----------------------------------------------------------------------
# Config / options keys
# ----------------------------------------------------------------------

CONF_WAREHOUSE_NAME = "warehouse_name"
CONF_DEFAULT_LOW_THRESHOLD = "default_low_threshold"
CONF_SUMMARY_BY_CATEGORY = "summary_by_category"
# Options flow intermediate: selected bambu_lab tray sensor entity_ids.
CONF_BAMBU_TRAY_ENTITIES = "bambu_tray_entities"
# Persisted option: {tray sensor entity_id -> item_id}.
CONF_BAMBU_TRAY_MAP = "bambu_tray_map"

# ----------------------------------------------------------------------
# Photo scan-to-stock (拍照扫描入库)
# ----------------------------------------------------------------------

CONF_SCAN_ENABLED = "scan_enabled"
CONF_SCAN_BASE_URL = "scan_base_url"
CONF_SCAN_API_KEY = "scan_api_key"
CONF_SCAN_MODEL = "scan_model"
CONF_SCAN_DEVICE_ID = "scan_device_id"

DEFAULT_SCAN_ENABLED = False
DEFAULT_SCAN_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_SCAN_MODEL = "glm-4.5v"

SCAN_WEBHOOK_NAME = "Stockroom 扫描入库"
# Keep under HA's 16MiB request cap even on the JSON+base64 path (×4/3).
SCAN_MAX_IMAGE_BYTES = 12 * 1024 * 1024
SCAN_MAX_SUGGESTIONS = 20
SCAN_MAX_QUEUE = 50
SCAN_TIMEOUT_SECONDS = 60

# Companion-app notification feedback: button actions are prefixed tokens;
# the tap comes back as a mobile_app_notification_action event.
ACTION_CONFIRM_PREFIX = "scan_confirm_"
ACTION_DISMISS_PREFIX = "scan_dismiss_"
NOTIFICATION_ACTION_EVENT = "mobile_app_notification_action"

DEFAULT_WAREHOUSE_NAME = "仓库"
DEFAULT_LOW_THRESHOLD = 1.0
DEFAULT_SUMMARY_BY_CATEGORY = False

# ----------------------------------------------------------------------
# Item model
# ----------------------------------------------------------------------

CATEGORY_FILAMENT = "filament"
CATEGORY_SCREW = "screw"
CATEGORY_OTHER = "other"
CATEGORIES = [CATEGORY_FILAMENT, CATEGORY_SCREW, CATEGORY_OTHER]

# Per-category default unit when add_item does not specify one.
DEFAULT_UNITS = {
    CATEGORY_FILAMENT: "g",
    CATEGORY_SCREW: "颗",
    CATEGORY_OTHER: "个",
}

# Filament extra attributes (AMS deduction counts grams).
EXTRA_MATERIAL = "material"
EXTRA_COLOR = "color"
EXTRA_FULL_WEIGHT_G = "full_weight_g"

ITEM_LOCATION = "location"
ITEM_LOW_THRESHOLD = "low_threshold"
ITEM_LAST_STOCKTAKE = "last_stocktake"
ITEM_UPDATED_AT = "updated_at"

# ----------------------------------------------------------------------
# Services
# ----------------------------------------------------------------------

SERVICE_ADD_ITEM = "add_item"
SERVICE_REMOVE_ITEM = "remove_item"
SERVICE_CONSUME = "consume"
SERVICE_RESTOCK = "restock"
SERVICE_STOCKTAKE = "stocktake"
SERVICE_SET_THRESHOLD = "set_threshold"
SERVICE_SCAN_CONFIRM = "scan_confirm"
SERVICE_SCAN_DISMISS = "scan_dismiss"
ALL_SERVICES = [
    SERVICE_ADD_ITEM,
    SERVICE_REMOVE_ITEM,
    SERVICE_CONSUME,
    SERVICE_RESTOCK,
    SERVICE_STOCKTAKE,
    SERVICE_SET_THRESHOLD,
    SERVICE_SCAN_CONFIRM,
    SERVICE_SCAN_DISMISS,
]

# ----------------------------------------------------------------------
# Events / signals
# ----------------------------------------------------------------------

# Fired on the hass bus after every item mutation so user automations can
# react (e.g. low-stock notifications). We never notify directly.
EVENT_ITEM_CHANGED = "stockroom_item_changed"

SIGNAL_ITEMS_UPDATED = f"{DOMAIN}_items_updated_{{}}"  # per entry_id

# ----------------------------------------------------------------------
# Entity keys (translation_key / unique_id kind part)
# ----------------------------------------------------------------------

KEY_ITEM_QUANTITY = "item_quantity"
KEY_ITEM_LOW_STOCK = "item_low_stock"
KEY_SUMMARY_TOTAL = "summary_total_items"
KEY_SUMMARY_LOW_STOCK = "summary_low_stock"

KIND_QUANTITY = "quantity"
KIND_LOW_STOCK = "low_stock"

# ----------------------------------------------------------------------
# Bambu Lab AMS auto-deduction
# ----------------------------------------------------------------------

BAMBU_STATUS_FINISH = "finish"
# print_status values from which a transition to `finish` counts as a real
# completion; anything else (None/replay, offline reconnect) must not deduct.
BAMBU_RUN_STATES = ("running", "pause", "prepare", "slicing", "init")
BAMBU_STATUS_ENTITY_SUFFIX = "_print_status"
BAMBU_WEIGHT_ENTITY_SUFFIX = "_print_weight"
BAMBU_TASK_NAME_ENTITY_SUFFIX = "_task_name"
BAMBU_START_TIME_ENTITY_SUFFIX = "_start_time"

# meta key holding idempotency records, keyed "f({entry_id}|{printer_prefix})"
# so multiple warehouses mapping the same printer never shadow each other.
META_LAST_DEDUCT = "last_deduct"
# meta key holding per-entry pending scan suggestions: {entry_id: {sid: item}}.
META_SCAN_PENDING = "scan_pending"

# Vision-extraction prompt: labels/packaging first, quantities only when
# grounded, JSON array output with no markdown fencing.
SCAN_PROMPT = (
    "你是仓储盘点助手。识别图片中的物品并输出库存条目建议。\n"
    "只输出一个 JSON 数组，不要任何解释文字或 markdown 代码围栏。每个元素形如：\n"
    '{"name": "条目名（中文，简洁唯一，如 M3×8 或 PLA 白色）", '
    '"category": "filament|screw|other", "quantity": 数字, '
    '"unit": "颗|个|卷|g", "low_threshold": 数字, '
    '"location": "位置或空字符串", '
    '"material": "材质（仅耗材，其他为空字符串）", '
    '"color": "颜色描述（仅耗材，其他为空字符串）", '
    '"full_weight_g": 数字（仅耗材满卷克重，未知填 0）}\n'
    "规则：优先读取包装或标签上的文字（品牌、规格、色号、净重）；"
    "数量只填有依据的数字（标签数量或可见件数），不确定填 0；"
    "散装螺丝的规格无法可靠分辨，规格不确定时把猜测写进 name 并将 quantity 置 0；"
    "最多输出 20 个条目。"
)
