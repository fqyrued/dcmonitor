"""Collector for the generators (DSE8610 MKII controllers, Modbus TCP).

Read-only: it uses dcmonitor.modbus_read, which implements only Modbus
function 3 and has no code for any write, so it cannot start, stop or change
a generator.

Only values confirmed on site are read:

    register 1027 (page 4, offset 3)   fuel level, percent
    register 1029 (page 4, offset 5)   battery voltage, tenths of a volt

Confirmed on GEN1 against DSE Scada Suite: 81 % fuel and 13.7 V battery read
as 81 and 137. Engine speed, frequency, mode and alarms have candidate
registers but are not read until a test run confirms them.

The controllers answer on any Modbus unit ID, so the ID does not protect
against a wrong IP. The unit IDs in config.toml match DSE Scada Suite's.

Run directly to test one generator:
    python -m dcmonitor.collectors.generator <IP> [unit]
"""

import sys

try:
    from ..modbus_read import ModbusError, read_registers
except ImportError:  # running the file directly rather than as a module
    from dcmonitor.modbus_read import ModbusError, read_registers

FIRST_REGISTER = 1024      # page 4, offset 0
REGISTER_COUNT = 6         # through offset 5
FUEL_OFFSET = 3
BATTERY_OFFSET = 5


class CollectorError(Exception):
    """The controller did not answer, or answered with something unusable."""


def read(ip: str, unit: int = 1, timeout: float = 5.0) -> dict:
    """Fuel level and battery voltage from one generator controller."""
    try:
        registers = read_registers(ip, FIRST_REGISTER, REGISTER_COUNT,
                                   unit=unit, timeout=timeout)
    except ModbusError as error:
        raise CollectorError(str(error)) from None

    values = {}
    fuel = registers[FUEL_OFFSET]
    # Above 100 is real: GEN3 reads 106 % when its sender is past the
    # calibrated full mark, as DSE Scada Suite also shows. Codes such as
    # 32767 for "no sensor" are still rejected.
    if 0 <= fuel <= 200:
        values["fuel_level_pct"] = float(fuel)
    battery = registers[BATTERY_OFFSET]
    if 0 <= battery <= 600:
        values["battery_v"] = battery / 10.0
    if not values:
        raise CollectorError(f"no plausible values in registers {registers}")
    return values


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("Usage: python -m dcmonitor.collectors.generator <IP> [unit]")
        return 2
    unit = int(sys.argv[2]) if len(sys.argv) == 3 else 1
    try:
        values = read(sys.argv[1], unit)
    except CollectorError as error:
        print(f"ERROR: {error}")
        return 1
    for name, value in values.items():
        print(f"  {name:16} {value:g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
