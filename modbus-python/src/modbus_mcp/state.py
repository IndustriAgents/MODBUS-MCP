import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import Settings, Tag
from .domain.ports import ModbusClientPort


@dataclass
class AppContext:
    """Per-server state, created by the lifespan once the device is connected."""

    modbus_client: ModbusClientPort
    settings: Settings = field(default_factory=Settings)
    tags: dict[str, Tag] = field(default_factory=dict)
    reconnect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    frames_sent: Callable[[], int] | None = None
    closing: Callable[[], bool] = lambda: False
    last_reply: Callable[[], bytes] | None = None
    quiet_until: float = 0.0


@dataclass
class OperationContext:
    app: AppContext
    info: Callable
    error: Callable
