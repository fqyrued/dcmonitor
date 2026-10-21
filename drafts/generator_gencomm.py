"""Collector for the generators (DSE8610 MKII controllers, Modbus TCP).

Read-only: it uses dcmonitor.modbus_read, which implements only Modbus
function 3 and has no code for any write, so it cannot start, stop or change
a generator.

Registers are from DSE's "GenComm standard for use with generating set
control equipment" (version 2.228, 16/07/2020). Register = page * 256 +
offset; 32-bit values are two registers, most significant first.

    page 4 (1024+)   basic instrumentation, offsets 0-33:
        0 oil pressure kPa, 1 coolant C (signed), 2 oil temperature C
        (signed), 3 fuel %, 4 charge alternator 0.1 V, 5 battery 0.1 V,
        6 engine speed RPM, 7 generator frequency 0.1 Hz, 8-13 generator
        L1-N..L3-N 0.1 V (32-bit), 20-25 generator L1..L3 current 0.1 A
        (32-bit), 28-33 generator L1..L3 watts (32-bit signed)
    page 3 (768+)    offset 4 control mode, offset 6 alarm flags
    page 6 (1536+)   derived instrumentation, "8xxx/7xxx/6xxx/P100/L40x/4xxx
        register allocation" table, offsets 0-22: 0-1 generator total watts
        (32-bit signed), 21 average power factor 0.01 (signed), 22 generator
        percentage of full power 0.1 % (signed)

Confirmed on site: fuel (81 read as 81) and battery (137 = 13.7 V) against
DSE Scada Suite on GEN1, mode 1 = Auto on GEN1, flags 512 = telemetry flag
only. Engine speed, frequency, volts, amps and watts read 0 while stopped,
as the standard says they should; their running values are still to be
compared with Scada Suite at a test run.

Instrument values at the top of their range are GenComm's "no value" codes
(0x7FFF unimplemented, 0x7FFD under measurable range, ...) and are dropped.
The GEN coolant input reads 0x7FFD and oil temperature 0x7FFF.

The controllers answer on any Modbus unit ID (GenComm fixes the host
controller at address 10 over TCP), so the ID does not protect against a
wrong IP. The IPs and unit IDs in config.toml match DSE Scada Suite's.

Run directly to test one generator:
    python -m dcmonitor.collectors.generator <IP> <unit number, e.g. 11>
"""

import sys

try:
    from ..modbus_read import ModbusError, read_registers
except ImportError:  # running the file directly rather than as a module
    from dcmonitor.modbus_read import ModbusError, read_registers

PAGE4_START = 1024         # page 4, offset 0
PAGE4_COUNT = 34           # offsets 0-33: through generator L3 watts
PAGE3_START = 772          # page 3, offset 4: control mode
PAGE3_COUNT = 3            # offsets 4-6: mode, mode selection, alarm flags
PAGE6_START = 1536         # page 6, offset 0
PAGE6_COUNT = 23           # offsets 0-22: through percentage of full power

# What a GenComm "no value" code means, for showing instead of a number.
NO_VALUE_16_SIGNED = {0x7FFF: "not fitted", 0x7FFE: "above range", 0x7FFD: "below range",
                      0x7FFC: "sensor fault", 0x7FFB: "bad data"}

CONTROL_MODES = {
    0: "Stop", 1: "Auto", 2: "Manual", 3: "Test on load",
    4: "Auto, manual restore", 5: "User configuration", 6: "Test off load", 7: "Off",
}

# Page 3 offset 6: bit n/16 of the standard is value bit n - 1 (16/16 = MSB).
FLAG_BITS = {
    "not_configured": 16,
    "control_unit_failure": 14,
    "shutdown": 13,
    "electrical_trip": 12,
    "warning": 11,
    "telemetry": 10,
    "controlled_shutdown": 8,
}


class CollectorError(Exception):
    """The controller did not answer, or answered with something unusable."""


def _unsigned16(value, low, high, scale=1.0):
    """None for a GenComm "no value" code (0xFFF8 and up) or out of range."""
    if value >= 0xFFF8 or not low <= value <= high:
        return None
    return value * scale


def _signed16(value, low, high, scale=1.0):
    if value >= 0x8000:
        value -= 0x10000
    if 0x7FF8 <= value <= 0x7FFF or not low <= value <= high:
        return None
    return value * scale


def _unsigned32(registers, offset, high, scale=1.0):
    value = (registers[offset] << 16) | registers[offset + 1]
    if value >= 0xFFFFFFF8 or value > high:
        return None
    return value * scale


def _signed32(registers, offset, limit, scale=1.0):
    value = (registers[offset] << 16) | registers[offset + 1]
    if value >= 0x80000000:
        value -= 0x100000000
    if 0x7FFFFFF8 <= value <= 0x7FFFFFFF or abs(value) > limit:
        return None
    return value * scale


def read(ip: str, unit: int = 1, timeout: float = 5.0) -> dict:
    """One generator: {"metrics": {...}, "states": {...}}.

    metrics are numbers for the history; states are the control mode and the
    controller's alarm flags. The page 3 read is allowed to fail on its own,
    so fuel and battery keep working if a controller refuses it.
    """
    try:
        registers = read_registers(ip, PAGE4_START, PAGE4_COUNT, unit=unit, timeout=timeout)
    except ModbusError as error:
        raise CollectorError(str(error)) from None

    metrics = {}

    def put(name, value):
        if value is not None:
            metrics[name] = round(value, 3)

    put("oil_pressure_kpa", _unsigned16(registers[0], 0, 10000))
    put("coolant_temp_c", _signed16(registers[1], -50, 200))
    coolant_note = NO_VALUE_16_SIGNED.get(registers[1])
    put("oil_temp_c", _signed16(registers[2], -50, 200))
    # Above 100 is real: GEN3 reads 101-106 % when its sender is past the
    # calibrated full mark, as DSE Scada Suite also shows.
    put("fuel_level_pct", _unsigned16(registers[3], 0, 200))
    put("charge_alt_v", _unsigned16(registers[4], 0, 400, 0.1))
    put("battery_v", _unsigned16(registers[5], 0, 400, 0.1))
    put("engine_speed_rpm", _unsigned16(registers[6], 0, 6000))
    put("generator_frequency_hz", _unsigned16(registers[7], 0, 700, 0.1))
    for phase in (1, 2, 3):
        put(f"generator_voltage_v_l{phase}",
            _unsigned32(registers, 8 + 2 * (phase - 1), 18000, 0.1))
        put(f"generator_current_a_l{phase}",
            _unsigned32(registers, 20 + 2 * (phase - 1), 999999, 0.1))
    watts = [_signed32(registers, 28 + 2 * (phase - 1), 99999999) for phase in (1, 2, 3)]
    if any(value is not None for value in watts):
        put("generator_kw", sum(value or 0 for value in watts) / 1000)

    if "fuel_level_pct" not in metrics and "battery_v" not in metrics:
        raise CollectorError(f"no plausible fuel or battery value in {registers[:6]}")

    states = {}
    if coolant_note:
        states["coolant_note"] = coolant_note

    # Derived values: total power, power factor and load as a share of the
    # set's rating as configured in the controller. Optional like page 3.
    try:
        derived = read_registers(ip, PAGE6_START, PAGE6_COUNT, unit=unit, timeout=timeout)
    except ModbusError as error:
        states["derived_error"] = str(error)
    else:
        total = _signed32(derived, 0, 99999999)
        if total is not None:
            metrics["generator_kw"] = round(total / 1000, 3)
        put("generator_power_factor", _signed16(derived[21], -100, 100, 0.01))
        put("generator_load_pct", _signed16(derived[22], -9999, 9999, 0.1))

    try:
        status = read_registers(ip, PAGE3_START, PAGE3_COUNT, unit=unit, timeout=timeout)
    except ModbusError as error:
        states["status_error"] = str(error)
    else:
        mode = status[0]
        states["control_mode_code"] = mode
        states["control_mode"] = CONTROL_MODES.get(mode, "unknown" if mode == 0xFFFF
                                                   else f"code {mode}")
        flags = status[2]
        for name, bit in FLAG_BITS.items():
            states[f"flag_{name}"] = bool(flags & (1 << (bit - 1)))
    return {"metrics": metrics, "states": states}


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("Usage: python -m dcmonitor.collectors.generator <IP> <unit number, e.g. 11>")
        return 2
    try:
        unit = int(sys.argv[2]) if len(sys.argv) == 3 else 1
    except ValueError:
        print(f"unit must be the Modbus unit number (11, 12, 13), not {sys.argv[2]!r}")
        return 2
    try:
        data = read(sys.argv[1], unit)
    except CollectorError as error:
        print(f"ERROR: {error}")
        return 1
    for name, value in data["metrics"].items():
        print(f"  {name:24} {value:g}")
    for name, value in data["states"].items():
        print(f"  {name:24} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
