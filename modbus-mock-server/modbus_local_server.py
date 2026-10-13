"""A simulated Modbus TCP device for testing modbus-mcp without real equipment.

The register map is documented in README.md; the initial values below are at
the addresses it lists. Runs on pymodbus 3.11.1-3.12 (see pyproject.toml).
"""

import argparse
import asyncio
import contextlib
import logging
import os
import random
import sys
import threading

from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.pdu.device import ModbusDeviceIdentification
from pymodbus.server import StartAsyncTcpServer

log = logging.getLogger("modbus_mock")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 1502
DEFAULT_UPDATE_INTERVAL = 1.0

# Entries per table: protocol addresses 0..99.
TABLE_SIZE = 100

# Function codes used to address each table through the device context.
COILS, DISCRETE_INPUTS, HOLDING_REGISTERS, INPUT_REGISTERS = 1, 2, 3, 4

INITIAL_HOLDING = [
    50,  # 0: valve position (0-100)
    0,  # 1: heater power (0-100)
    0,  # 2: fan speed (0-100)
    0,  # 3: conveyor speed (0-100)
    0,  # 4: production rate setpoint (units/hour)
    0,  # 5: system mode (0=MANUAL, 1=AUTO, 2=MAINT)
    0,  # 6: command start
    0,  # 7: command stop
    0,  # 8: command emergency stop
    0,  # 9: command reset
]
INITIAL_COILS = [
    0,  # 0: pump enabled
    0,  # 1: alarm active (written by the simulation)
]


def clamp_int(value: float, min_v: int = 0, max_v: int = 0xFFFF) -> int:
    return max(min_v, min(max_v, int(value)))


def to_reg(value: float, scale: float = 1.0, min_v: int = 0, max_v: int = 0xFFFF) -> int:
    return clamp_int(round(value * scale), min_v, max_v)


INITIAL_INPUTS = [
    to_reg(25.0, 10),  # 0: temperature x10
    to_reg(1013.0, 1),  # 1: pressure (hPa)
    to_reg(0.0, 1),  # 2: flow rate (L/min)
    to_reg(75.0, 10),  # 3: tank level x10
    to_reg(0.3, 100),  # 4: vibration x100
    to_reg(7.2, 100),  # 5: pH x100
    to_reg(45.0, 10),  # 6: humidity x10
    to_reg(0.0, 1),  # 7: motor speed (RPM)
    to_reg(0.0, 1),  # 8: total production (wraps at 65536)
]
INITIAL_DISCRETE = [
    0,  # 0: emergency stop active
    0,  # 1: system running
]


def _block() -> ModbusSequentialDataBlock:
    # ModbusDeviceContext adds 1 to every protocol address before it reaches a
    # block, so a block that starts at 1 serves protocol addresses 0..TABLE_SIZE-1.
    return ModbusSequentialDataBlock(1, [0] * TABLE_SIZE)


def build_context() -> ModbusServerContext:
    """Create the datastore, with the initial values at their documented addresses."""
    device = ModbusDeviceContext(di=_block(), co=_block(), hr=_block(), ir=_block())
    # Seed through the device context, i.e. through the same address mapping a
    # client request goes through. (Writing to the blocks directly put every
    # value one address too low: Valve Position read 0 instead of 50.)
    for function_code, values in (
        (HOLDING_REGISTERS, INITIAL_HOLDING),
        (COILS, INITIAL_COILS),
        (INPUT_REGISTERS, INITIAL_INPUTS),
        (DISCRETE_INPUTS, INITIAL_DISCRETE),
    ):
        if device.setValues(function_code, 0, list(values)) is not None:
            raise RuntimeError(f"could not seed table for function code {function_code}")
    # With single=True pymodbus wants the device context itself; a {0: device}
    # dict gets wrapped again, and every request then fails with exception 4.
    return ModbusServerContext(devices=device, single=True)


def _step(context: ModbusServerContext, state: dict) -> None:
    """Advance the simulation by one tick: process commands, update sensors."""
    device = context[0]  # single=True: every unit id maps to the same device

    # Read controls / setpoints
    pump_enabled = bool(device.getValues(COILS, 0, count=1)[0])
    vals_hr = device.getValues(HOLDING_REGISTERS, 0, count=10)
    valve_pos = clamp_int(vals_hr[0], 0, 100)
    heater_power = clamp_int(vals_hr[1], 0, 100)
    conveyor_speed = clamp_int(vals_hr[3], 0, 100)
    prod_rate_sp = clamp_int(vals_hr[4], 0, 1000)
    system_mode = clamp_int(vals_hr[5], 0, 2)  # 0=MANUAL,1=AUTO,2=MAINT
    cmd_start, cmd_stop, cmd_e_stop, cmd_reset = (v > 0 for v in vals_hr[6:10])

    # Process commands, clearing each one once handled
    if cmd_start:
        system_mode = 1  # AUTO
        pump_enabled = True
        conveyor_speed = max(conveyor_speed, min(prod_rate_sp * 2, 100))
        device.setValues(HOLDING_REGISTERS, 6, [0])
    if cmd_stop:
        system_mode = 0  # MANUAL
        pump_enabled = False
        conveyor_speed = 0
        device.setValues(HOLDING_REGISTERS, 7, [0])
    if cmd_e_stop:
        system_mode = 2  # MAINT
        pump_enabled = False
        conveyor_speed = 0
        device.setValues(HOLDING_REGISTERS, 8, [0])
    if cmd_reset:
        system_mode = 0  # MANUAL
        state["total_production"] = 0.0
        device.setValues(HOLDING_REGISTERS, 9, [0])

    # Apply possibly updated controls back to the stores
    device.setValues(COILS, 0, [1 if pump_enabled else 0])
    device.setValues(HOLDING_REGISTERS, 3, [conveyor_speed])
    device.setValues(HOLDING_REGISTERS, 5, [system_mode])

    # Discrete inputs (status)
    di_vals = device.getValues(DISCRETE_INPUTS, 0, count=2)
    emergency_stop = di_vals[0] == 1 or (system_mode == 2)
    running = (system_mode == 1) and not emergency_stop and (prod_rate_sp > 0)

    # Temperature influenced by heater power
    temperature = 25.0 + (heater_power * 0.5) + random.uniform(-2.0, 2.0)

    # Pressure influenced by pump and valve
    base_pressure = 1013.0 + (50.0 if pump_enabled else 0.0) - (valve_pos - 50.0) * 0.5
    pressure = base_pressure + random.uniform(-5.0, 5.0)

    # Flow rate influenced by pump and valve
    base_flow = 150.0 * (valve_pos / 100.0) if pump_enabled else 0.0
    flow_rate = max(0.0, base_flow + random.uniform(-10.0, 10.0))

    # Tank level dynamics
    tank_level = device.getValues(INPUT_REGISTERS, 3, count=1)[0] / 10.0
    level_change = (flow_rate - prod_rate_sp) * 0.01
    tank_level = max(0.0, min(100.0, tank_level + level_change + random.uniform(-0.5, 0.5)))

    # Vibration influenced by conveyor and production
    base_vibration = (conveyor_speed / 100.0) * 0.2 + (prod_rate_sp / 100.0) * 0.2
    vibration = base_vibration + random.uniform(-0.1, 0.1)

    # pH with slow drift
    ph_level = device.getValues(INPUT_REGISTERS, 5, count=1)[0] / 100.0
    ph_level = max(6.0, min(8.0, ph_level + random.uniform(-0.05, 0.05)))

    # Humidity influenced by temperature
    base_humidity = 45.0 - (temperature - 25.0) * 2.0
    humidity = max(20.0, min(80.0, base_humidity + random.uniform(-3.0, 3.0)))

    # Motor speed influenced by production
    motor_speed = 0.0
    if running:
        motor_speed = 1500.0 + (prod_rate_sp * 10.0) + random.uniform(-50.0, 50.0)

    # Production accumulation
    if running and not emergency_stop:
        state["total_production"] += prod_rate_sp / 3600.0

    # Alarms
    alarm_active = temperature > 80.0 or pressure > 1200.0 or tank_level < 10.0 or vibration > 2.0

    if temperature > 100.0 or pressure > 1300.0:
        emergency_stop = True
        running = False

    # Write updated status back
    device.setValues(DISCRETE_INPUTS, 0, [1 if emergency_stop else 0])
    device.setValues(DISCRETE_INPUTS, 1, [1 if running else 0])
    device.setValues(COILS, 1, [1 if alarm_active else 0])

    # Input registers (sensors)
    device.setValues(
        INPUT_REGISTERS,
        0,
        [
            to_reg(temperature, 10),
            to_reg(pressure, 1),
            to_reg(flow_rate, 1),
            to_reg(tank_level, 10),
            to_reg(vibration, 100),
            to_reg(ph_level, 100),
            to_reg(humidity, 10),
            to_reg(motor_speed, 1),
            to_reg(state["total_production"], 1) % 65536,
        ],
    )


async def simulation_loop(
    context: ModbusServerContext, interval: float = DEFAULT_UPDATE_INTERVAL
) -> None:
    """Background loop to update sensor values and process commands."""
    state = {"total_production": 0.0}
    last_error = None
    while True:
        try:
            _step(context, state)
            if last_error is not None:
                log.info("Simulation recovered")
                last_error = None
        except Exception as exc:
            # Keep simulating, but say why a step failed (once per distinct error):
            # swallowing these silently is how a broken datastore went unnoticed.
            error = f"{type(exc).__name__}: {exc}"
            if error != last_error:
                log.exception("Simulation step failed; retrying every %ss", interval)
                last_error = error
        await asyncio.sleep(interval)


def _stop_at_stdin_eof(loop: asyncio.AbstractEventLoop, task: asyncio.Task) -> None:
    """Block until stdin reaches end of file, then cancel `task` (the server)."""
    with contextlib.suppress(Exception):
        while sys.stdin.buffer.read(4096):
            pass
    loop.call_soon_threadsafe(task.cancel)


async def run_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    interval: float = DEFAULT_UPDATE_INTERVAL,
    exit_on_stdin_eof: bool = False,
) -> None:
    if exit_on_stdin_eof:
        # Whoever started us holds the other end of stdin; when it exits, even
        # by SIGKILL, the pipe closes and the mock stops instead of lingering.
        threading.Thread(
            target=_stop_at_stdin_eof,
            args=(asyncio.get_running_loop(), asyncio.current_task()),
            daemon=True,
        ).start()
    context = build_context()

    identity = ModbusDeviceIdentification()
    identity.VendorName = "Mock ICS"
    identity.ProductCode = "MODBUS-MOCK"
    identity.VendorUrl = "https://example.com"
    identity.ProductName = "Modbus Mock Server"
    identity.ModelName = "ModbusSim v0.1"
    identity.MajorMinorRevision = "0.1"

    sim_task = asyncio.create_task(simulation_loop(context, interval))
    try:
        await StartAsyncTcpServer(context=context, identity=identity, address=(host, port))
    finally:
        sim_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sim_task


def _settings_from_env() -> tuple[str, int, float]:
    host = os.environ.get("MODBUS_HOST", "").strip() or DEFAULT_HOST
    try:
        port = int(os.environ.get("MODBUS_PORT", "").strip() or DEFAULT_PORT)
        interval = float(os.environ.get("UPDATE_INTERVAL", "").strip() or DEFAULT_UPDATE_INTERVAL)
    except ValueError as exc:
        raise SystemExit(f"modbus-mock-server: invalid setting: {exc}") from None
    if not 1 <= port <= 65535:
        raise SystemExit(f"modbus-mock-server: MODBUS_PORT must be 1-65535, got {port}")
    if not interval > 0:
        raise SystemExit(f"modbus-mock-server: UPDATE_INTERVAL must be > 0, got {interval}")
    return host, port, interval


def main(argv: list[str] | None = None) -> None:
    """Entry point of `modbus-mock-server`. Settings come from the environment."""
    parser = argparse.ArgumentParser(
        prog="modbus-mock-server",
        description="Simulated Modbus TCP device. Settings: MODBUS_HOST, MODBUS_PORT, "
        "UPDATE_INTERVAL.",
    )
    parser.add_argument(
        "--exit-on-stdin-eof",
        action="store_true",
        help="stop when stdin is closed (for test harnesses: the mock then cannot outlive "
        "the process that started it)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s"
    )
    host, port, interval = _settings_from_env()
    log.info("Starting Modbus Mock Server on %s:%s ...", host, port)
    try:
        asyncio.run(run_server(host, port, interval, args.exit_on_stdin_eof))
    except KeyboardInterrupt:
        log.info("Server stopped by user.")
    except asyncio.CancelledError:
        log.info("stdin closed; server stopped.")


if __name__ == "__main__":
    main()
