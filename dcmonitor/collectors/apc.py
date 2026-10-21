"""Collector for APC / Schneider Rack PDU 2G (AP8881) read directly over SNMP.

Read-only: SNMP GET only, through dcmonitor.snmp_v1, which has no SET. The
MIB's control and configuration objects (outlet commands, resets,
thresholds) are never touched; there is no code that could write them.

OIDs are from APC's PowerNet-MIB 4.6.0 (mibs/powernet460.mib
2 Oct 2026), rPDU2 branch, enterprises.318.1.1.26. On site: 20 AP8881
("Metered Rack PDU 2G", card firmware rpdu2g 1.2.0.4, sysObjectID
.318.1.3.4.6), three phases, no metered banks, 16 A per phase.

The PDUs are linked in pairs (APC Network Port Sharing): one network cable
to one PDU, a cable from it to the other. Each card answers for both:
every table holds a row per PDU, told apart by its "module" column, and the
rows are found by the PDU's name (rPDU2DeviceStatusName = the name in
pdus.toml). So a PDU is read by name, whichever card is asked. Checked 2 Oct
2026 on RACK15-PDU02's card against Power IQ: RACK15-PDU02 (module 2)
phases 21 / 33 / 8 tenths of A, 232 / 230 / 229 V, 125 hundredths of kW
against Power IQ's 2.1 / 3.3 / 0.8 A, 232 / 230 / 229 V, 1250 W; RACK15-PDU01
(module 1) 21 / 37 / 9, 132 against 2.1 / 3.7 / 0.9 A, 1320 W. Power IQ gives
VA = W and PF 1.0 for these; the PDU gives 143 hundredths of kVA, PF 0.88.

Configuration (read once, by describe()):
    .3.0                    rPDU2DeviceTableSize      2 (both PDUs of the pair)
    .5.0                    rPDU2PhaseTableSize       6
    .10.1.0                 rPDU2SensorTempHumidityTableSize
    .4.3.1.{2,3}.d          device row: module, name
    .4.2.1.12.d             rPDU2DevicePropertiesMaxPhaseCurrentRating, A (16)
    .4.2.1.9.d              rPDU2DevicePropertiesMaxCurrentRating, A (16)
    .2.1.8.d                rPDU2IdentModelNumber (AP8881)
    .6.3.1.{2,3}.p          phase row: module, phase number (1-3)
    .10.2.2.1.{2,3,5}.s     probe row: module, name, type (1 temperature,
                            2 temperature and humidity, 4 not installed)

Measurements (one GET per poll, by read()):
    .4.3.1.5.d              rPDU2DeviceStatusPower, hundredths of kW
    .4.3.1.16.d             rPDU2DeviceStatusApparentPower, hundredths of kVA
    .4.3.1.17.d             rPDU2DeviceStatusPowerFactor, hundredths
    .4.3.1.9.d              rPDU2DeviceStatusEnergy, tenths of kWh
    .6.3.1.5.p              rPDU2PhaseStatusCurrent, tenths of A
    .6.3.1.6.p              rPDU2PhaseStatusVoltage, V
    .10.2.2.1.{6,8,9}.s     probe comms (2 = OK), temperature in tenths of C,
                            temperature status (1 = not present)
    -1 means "not supported" for the power values.

The PDU has no breakers in SNMP (no metered banks): its three phases are
its circuits, labelled L1..L3 as Power IQ labels them, so the alarm and
history keys stay the same. Their limit is the PDU's phase rating. The
inlet current is the highest phase.

Run directly to test one PDU (name from pdus.toml, community from config.toml):
    python -m dcmonitor.collectors.apc RACK15-PDU02
    python -m dcmonitor.collectors.apc RACK15-PDU01 RACK15-PDU02
the second form reads RACK15-PDU01 through RACK15-PDU02's card.
"""

import sys
from dataclasses import dataclass

try:
    from ..snmp_v1 import SnmpError, snmp_get
    from .raritan_px2 import BreakerReading, CollectorError, PduReading, _from_project, _text
except ImportError:  # running the file directly rather than as a module
    from dcmonitor.snmp_v1 import SnmpError, snmp_get
    from dcmonitor.collectors.raritan_px2 import (BreakerReading, CollectorError, PduReading,
                                                  _from_project, _text)

RPDU2 = "1.3.6.1.4.1.318.1.1.26"
CHUNK = 20
BY_NAME = True              # main.py passes the PDU's name to describe()
PROBE_TYPES = (1, 2)        # temperatureOnly, temperatureHumidity
COMMS_OK = 2
NOT_PRESENT = 1


@dataclass(frozen=True)
class Layout:
    name: str
    model: str
    device: int             # the PDU's row in the device tables
    phase_rated: float | None
    inlet_rated: float | None
    phases: tuple           # (phase number, row) in phase order
    probes: tuple           # (row, name)

    @property
    def inlet_poles(self) -> int:
        return len(self.phases)


def _oid(*parts) -> str:
    return ".".join([RPDU2] + [str(part) for part in parts])


def _get(ip, oids, community, timeout, retries) -> dict:
    values = {}
    try:
        for start in range(0, len(oids), CHUNK):
            values.update(snmp_get(ip, oids[start:start + CHUNK], community=community,
                                   timeout=timeout, retries=retries, version="2c"))
    except SnmpError as error:
        raise CollectorError(str(error)) from None
    return values


def _count(value) -> int:
    return value if isinstance(value, int) and 0 < value <= 64 else 0


def describe(ip: str, community: str, timeout: float = 3.0, retries: int = 1,
             name: str | None = None) -> Layout:
    """Find the PDU called `name` among the PDUs this card answers for (the
    only one if no name is given), with its phases and probes."""
    sizes = _get(ip, [_oid(3, 0), _oid(5, 0), _oid(10, 1, 0)], community, timeout, retries)
    devices, phases, sensors = (_count(sizes.get(_oid(*parts)))
                                for parts in ((3, 0), (5, 0), (10, 1, 0)))
    if not devices:
        raise CollectorError("no rPDU2 device table: not an APC Rack PDU 2G?")

    oids = []
    for d in range(1, devices + 1):
        oids += [_oid(4, 3, 1, 2, d), _oid(4, 3, 1, 3, d), _oid(4, 2, 1, 12, d),
                 _oid(4, 2, 1, 9, d), _oid(2, 1, 8, d)]
    for p in range(1, phases + 1):
        oids += [_oid(6, 3, 1, 2, p), _oid(6, 3, 1, 3, p)]
    for s in range(1, sensors + 1):
        oids += [_oid(10, 2, 2, 1, column, s) for column in (2, 3, 5)]
    values = _get(ip, oids, community, timeout, retries)

    names = {d: _text(values.get(_oid(4, 3, 1, 3, d))) for d in range(1, devices + 1)}
    if name is None:
        if devices != 1:
            raise CollectorError(f"{devices} PDUs behind this card ({', '.join(names.values())}): "
                                 "give the PDU's name")
        device = 1
    else:
        found = [d for d, text in names.items() if text.strip().upper() == name.strip().upper()]
        if not found:
            raise CollectorError(f"no PDU named {name} on this card (it has: "
                                 f"{', '.join(names.values()) or 'none'})")
        device = found[0]
    module = values.get(_oid(4, 3, 1, 2, device))

    def amps(value):
        return float(value) if isinstance(value, int) and value > 0 else None

    rows = [(values.get(_oid(6, 3, 1, 3, p)), p) for p in range(1, phases + 1)
            if values.get(_oid(6, 3, 1, 2, p)) == module]
    probes = tuple((s, _text(values.get(_oid(10, 2, 2, 1, 3, s))))
                   for s in range(1, sensors + 1)
                   if values.get(_oid(10, 2, 2, 1, 2, s)) == module
                   and values.get(_oid(10, 2, 2, 1, 5, s)) in PROBE_TYPES)
    return Layout(
        name=names[device],
        model=_text(values.get(_oid(2, 1, 8, device))),
        device=device,
        phase_rated=amps(values.get(_oid(4, 2, 1, 12, device))),
        inlet_rated=amps(values.get(_oid(4, 2, 1, 9, device))),
        phases=tuple(sorted((number, row) for number, row in rows if isinstance(number, int))),
        probes=probes,
    )


def read(ip: str, community: str, layout: Layout, timeout: float = 3.0,
         retries: int = 1) -> PduReading:
    """The PDU's readings now, in one GET."""
    d = layout.device
    oids = [_oid(4, 3, 1, column, d) for column in (3, 5, 9, 16, 17)]
    for _number, row in layout.phases:
        oids += [_oid(6, 3, 1, 5, row), _oid(6, 3, 1, 6, row)]
    for row, _name in layout.probes:
        oids += [_oid(10, 2, 2, 1, column, row) for column in (6, 8, 9)]
    values = _get(ip, oids, community, timeout, retries)

    # The rows are by position in the card's tables; if the pair changed
    # (a PDU added or removed), the name tells, and the layout is re-read.
    if _text(values.get(_oid(4, 3, 1, 3, d))).strip().upper() != layout.name.strip().upper():
        raise CollectorError(f"{layout.name} is no longer row {d} on this card")

    def number(*parts, scale=1):
        value = values.get(_oid(*parts))
        return value / scale if isinstance(value, int) and value >= 0 else None

    lines = tuple(number(6, 3, 1, 5, row, scale=10) for _number, row in layout.phases)
    volts = [v for v in (number(6, 3, 1, 6, row) for _number, row in layout.phases)
             if v is not None]
    present = [current for current in lines if current is not None]
    breakers = tuple(BreakerReading(f"L{phase}", current, layout.phase_rated, phase, None)
                     for (phase, _row), current in zip(layout.phases, lines))
    temperatures = []
    for row, name in layout.probes:
        celsius = None
        if (values.get(_oid(10, 2, 2, 1, 6, row)) == COMMS_OK
                and values.get(_oid(10, 2, 2, 1, 9, row)) != NOT_PRESENT):
            raw = values.get(_oid(10, 2, 2, 1, 8, row))
            if isinstance(raw, int) and raw != -1:
                celsius = round(raw / 10, 1)
        temperatures.append((str(row), name or f"probe {row}", celsius))
    kw, kva = number(4, 3, 1, 5, d, scale=100), number(4, 3, 1, 16, d, scale=100)
    energy = number(4, 3, 1, 9, d, scale=10)
    return PduReading(
        name=layout.name, amps=max(present) if present else None,
        volts=volts[0] if volts else None,
        watts=None if kw is None else kw * 1000, va=None if kva is None else kva * 1000,
        power_factor=number(4, 3, 1, 17, d, scale=100),
        energy_wh=None if energy is None else energy * 1000, hz=None,
        inlet_rated=layout.inlet_rated, lines=lines if len(lines) > 1 else (),
        breakers=breakers, temperatures=tuple(temperatures), reading_utc=None,
    )


def main() -> int:
    if len(sys.argv) in (2, 3):
        name = sys.argv[1]
        ip, community = _from_project(sys.argv[2] if len(sys.argv) == 3 else name)
    else:
        print("Usage: python -m dcmonitor.collectors.apc <PDU name in pdus.toml> "
              "[<PDU whose card to ask>]")
        return 2
    try:
        layout = describe(ip, community, name=name)
        reading = read(ip, community, layout)
    except CollectorError as error:
        print(f"ERROR: {error}")
        return 1

    def show(value, unit, digits=1):
        return "--" if value is None else f"{value:.{digits}f} {unit}"

    print(f"{layout.name}  ({layout.model}), row {layout.device} on the card, "
          f"{layout.inlet_poles} phase(s), {show(layout.phase_rated, 'A', 0)} per phase")
    print(f"  current        {show(reading.amps, 'A', 1)}  (highest phase)")
    print(f"  voltage        {show(reading.volts, 'V', 0)}")
    print(f"  active power   {show(reading.watts, 'W', 0)}")
    print(f"  apparent power {show(reading.va, 'VA', 0)}")
    print(f"  power factor   {show(reading.power_factor, '', 2)}")
    print(f"  energy         {show(None if reading.energy_wh is None else reading.energy_wh / 1000, 'kWh', 1)}")
    for breaker in reading.breakers:
        print(f"  phase {breaker.label:8} {show(breaker.current, 'A', 1):>7} of "
              f"{show(breaker.rated, 'A', 0)}")
    if not reading.temperatures:
        print("  temperature    no probe connected")
    for _row, name, celsius in reading.temperatures:
        print(f"  temperature    {name}: {show(celsius, 'C') if celsius is not None else 'unavailable'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
