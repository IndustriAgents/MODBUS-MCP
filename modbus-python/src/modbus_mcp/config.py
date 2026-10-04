"""Server configuration, read from the environment when the server starts.

Nothing here runs at import time: ``modbus_mcp.cli.main()`` loads ``.env`` first
and then calls :meth:`Settings.from_env` and :func:`load_tag_map`, so values
from ``.env`` are seen and a bad value stops the server at startup with a
message, instead of surfacing later as a failed tool call.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import codec


class ConfigError(ValueError):
    """Configuration the server cannot run with."""


_TRUE = {"1", "true", "t", "yes", "y", "on"}
_FALSE = {"0", "false", "f", "no", "n", "off"}

MODBUS_TYPES = ("tcp", "udp", "serial")
PARITIES = ("N", "E", "O")
MAX_UNIT_ID = 255


def _raw(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None or not value.strip():
        return None  # unset and empty both mean "use the default"
    return value.strip()


def _int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = _raw(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _float(env: Mapping[str, str], name: str, default: float | None, lo: float) -> float | None:
    raw = _raw(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if not math.isfinite(value) or value < lo:
        raise ConfigError(f"{name} must be a finite number >= {lo}, got {raw!r}")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _raw(env, name)
    if raw is None:
        return default
    if raw.lower() in _TRUE:
        return True
    if raw.lower() in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _choice(env: Mapping[str, str], name: str, default: str, choices: tuple[str, ...]) -> str:
    raw = _raw(env, name)
    if raw is None:
        return default
    for choice in choices:
        if raw.lower() == choice.lower():
            return choice
    raise ConfigError(f"{name} must be one of {', '.join(choices)}, got {raw!r}")


@dataclass(frozen=True)
class Settings:
    """Everything the server reads from ``MODBUS_*`` and ``REGISTER_MAP_FILE``."""

    modbus_type: str = "tcp"
    host: str = "127.0.0.1"
    port: int = 502
    serial_port: str = "/dev/ttyUSB0"
    baudrate: int = 9600
    parity: str = "N"
    stopbits: int = 1
    bytesize: int = 8
    serial_timeout: float = 1.0
    default_slave_id: int = 1
    max_retries: int = 2
    retry_backoff_base: float = 0.2
    tool_timeout: float | None = None
    writes_enabled: bool = False
    register_map_file: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Build settings from ``environ`` (default: ``os.environ``); raise ConfigError if invalid."""
        env = os.environ if environ is None else environ
        tool_timeout = _float(env, "MODBUS_TOOL_TIMEOUT", None, 0.0)
        return cls(
            modbus_type=_choice(env, "MODBUS_TYPE", "tcp", MODBUS_TYPES),
            host=_raw(env, "MODBUS_HOST") or "127.0.0.1",
            port=_int(env, "MODBUS_PORT", 502, 1, 65535),
            serial_port=_raw(env, "MODBUS_SERIAL_PORT") or "/dev/ttyUSB0",
            baudrate=_int(env, "MODBUS_BAUDRATE", 9600, 1, 10_000_000),
            parity=_choice(env, "MODBUS_PARITY", "N", PARITIES),
            stopbits=_int(env, "MODBUS_STOPBITS", 1, 1, 2),
            bytesize=_int(env, "MODBUS_BYTESIZE", 8, 5, 8),
            serial_timeout=_float(env, "MODBUS_TIMEOUT", 1.0, 0.001),  # type: ignore[arg-type]
            default_slave_id=_int(env, "MODBUS_DEFAULT_SLAVE_ID", 1, 0, MAX_UNIT_ID),
            max_retries=_int(env, "MODBUS_MAX_RETRIES", 2, 0, 10),
            retry_backoff_base=_float(env, "MODBUS_RETRY_BACKOFF_BASE", 0.2, 0.0),  # type: ignore[arg-type]
            # 0 keeps its old meaning: no per-tool timeout.
            tool_timeout=tool_timeout if tool_timeout else None,
            writes_enabled=_bool(env, "MODBUS_WRITES_ENABLED", False),
            register_map_file=_raw(env, "REGISTER_MAP_FILE"),
        )

    @property
    def target(self) -> str:
        """Human-readable device address, for messages."""
        if self.modbus_type == "serial":
            return self.serial_port
        return f"{self.host}:{self.port}"


# ---------------------------------------------------------------------------
# Tag map
# ---------------------------------------------------------------------------

_TABLE_ALIASES = {
    "holding": {"holding", "hr", "register", "holding_register", "holding_registers"},
    "input": {"input", "ir", "input_register", "input_registers", "inputs"},
    "coil": {"coil", "coils", "co"},
    "discrete": {"discrete", "di", "discrete_input", "discrete_inputs"},
}
_TAG_KEYS = {
    "table",
    "address",
    "count",
    "slave_id",
    "dtype",
    "byteorder",
    "wordorder",
    "scale",
    "offset",
    "description",
    "unit",
}


@dataclass(frozen=True)
class Tag:
    """One named point from the tag map, validated when the map is loaded."""

    name: str
    table: str
    address: int
    count: int = 1
    slave_id: int | None = None
    dtype: str | None = None
    byteorder: str = "big"
    wordorder: str = "big"
    scale: float = 1.0
    offset: float = 0.0
    description: str | None = None
    unit: str | None = None

    @property
    def writable(self) -> bool:
        return self.table in {"holding", "coil"}

    @property
    def span(self) -> int:
        """Number of registers or bits the tag covers."""
        if self.table in {"holding", "input"}:
            return self.count * codec.register_count(self.dtype or "uint16")
        return self.count

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "table": self.table,
            "address": self.address,
            "count": self.count,
            "slave_id": self.slave_id,
            "writable": self.writable,
        }
        if self.table in {"holding", "input"}:
            out.update(
                dtype=self.dtype,
                byteorder=self.byteorder,
                wordorder=self.wordorder,
                scale=self.scale,
                offset=self.offset,
            )
        if self.description is not None:
            out["description"] = self.description
        if self.unit is not None:
            out["unit"] = self.unit
        return out


def _tag_int(name: str, spec: Mapping[str, Any], key: str, lo: int, hi: int, default=None) -> int:
    value = spec.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"tag {name!r}: {key} must be an integer, got {value!r}")
    if not lo <= value <= hi:
        raise ConfigError(f"tag {name!r}: {key} must be between {lo} and {hi}, got {value}")
    return value


def _tag_number(name: str, spec: Mapping[str, Any], key: str, default: float) -> float:
    value = spec.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigError(f"tag {name!r}: {key} must be a finite number, got {value!r}")
    return float(value)


def parse_tag(name: str, spec: Any) -> Tag:
    """Validate one tag-map entry."""
    if not isinstance(name, str) or not name:
        raise ConfigError(f"tag names must be non-empty strings, got {name!r}")
    if not isinstance(spec, Mapping):
        raise ConfigError(f"tag {name!r}: expected an object, got {type(spec).__name__}")
    unknown = set(spec) - _TAG_KEYS
    if unknown:
        raise ConfigError(
            f"tag {name!r}: unknown key(s) {', '.join(sorted(unknown))}; "
            f"allowed: {', '.join(sorted(_TAG_KEYS))}"
        )
    raw_table = str(spec.get("table", "holding")).lower()
    table = next((t for t, aliases in _TABLE_ALIASES.items() if raw_table in aliases), None)
    if table is None:
        raise ConfigError(f"tag {name!r}: unknown table {spec.get('table')!r}")
    if "address" not in spec:
        raise ConfigError(f"tag {name!r}: address is required")
    address = _tag_int(name, spec, "address", 0, 65535)
    count = _tag_int(name, spec, "count", 1, 65536, default=1)
    slave_id = (
        None if spec.get("slave_id") is None else _tag_int(name, spec, "slave_id", 0, MAX_UNIT_ID)
    )
    dtype = spec.get("dtype")
    typed_keys = {"dtype", "byteorder", "wordorder", "scale", "offset"} & set(spec)
    if table in {"coil", "discrete"} and typed_keys:
        raise ConfigError(
            f"tag {name!r}: {', '.join(sorted(typed_keys))} only apply to holding/input tags"
        )
    if dtype is None and {"scale", "offset"} & set(spec):
        raise ConfigError(f"tag {name!r}: scale and offset need a dtype")
    try:
        if dtype is not None:
            codec.check_dtype(dtype)
        byteorder = codec.check_order(spec.get("byteorder", "big"), "byteorder")
        wordorder = codec.check_order(spec.get("wordorder", "big"), "wordorder")
    except codec.CodecError as exc:
        raise ConfigError(f"tag {name!r}: {exc}") from None
    scale = _tag_number(name, spec, "scale", 1.0)
    if scale == 0:
        raise ConfigError(f"tag {name!r}: scale must not be 0")
    offset = _tag_number(name, spec, "offset", 0.0)
    for key in ("description", "unit"):
        if key in spec and not isinstance(spec[key], str):
            raise ConfigError(f"tag {name!r}: {key} must be a string")
    tag = Tag(
        name=name,
        table=table,
        address=address,
        count=count,
        slave_id=slave_id,
        dtype=dtype,
        byteorder=byteorder,
        wordorder=wordorder,
        scale=scale,
        offset=offset,
        description=spec.get("description"),
        unit=spec.get("unit"),
    )
    if tag.address + tag.span > 65536:
        raise ConfigError(f"tag {name!r}: address {address} + {tag.span} runs past 65535")
    return tag


def parse_tag_map(data: Any) -> dict[str, Tag]:
    """Validate a decoded tag-map JSON object."""
    if not isinstance(data, Mapping):
        raise ConfigError("the tag map must be a JSON object of tag name -> definition")
    return {name: parse_tag(name, spec) for name, spec in data.items()}


def load_tag_map(path: str | None) -> dict[str, Tag]:
    """Load ``REGISTER_MAP_FILE``; no path means no tags. Any problem raises ConfigError."""
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"REGISTER_MAP_FILE {path!r} does not exist") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"REGISTER_MAP_FILE {path!r} cannot be read: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"REGISTER_MAP_FILE {path!r} is not valid JSON: {exc}") from None
    try:
        return parse_tag_map(data)
    except ConfigError as exc:
        raise ConfigError(f"REGISTER_MAP_FILE {path!r}: {exc}") from None
