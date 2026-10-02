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
ALL_SERVICES = [
    SERVICE_ADD_ITEM,
    SERVICE_REMOVE_ITEM,
    SERVICE_CONSUME,
    SERVICE_RESTOCK,
    SERVICE_STOCKTAKE,
    SERVICE_SET_THRESHOLD,
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
