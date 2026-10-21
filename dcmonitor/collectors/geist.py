"""Collector for Geist / Vertiv rack PDUs read directly over SNMP (VERTIV-V5-MIB).

Read-only: SNMP GET and GETNEXT through dcmonitor.snmp_v1, which has no SET.

OIDs are from Vertiv's VERTIV-V5-MIB (revision 2021-03-11, the geist_v5.mib
used for the probes in pdu_sensor.py), under enterprises.21239.5.2. On site
(2 Oct 2026): 48 MG03E4B1-26IJ63-3PS6B2A10-S ("Geist IMD3", firmware 5.9.0,
sysObjectID .21239.42.1.53, one phase) and 4 MG05M4W1-42ITB3-3PS56B2A10-S
("Vertiv IMD5", firmware 6.3.0, .21239.42.1.55, three phases). Every table
has a single index. Checked against Power IQ the same day: RACK13-PDU02
total 638 W / 712 VA / 90 % against 639 W, 712 VA, PF 0.9; phase A 2274
decivolts and 313 centiamps against 227.3 V and 3.1 A; breakers 145 and 177
centiamps against 1.5 and 1.8 A; RACK14-PDU01 phase voltages 2412 / 2288 /
2301 against Power IQ's poles 1-3 241.2 / 228.8 / 230.1 V.

Configuration (read once, by describe()):
    .1.7.0                temperatureUnits: 0 = F, 1 = C
    .1.8.0                productModelNumber
    .3.1.1.4.1            pduMainLabel (the PDU name on site)
    .3.2.1.2.p            phase names (Phase A, B, C), counted by a walk
    .3.3.1.2.k            breaker names (Circuit 1, 2 ...), counted by a walk
    .4.1.2.n / .4.1.3.n   probe serial / label, counted by a walk

Measurements (one GET per poll, by read()):
    .3.1.1.9.1            pduTotalRealPower, W
    .3.1.1.10.1           pduTotalApparentPower, VA
    .3.1.1.11.1           pduTotalPowerFactor, %
    .3.1.1.12.1           pduTotalEnergy, Wh
    .3.2.1.4.p            pduPhaseVoltage, decivolts
    .3.2.1.8.p            pduPhaseCurrent, centiamps
    .3.3.1.4.k            pduBreakerCurrent, centiamps
    .4.1.4.n / .4.1.5.n   probe available (1) / temperature, tenths of a
                          degree in temperatureUnits (confirmed for the probes,
                          6.6 in CLAUDE.md)

The phases are taken in index order as L1, L2, L3, as Power IQ numbers them:
their user labels can differ (RACK14-PDU01 labels Phase B "Phase - L3").
The inlet current is the highest phase current. The MIB has no breaker or
inlet rating and no breaker trip state; pduBreakerLossOfLoadDetected
(.3.3.1.21.k, false = 2 on site) exists but is not used.

Run directly to test one PDU (name from pdus.toml, community from config.toml):
    python -m dcmonitor.collectors.geist RACK13-PDU02
"""

import sys
from dataclasses import dataclass

try:
    from ..snmp_v1 import SnmpError, snmp_get, snmp_walk
    from .raritan_px2 import BreakerReading, CollectorError, PduReading, _from_project, _text
except ImportError:  # running the file directly rather than as a module
    from dcmonitor.snmp_v1 import SnmpError, snmp_get, snmp_walk
    from dcmonitor.collectors.raritan_px2 import (BreakerReading, CollectorError, PduReading,
                                                  _from_project, _text)

V5 = "1.3.6.1.4.1.21239.5.2"
CHUNK = 20


@dataclass(frozen=True)
class Layout:
    name: str
    model: str
    fahrenheit: bool
    phases: tuple          # phase indexes, in order
    breakers: tuple        # breaker indexes, in order
    probes: tuple          # (index, serial, label)

    @property
    def inlet_poles(self) -> int:
        return len(self.phases)


def _oid(*parts) -> str:
    return ".".join([V5] + [str(part) for part in parts])


def _get(ip, oids, community, timeout, retries) -> dict:
    values = {}
    try:
        for start in range(0, len(oids), CHUNK):
            values.update(snmp_get(ip, oids[start:start + CHUNK], community=community,
                                   timeout=timeout, retries=retries, version="2c"))
    except SnmpError as error:
        raise CollectorError(str(error)) from None
    return values


def _rows(ip, base, community, timeout, retries) -> dict:
    """{index: value} of one table column, by GETNEXT."""
    try:
        found = snmp_walk(ip, base, community=community, timeout=timeout,
                          retries=retries, version="2c", max_rows=64)
    except SnmpError as error:
        raise CollectorError(str(error)) from None
    return {int(oid.rsplit(".", 1)[1]): value for oid, value in found.items()}


def describe(ip: str, community: str, timeout: float = 3.0, retries: int = 1) -> Layout:
    """Read the PDU's layout: its phases, breakers and temperature probes."""
    head = _get(ip, [_oid(1, 7, 0), _oid(1, 8, 0), _oid(3, 1, 1, 4, 1)],
                community, timeout, retries)
    phases = _rows(ip, _oid(3, 2, 1, 2), community, timeout, retries)
    if not phases:
        raise CollectorError("no VERTIV-V5-MIB phase table: not a Geist/Vertiv PDU?")
    breakers = _rows(ip, _oid(3, 3, 1, 2), community, timeout, retries)
    serials = _rows(ip, _oid(4, 1, 2), community, timeout, retries)
    labels = _rows(ip, _oid(4, 1, 3), community, timeout, retries) if serials else {}
    return Layout(
        name=_text(head.get(_oid(3, 1, 1, 4, 1))),
        model=_text(head.get(_oid(1, 8, 0))),
        fahrenheit=head.get(_oid(1, 7, 0)) == 0,
        phases=tuple(sorted(phases)),
        breakers=tuple(sorted(breakers)),
        probes=tuple((n, _text(serials[n]), _text(labels.get(n))) for n in sorted(serials)),
    )


def read(ip: str, community: str, layout: Layout, timeout: float = 3.0,
         retries: int = 1) -> PduReading:
    """The PDU's readings now, in one GET (two for large PDUs)."""
    oids = [_oid(3, 1, 1, column, 1) for column in (9, 10, 11, 12)]
    for p in layout.phases:
        oids += [_oid(3, 2, 1, 4, p), _oid(3, 2, 1, 8, p)]
    oids += [_oid(3, 3, 1, 4, k) for k in layout.breakers]
    for n, _serial, _label in layout.probes:
        oids += [_oid(4, 1, 4, n), _oid(4, 1, 5, n)]
    values = _get(ip, oids, community, timeout, retries)

    def number(*parts, scale=1):
        value = values.get(_oid(*parts))
        return value / scale if isinstance(value, int) else None

    lines = tuple(number(3, 2, 1, 8, p, scale=100) for p in layout.phases)
    volts = [v for v in (number(3, 2, 1, 4, p, scale=10) for p in layout.phases)
             if v is not None]
    present = [amps for amps in lines if amps is not None]
    breakers = tuple(BreakerReading(f"C{position}", number(3, 3, 1, 4, k, scale=100),
                                    None, None, None)
                     for position, k in enumerate(layout.breakers, start=1))
    temperatures = []
    for n, serial, label in layout.probes:
        celsius = None
        if values.get(_oid(4, 1, 4, n)) == 1:
            celsius = number(4, 1, 5, n, scale=10)
            if celsius is not None and layout.fahrenheit:
                celsius = (celsius - 32) * 5 / 9
            if celsius is not None:
                celsius = round(celsius, 1)
        temperatures.append((serial, label or serial, celsius))
    factor = number(3, 1, 1, 11, 1)
    return PduReading(
        name=layout.name, amps=max(present) if present else None,
        volts=volts[0] if volts else None, watts=number(3, 1, 1, 9, 1),
        va=number(3, 1, 1, 10, 1), power_factor=None if factor is None else factor / 100,
        energy_wh=number(3, 1, 1, 12, 1), hz=None, inlet_rated=None,
        lines=lines if len(lines) > 1 else (), breakers=breakers,
        temperatures=tuple(temperatures), reading_utc=None,
    )


def main() -> int:
    if len(sys.argv) == 2:
        ip, community = _from_project(sys.argv[1])
    elif len(sys.argv) == 3:
        ip, community = sys.argv[1], sys.argv[2]
    else:
        print("Usage: python -m dcmonitor.collectors.geist <PDU name in pdus.toml>")
        print("       python -m dcmonitor.collectors.geist <IP> <COMMUNITY>")
        return 2
    try:
        layout = describe(ip, community)
        reading = read(ip, community, layout)
    except CollectorError as error:
        print(f"ERROR: {error}")
        return 1

    def show(value, unit, digits=1):
        return "--" if value is None else f"{value:.{digits}f} {unit}"

    print(f"{layout.name}  ({layout.model}), {layout.inlet_poles} phase(s)")
    print(f"  current        {show(reading.amps, 'A', 2)}  (highest phase)")
    print(f"  voltage        {show(reading.volts, 'V', 1)}")
    print(f"  active power   {show(reading.watts, 'W', 0)}")
    print(f"  apparent power {show(reading.va, 'VA', 0)}")
    print(f"  power factor   {show(reading.power_factor, '', 2)}")
    for number, amps in enumerate(reading.lines, start=1):
        print(f"  line L{number}        {show(amps, 'A', 2)}")
    for breaker in reading.breakers:
        print(f"  breaker {breaker.label:6} {show(breaker.current, 'A', 2):>9}  (no rating in the MIB)")
    if not reading.temperatures:
        print("  temperature    no probe connected")
    for serial, name, celsius in reading.temperatures:
        print(f"  temperature    {name} ({serial}): "
              f"{show(celsius, 'C') if celsius is not None else 'unavailable'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
