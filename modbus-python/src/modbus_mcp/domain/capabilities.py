"""Backend support is distinct from device capability and authorization."""

from typing import Any


def capability_report(
    *, writes_enabled: bool, system_commands_enabled: bool = False, transport: str = "tcp"
) -> dict[str, Any]:
    operations = {
        "read_values": {"supported": True, "enabled": True},
        "write_values": {"supported": True, "enabled": writes_enabled},
        "mask_write_register": {"supported": True, "enabled": writes_enabled},
        "read_device_information": {"supported": True, "enabled": True},
        "list_tags": {"supported": True, "enabled": True},
        "ping": {"supported": True, "enabled": True},
        "get_connection_status": {"supported": True, "enabled": True},
    }
    for operation in operations.values():
        if not operation["enabled"]:
            operation["reason"] = "Disabled by native configuration gates"
    return {
        "protocol": "modbus",
        "backend": "pymodbus",
        "simulated": False,
        "scope": "backend_support",
        "device_verified": False,
        "operations": operations,
        "prerequisites": [
            "A configured TCP/UDP endpoint or serial interface and correct unit ID are required.",
            "Device register maps supply types, units and scaling; the protocol cannot infer them.",
        ],
        "limitations": [
            "TLS/Modbus Security and native subscriptions are not implemented.",
            "Discovery is configured endpoint access, not a network scan.",
            "Reads split across PDUs are not atomic snapshots; "
            "a write acknowledgement does not verify a physical process.",
        ],
        "authorization": "enabled reflects native configuration only; "
        "MCP policy and device permissions still apply",
        "transport": transport,
    }
