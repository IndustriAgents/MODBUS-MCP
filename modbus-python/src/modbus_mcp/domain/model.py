import decimal
import logging
import math
from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import Field

from .. import codec
from ..config import Tag
from ..state import AppContext

log = logging.getLogger("modbus_mcp")
WRITES_DISABLED = (
    "Writes are disabled by configuration (set MODBUS_WRITES_ENABLED=true to allow them)"
)
WRITE_NO_REPLY = "no reply; the write may have been applied; check before retrying"
WRITE_REPLY_MISMATCH = (
    "the device's reply does not match the write; the"
    " write may have been applied; check before "
    "retrying"
)
READ_REPLY_MISMATCH = "the device's reply does not match the request"
STARTUP_CONNECT_TIMEOUT = 1.0
WRITE_TOOLS = frozenset(
    {
        "write_register",
        "write_registers",
        "write_coil",
        "write_coils_bulk",
        "mask_write_register",
        "write_tag",
    }
)
MAY_HAVE_BEEN_APPLIED = "the write may have been applied; check before retrying"
REFUSED = frozenset({1, 2, 3, 6, 10})
MAX_READ_REGISTERS = 125
MAX_READ_BITS = 2000
MAX_WRITE_REGISTERS = 123
MAX_WRITE_COILS = 1968
ADDRESS_SPACE = 65536
_EXCEPTION_NAMES = {
    1: "Illegal Function",
    2: "Illegal Data Address",
    3: "Illegal Data Value",
    4: "Server Device Failure",
    5: "Acknowledge",
    6: "Server Device Busy",
    8: "Memory Parity Error",
    10: "Gateway Path Unavailable",
    11: "Gateway Target Device Failed to Respond",
}


def _int(lo: int, hi: int, description: str) -> Any:
    return Annotated[int, Field(ge=lo, le=hi, strict=True, description=description)]


StrictBool = Annotated[bool, Field(strict=True)]
Address = _int(0, 65535, "Zero-based Modbus address (0-65535).")
RegisterValue = _int(0, 65535, "Unsigned 16-bit register value (0-65535).")
Mask = _int(0, 65535, "16-bit mask (0-65535).")
RegisterCount = _int(1, ADDRESS_SPACE, "Number of registers to read (at least 1).")
BitCount = _int(1, ADDRESS_SPACE, "Number of bits to read (at least 1).")
TypedCount = _int(1, ADDRESS_SPACE, "Number of typed values to decode (at least 1).")
SlaveId = Annotated[
    Annotated[int, Field(ge=0, le=255, strict=True)] | None,
    Field(description="Modbus unit/slave ID (0-255); omit to use MODBUS_DEFAULT_SLAVE_ID."),
]
DType = Annotated[
    Literal["int16", "uint16", "int32", "uint32", "int64", "uint64", "float32", "float64"],
    Field(description="Value type; 16-bit types use 1 register, 32-bit 2, 64-bit 4."),
]
ByteOrder = Annotated[
    Literal["big", "little"],
    Field(description="Byte order inside each register ('big' is the Modbus default)."),
]
WordOrder = Annotated[
    Literal["big", "little"],
    Field(description="Register order for values wider than 16 bits ('big' = high word first)."),
]
Scale = Annotated[
    float,
    Field(strict=True, allow_inf_nan=False, description="Multiply each decoded value by this."),
]
Offset = Annotated[
    float,
    Field(strict=True, allow_inf_nan=False, description="Then add this to each decoded value."),
]


def _make_result(
    success: bool, data: Any = None, error: str | None = None, meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {"success": success, "data": data, "error": error, "meta": meta or {}}


def _not_sent(error: str, meta: dict[str, Any]) -> dict[str, Any]:
    """A write refused before anything was sent to the device."""
    return _make_result(
        False, error=error, meta={**meta, "outcome": "not_sent", "request_sent": False}
    )


def _slave(app: AppContext, slave_id: int | None) -> int:
    return app.settings.default_slave_id if slave_id is None else slave_id


def _is_error(result: Any) -> bool:
    return bool(hasattr(result, "isError") and result.isError())


def _device_error(result: Any) -> tuple[str, dict[str, Any]]:
    """Describe a Modbus exception response, e.g. 'Modbus exception 2 (Illegal Data Address)'."""
    code = getattr(result, "exception_code", None)
    if not isinstance(code, int) or not code:
        return (f"Device error: {result}", {})
    message = f"Modbus exception {code}"
    if code in _EXCEPTION_NAMES:
        message += f" ({_EXCEPTION_NAMES[code]})"
    function_code = getattr(result, "function_code", None)
    if isinstance(function_code, int):
        message += f" for function 0x{function_code & 127:02X}"
    return (message, {"exception_code": code})


def _span_error(address: int, span: int) -> str | None:
    if address + span > ADDRESS_SPACE:
        return f"address {address} + {span} runs past the last Modbus address (65535)"
    return None


def reply_check(
    function_code: int,
    *,
    registers: int | None = None,
    bits: int | None = None,
    echo: dict[str, Any] | None = None,
    raw: tuple[bytes, Callable[[], bytes] | None] | None = None,
) -> Callable[[Any], dict[str, Any] | None]:
    """A check that a reply belongs to the request: what is wrong with it, or None.

    - an exception reply must carry this request's function code (+ 0x80);
    - a normal reply must have the function code, exactly `registers` registers or
      `bits` bits (padded to whole bytes, as on the wire), and echo the fields in
      `echo` (Modbus write replies repeat the request);
    - `raw` = (expected PDU, provider of the raw reply bytes): the reply frame must
      end with that PDU (for TCP/UDP) or with it followed by the CRC (RTU). It is for
      replies the decoded object cannot show exactly (pymodbus decodes any non-zero
      FC 5 value as ON).

    Modbus RTU has no transaction id, so a reply to an earlier request (a late one,
    or one for another unit) can otherwise be taken for this request's.
    """

    def check(response: Any) -> dict[str, Any] | None:
        got = getattr(response, "function_code", None)
        if _is_error(response):
            if not isinstance(got, int) or got & 127 != function_code:
                return {"function_code": {"expected": function_code | 128, "got": got}}
            return None
        problems: dict[str, Any] = {}
        if got != function_code:
            problems["function_code"] = {"expected": function_code, "got": got}
        if registers is not None:
            count = len(getattr(response, "registers", None) or [])
            if count != registers:
                problems["registers"] = {"expected": registers, "got": count}
        if bits is not None:
            count = len(getattr(response, "bits", None) or [])
            if count != 8 * ((bits + 7) // 8):
                problems["bits"] = {"expected": 8 * ((bits + 7) // 8), "got": count}
        if echo:
            problems.update(_echo_mismatch(response, echo))
        if raw is not None and raw[1] is not None:
            pdu, frame = (raw[0], bytes(raw[1]()))
            if not (frame.endswith(pdu) or frame[:-2].endswith(pdu)):
                problems["raw_reply"] = {"expected_pdu": pdu.hex(), "got": frame.hex()}
        return problems or None

    return check


def _echo_mismatch(response: Any, expected: dict[str, Any]) -> dict[str, Any]:
    """Fields of a write reply that do not echo the request (Modbus write replies do).

    `expected` maps response attributes to values; for the lists `registers` and
    `bits` only the leading elements are compared.
    """
    mismatch: dict[str, Any] = {}
    for name, want in expected.items():
        got = getattr(response, name, None)
        if isinstance(want, list):
            got = list(got)[: len(want)] if isinstance(got, (list, tuple)) else got
        if got != want:
            mismatch[name] = {"expected": want, "got": got}
    return mismatch


def _json_number(value: Any) -> Any:
    """JSON has no NaN or infinity: spell a non-finite float out rather than emit invalid JSON."""
    if isinstance(value, float) and (not math.isfinite(value)):
        return "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
    return value


def _apply_scale(values: list[Any], scale: float, offset: float) -> list[Any]:
    """Apply `value * scale + offset`, leaving raw values untouched when that is the identity.

    Converting unscaled integers to float would silently lose precision for 64-bit values.
    """
    if scale != 1 or offset != 0:
        values = [v * float(scale) + float(offset) for v in values]
    return [_json_number(v) for v in values]


def _unknown_tag(name: str) -> dict[str, Any]:
    return _make_result(
        False, error=f"Unknown tag {name!r}; list_tags shows the defined tags", meta={"tag": name}
    )


def _decimal(value: int | float) -> decimal.Decimal:
    """The number as written: an int exactly, a float by its shortest repr (0.1 -> 0.1)."""
    return decimal.Decimal(value) if isinstance(value, int) else decimal.Decimal(repr(value))


def _approx(d: decimal.Decimal) -> str:
    """`d` for a message, to about 20 significant digits, with "..." when digits were
    cut (1/3 -> 0.33333333333333333333...). Plain notation with at least 3 decimals,
    or scientific notation above 1e20, beyond any 64-bit value."""
    if d.adjusted() >= 20:
        text = f"{d:.19e}"
    else:
        text = f"{d:.{max(19 - d.adjusted(), 3)}f}"
    if decimal.Decimal(text) != d:
        return text + "..."
    return text if "e" in text else text.rstrip("0").rstrip(".")


def _unscale(values: list[Any], tag: Tag) -> list[Any]:
    """Turn engineering values back into raw values: (value - offset) / scale.

    read_tag returns raw * scale + offset, so write_tag accepts the same units. The
    arithmetic is decimal, not float: for an integer dtype the raw value must come
    out exactly whole, so 25.35 on a tag with scale 0.1, or 1000000000 on a tag with
    offset 0.5, is refused rather than rounded, and a 64-bit value is not squeezed
    through a float's 53 bits.
    """
    if tag.scale == 1 and tag.offset == 0:
        return values
    raw: list[Any] = []
    with decimal.localcontext() as context:
        context.prec = 120
        scale, offset = (_decimal(tag.scale), _decimal(tag.offset))
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise codec.CodecError(f"value must be a number, got {value!r}")
            if isinstance(value, float) and (not math.isfinite(value)):
                raise codec.CodecError(f"value must be finite, got {value!r}")
            r = (_decimal(value) - offset) / scale
            if tag.dtype in ("float32", "float64"):
                raw.append(float(r))
            elif r != r.to_integral_value():
                raise codec.CodecError(
                    f"{value!r}"
                    " is not a whole number of steps of scale "
                    f"{tag.scale}"
                    " (offset "
                    f"{tag.offset}"
                    "); the raw value would be "
                    f"{_approx(r)}"
                )
            else:
                raw.append(int(r))
    return raw
