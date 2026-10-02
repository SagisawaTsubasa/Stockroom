"""Home Assistant stubs so the Stockroom unit tests run without a HA install.

Same approach as Filter-Life-Tracker: only the surface the integration
modules actually touch is faked here; behavior under test (item CRUD, clamp,
stocktake stamps, low-stock judgement, AMS idempotency) is pure Python.
Tests drive engine methods directly and monkeypatch the dispatcher.

If the real ``homeassistant`` package is installed, stubs are skipped so the
tests exercise the real helpers instead.
"""

from __future__ import annotations

# ``datetime.UTC`` is 3.11+; shim it for older local interpreters so the
# integration modules (which target HA's modern Python) still import.
import datetime as _datetime
import sys
import types
from datetime import timezone
from pathlib import Path
from typing import Any, Generic, TypeVar

if not hasattr(_datetime, "UTC"):
    _datetime.UTC = timezone.utc  # type: ignore[attr-defined]

# Make ``custom_components.stockroom`` importable from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 已安装真实 homeassistant 时不打桩，避免遮蔽依赖真实行为的测试
_INSTALL_STUBS = "homeassistant" not in sys.modules

_T = TypeVar("_T")


def _module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


def _identity_decorator(func):
    return func


def _accepts_anything(name: str):
    return type(name, (), {"__init__": lambda self, *a, **k: None})


class _Store(Generic[_T]):
    """Minimal stand-in for homeassistant.helpers.storage.Store."""

    def __init__(self, hass: Any, version: int, key: str, **kwargs: Any) -> None:
        self.hass = hass
        self.version = version
        self.key = key
        self.kwargs = kwargs
        self.saved: list[Any] = []
        self.load_result: Any = None

    async def async_save(self, data: Any) -> None:
        self.saved.append(data)

    async def async_load(self) -> Any:
        return self.load_result


if _INSTALL_STUBS:
    _module("homeassistant")
    _module("homeassistant.helpers")
    _module("homeassistant.components")

    class _SensorEntity:
        pass

    class _BinarySensorEntity:
        pass

    _module(
        "homeassistant.components.sensor",
        SensorEntity=_SensorEntity,
        SensorStateClass=types.SimpleNamespace(MEASUREMENT="measurement"),
    )
    _module(
        "homeassistant.components.binary_sensor",
        BinarySensorEntity=_BinarySensorEntity,
        BinarySensorDeviceClass=types.SimpleNamespace(PROBLEM="problem"),
    )

    class _ConfigFlow:
        # Real ConfigFlow consumes domain=/title= in __init_subclass__.
        def __init_subclass__(cls, domain=None, title=None, **kwargs: Any) -> None:
            super().__init_subclass__(**kwargs)

    class _OptionsFlow:
        config_entry = None

    _module(
        "homeassistant.config_entries",
        ConfigEntry=type("ConfigEntry", (), {}),
        ConfigFlow=_ConfigFlow,
        OptionsFlow=_OptionsFlow,
        ConfigFlowResult=type("ConfigFlowResult", (), {}),
    )

    class _Platform:
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"

    _module(
        "homeassistant.const",
        STATE_UNAVAILABLE="unavailable",
        STATE_UNKNOWN="unknown",
        EVENT_HOMEASSISTANT_STOP="homeassistant_stop",
        Platform=_Platform,
    )
    _module(
        "homeassistant.core",
        Event=type("Event", (), {}),
        EventStateChangedData=type("EventStateChangedData", (), {}),
        HomeAssistant=type("HomeAssistant", (), {}),
        ServiceCall=type("ServiceCall", (), {}),
        State=type("State", (), {"state": "", "attributes": {}}),
        callback=_identity_decorator,
    )
    _module("homeassistant.exceptions", HomeAssistantError=type("HomeAssistantError", (Exception,), {}))

    # config_validation surface used at schema-build time; validators are
    # never invoked in these tests.
    def _cv_string(value):
        return str(value)

    def _cv_positive_float(value):
        number = float(value)
        if number < 0:
            raise ValueError("must be >= 0")
        return number

    _module(
        "homeassistant.helpers.config_validation",
        string=_cv_string,
        positive_float=_cv_positive_float,
    )
    _module(
        "homeassistant.helpers.device_registry",
        DeviceInfo=lambda **kwargs: dict(kwargs),
    )
    _module(
        "homeassistant.helpers.dispatcher",
        async_dispatcher_connect=lambda *args, **kwargs: lambda: None,
        async_dispatcher_send=lambda *args, **kwargs: None,
        dispatcher_send=lambda *args, **kwargs: None,
    )
    _module(
        "homeassistant.helpers.entity",
        Entity=type("Entity", (), {"hass": None, "async_write_ha_state": lambda self: None}),
        DeviceInfo=lambda **kwargs: dict(kwargs),
    )
    _module(
        "homeassistant.helpers.entity_platform",
        AddEntitiesCallback=object,
    )
    _module(
        "homeassistant.helpers.entity_registry",
        # Only referenced at runtime by the removal helper; unit tests never
        # invoke it (no platform tests), import-time just needs the module.
        async_get=lambda hass: None,
    )
    _module(
        "homeassistant.helpers.event",
        async_call_later=lambda *args, **kwargs: lambda: None,
        async_track_state_change_event=lambda *args, **kwargs: lambda: None,
        async_track_time_interval=lambda *args, **kwargs: lambda: None,
    )
    _module(
        "homeassistant.helpers.selector",
        BooleanSelector=_accepts_anything("BooleanSelector"),
        EntitySelector=_accepts_anything("EntitySelector"),
        EntitySelectorConfig=_accepts_anything("EntitySelectorConfig"),
        NumberSelector=_accepts_anything("NumberSelector"),
        NumberSelectorConfig=_accepts_anything("NumberSelectorConfig"),
        NumberSelectorMode=types.SimpleNamespace(BOX="box"),
        SelectSelector=_accepts_anything("SelectSelector"),
        SelectSelectorConfig=_accepts_anything("SelectSelectorConfig"),
        SelectSelectorMode=types.SimpleNamespace(DROPDOWN="dropdown", LIST="list"),
        TextSelector=_accepts_anything("TextSelector"),
    )
    _module("homeassistant.helpers.storage", Store=_Store)
