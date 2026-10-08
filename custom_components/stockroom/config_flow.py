"""Config flow for Stockroom (仓储盘点)."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    DeviceSelector,
    DeviceSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_BAMBU_TRAY_ENTITIES,
    CONF_BAMBU_TRAY_MAP,
    CONF_DEFAULT_LOW_THRESHOLD,
    CONF_OCR_SECRET_ID,
    CONF_OCR_SECRET_KEY,
    CONF_SCAN_API_KEY,
    CONF_SCAN_BASE_URL,
    CONF_SCAN_DEVICE_ID,
    CONF_SCAN_ENABLED,
    CONF_SCAN_ENGINE,
    CONF_SCAN_MODEL,
    CONF_SUMMARY_BY_CATEGORY,
    CONF_WAREHOUSE_NAME,
    DEFAULT_LOW_THRESHOLD,
    DEFAULT_SCAN_BASE_URL,
    DEFAULT_SCAN_ENABLED,
    DEFAULT_SCAN_ENGINE,
    DEFAULT_SCAN_MODEL,
    DEFAULT_SUMMARY_BY_CATEGORY,
    DEFAULT_WAREHOUSE_NAME,
    DOMAIN,
    SCAN_ENGINE_LLM,
    SCAN_ENGINE_OCR,
    SCAN_ENGINES,
)
from .inventory import InventoryEngine

_LOGGER = logging.getLogger(__name__)


class StockroomConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the single-step warehouse creation flow."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        # Multiple warehouses are allowed (no unique identifier); an
        # identical title only warns once, then a second submit confirms.
        self._duplicate_warned = False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create a warehouse from its name."""
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input[CONF_WAREHOUSE_NAME]).strip() or DEFAULT_WAREHOUSE_NAME
            duplicate = any(
                entry.title == name for entry in self._async_current_entries()
            )
            if duplicate and not self._duplicate_warned:
                self._duplicate_warned = True
                errors[CONF_WAREHOUSE_NAME] = "duplicate_name"
            else:
                return self.async_create_entry(
                    title=name, data={CONF_WAREHOUSE_NAME: name}
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_WAREHOUSE_NAME, default=DEFAULT_WAREHOUSE_NAME
                ): TextSelector(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    # ------------------------------------------------------------------
    # Options flow
    # ------------------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> StockroomOptionsFlow:
        """Return the options flow."""
        return StockroomOptionsFlow()


class StockroomOptionsFlow(OptionsFlow):
    """Reconfigure thresholds, summary breakdown and the AMS tray mapping."""

    def __init__(self) -> None:
        """Initialize."""
        self._tray_entities: list[str] = []
        self._base_input: dict[str, Any] = {}

    def _opt(self, entry: ConfigEntry, key: str, default: Any) -> Any:
        """Options-over-data fallback chain."""
        return entry.options.get(key, entry.data.get(key, default))

    def _engine(self, entry: ConfigEntry) -> InventoryEngine | None:
        """Return the live engine of this entry, if set up."""
        engine = self.hass.data.get(DOMAIN, {}).get("entries", {}).get(entry.entry_id)
        return engine if isinstance(engine, InventoryEngine) else None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: scalars + which Bambu tray sensors to map."""
        entry = self.config_entry
        if user_input is not None:
            self._base_input = dict(user_input)
            self._tray_entities = list(user_input.get(CONF_BAMBU_TRAY_ENTITIES) or [])
            if self._tray_entities:
                return await self.async_step_ams_map()
            return self._finish(self._base_input, {})

        current_map = dict(self._opt(entry, CONF_BAMBU_TRAY_MAP, {}) or {})
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DEFAULT_LOW_THRESHOLD,
                    default=float(
                        self._opt(entry, CONF_DEFAULT_LOW_THRESHOLD, DEFAULT_LOW_THRESHOLD)
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(min=0, step=0.1, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_SUMMARY_BY_CATEGORY,
                    default=bool(
                        self._opt(entry, CONF_SUMMARY_BY_CATEGORY, DEFAULT_SUMMARY_BY_CATEGORY)
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_SCAN_ENABLED,
                    default=bool(self._opt(entry, CONF_SCAN_ENABLED, DEFAULT_SCAN_ENABLED)),
                ): BooleanSelector(),
                vol.Required(
                    CONF_SCAN_ENGINE,
                    default=str(
                        self._opt(entry, CONF_SCAN_ENGINE, DEFAULT_SCAN_ENGINE)
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {
                                "value": SCAN_ENGINE_LLM,
                                "label": "视觉模型（OpenAI 兼容端点，可指向本地 Ollama）",
                            },
                            {"value": SCAN_ENGINE_OCR, "label": "在线 OCR（腾讯云通用印刷体，免费额度）"},
                        ]
                    )
                ),
                vol.Optional(
                    CONF_OCR_SECRET_ID,
                    description={
                        "suggested_value": self._opt(entry, CONF_OCR_SECRET_ID, "")
                    },
                ): TextSelector(),
                vol.Optional(
                    CONF_OCR_SECRET_KEY,
                    description={"suggested_value": self._opt(entry, CONF_OCR_SECRET_KEY, "")},
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Optional(
                    CONF_SCAN_DEVICE_ID,
                    description={"suggested_value": self._opt(entry, CONF_SCAN_DEVICE_ID, "")},
                ): DeviceSelector(DeviceSelectorConfig(integration="mobile_app")),
                vol.Optional(
                    CONF_SCAN_BASE_URL,
                    description={
                        "suggested_value": self._opt(entry, CONF_SCAN_BASE_URL, DEFAULT_SCAN_BASE_URL)
                    },
                ): TextSelector(),
                vol.Optional(
                    CONF_SCAN_API_KEY,
                    description={"suggested_value": self._opt(entry, CONF_SCAN_API_KEY, "")},
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Optional(
                    CONF_SCAN_MODEL,
                    description={"suggested_value": self._opt(entry, CONF_SCAN_MODEL, DEFAULT_SCAN_MODEL)},
                ): TextSelector(),
                vol.Optional(
                    CONF_BAMBU_TRAY_ENTITIES,
                    description={"suggested_value": sorted(current_map)},
                ): EntitySelector(
                    EntitySelectorConfig(
                        integration="bambu_lab", domain="sensor", multiple=True
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_ams_map(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: pick the item fed by each selected tray sensor."""
        entry = self.config_entry
        current_map = dict(self._opt(entry, CONF_BAMBU_TRAY_MAP, {}) or {})
        engine = self._engine(entry)
        if user_input is not None:
            mapping: dict[str, str] = {}
            errors: dict[str, str] = {}
            for entity_id in self._tray_entities:
                value = str(user_input.get(entity_id) or "").strip()
                if not value:
                    continue
                if engine is not None and value not in engine.items:
                    # Custom values are free-text; a typo here would become a
                    # dangling mapping that silently never deducts.
                    errors[entity_id] = "unknown_item"
                    continue
                mapping[entity_id] = value
            if errors:
                # Redisplay with what the user just typed so the valid picks
                # survive the failed validation round.
                typed = {
                    entity_id: str(user_input.get(entity_id) or "")
                    for entity_id in self._tray_entities
                }
                return self.async_show_form(
                    step_id="ams_map",
                    data_schema=self._ams_map_schema({**current_map, **typed}),
                    errors=errors,
                )
            return self._finish(self._base_input, mapping)

        return self.async_show_form(
            step_id="ams_map", data_schema=self._ams_map_schema(current_map)
        )

    def _ams_map_schema(self, current_map: dict[str, str]) -> vol.Schema:
        """Build the per-tray item dropdowns for the currently selected trays."""
        entry = self.config_entry
        engine = self._engine(entry)
        options = [
            {"value": item_id, "label": f"{item.get('name', item_id)} ({item_id})"}
            for item_id, item in sorted((engine.items if engine else {}).items())
        ]

        schema: dict[Any, Any] = {}
        for entity_id in self._tray_entities:
            schema[vol.Optional(entity_id, default=current_map.get(entity_id, ""))] = (
                SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.DROPDOWN,
                        custom_value=True,
                    )
                )
            )
        return vol.Schema(schema)

    def _finish(
        self, base_input: dict[str, Any], mapping: dict[str, str]
    ) -> ConfigFlowResult:
        """Assemble the final options payload."""
        base_url = str(base_input.get(CONF_SCAN_BASE_URL) or "").strip().rstrip("/")
        engine = str(
            base_input.get(CONF_SCAN_ENGINE) or DEFAULT_SCAN_ENGINE
        ).strip()
        options = {
            CONF_DEFAULT_LOW_THRESHOLD: float(
                base_input.get(CONF_DEFAULT_LOW_THRESHOLD, DEFAULT_LOW_THRESHOLD)
            ),
            CONF_SUMMARY_BY_CATEGORY: bool(
                base_input.get(CONF_SUMMARY_BY_CATEGORY, DEFAULT_SUMMARY_BY_CATEGORY)
            ),
            CONF_SCAN_ENABLED: bool(
                base_input.get(CONF_SCAN_ENABLED, DEFAULT_SCAN_ENABLED)
            ),
            CONF_SCAN_ENGINE: engine if engine in SCAN_ENGINES else DEFAULT_SCAN_ENGINE,
            CONF_OCR_SECRET_ID: str(base_input.get(CONF_OCR_SECRET_ID) or "").strip(),
            CONF_OCR_SECRET_KEY: str(
                base_input.get(CONF_OCR_SECRET_KEY) or ""
            ).strip(),
            CONF_SCAN_DEVICE_ID: str(
                base_input.get(CONF_SCAN_DEVICE_ID) or ""
            ),
            CONF_SCAN_BASE_URL: base_url or DEFAULT_SCAN_BASE_URL,
            CONF_SCAN_API_KEY: str(base_input.get(CONF_SCAN_API_KEY) or ""),
            CONF_SCAN_MODEL: str(
                base_input.get(CONF_SCAN_MODEL) or ""
            ).strip()
            or DEFAULT_SCAN_MODEL,
            CONF_BAMBU_TRAY_MAP: mapping,
        }
        return self.async_create_entry(data=options)
