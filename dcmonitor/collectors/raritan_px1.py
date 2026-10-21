"""Collector for Raritan PX1 / Dominion PX PDUs read directly over SNMP (PDU-MIB).

Read-only: SNMP GET and GETNEXT through dcmonitor.snmp_v1, which has no SET.

OIDs are from Raritan's PX1 PDU-MIB 1.5.2 (mibs/pdu-mib-1-5-2.txt, from
support.raritan.com, filed under PX firmware 1.5.4), under enterprises.13742.4.
The PDUs on site run firmware 01.05.07-11172 (DPXS20A-32 and PX-5469); every
OID below answered on them. Their web pages cannot be opened (TLS 1.0 only),
so values were checked against Power IQ on 2 Oct 2026 instead: RACK16-PDU02
inlet 1224 mA / 229000 mV / 190 W / 284 VA / 67 % against Power IQ's 1.26 A,
229 V, 190 W, 284 VA, PF 0.67; breakers CB1 1224 mA and CB2 0 mA of 16000 mA
against 1.26 and 0.0 A of 16 A; RACK17-PDU01 and RACK18-PDU01 likewise.

Configuration (read once, by describe()):
    .1.1.12.0               objectName (model)              DPXS20A-32
    .1.1.13.0               objectInstance (device name)    RACK16-PDU02
    .1.20.1.0               inletCount                      1
    .1.20.2.1.3.1           inletPoleCount (1 = single phase)
    .1.20.2.1.4.1           inletCurrentRating, mA          32000
    .1.5.1.0                circuitBreakerCount             2
    .1.5.2.1.{2,4}.k        breaker label, rating mA        CB1, 16000
    .3.1.0                  externalSensorCount             0 or 1 on site
    .3.3.1.{2,3,4,16,17}.s  sensor type (10 = temperature), serial, name,
                            units (7 C, 14 F), decimal digits

Measurements (one GET per poll, by read()):
    .1.20.2.1.7.1           inletCurrent, mA
    .1.20.2.1.8.1           inletVoltage, mV
    .1.20.2.1.9.1           inletActivePower, W
    .1.20.2.1.10.1          inletApparentPower, VA
    .1.20.2.1.11.1          inletPowerFactor, percent (67 = 0.67)
    .1.21.2.1.3.1.p         inletPoleCurrent, mA (read when the inlet has
                            more than 1 pole; none on site)
    .1.5.2.1.3.k            circuitBreakerState: ok(0), tripped(1)
    .1.5.2.1.5.k            circuitBreakerCurrent, mA
    .1.2.2.1.4.o            outletCurrent, mA (outletCount .1.2.1.0, in
                            describe). 9 Oct 2026: RACK17-PDU01 (DPXS20A-32)
                            20 outlets add up to 8.50 A, inlet 8.39 A;
                            RACK18-PDU01 (PX-5469) 2.50 A, inlet 2.49 A. The MIB
                            does not say which breaker an outlet is on.
    .3.3.1.{40,41}.s        sensor state (-1 unavailable), value scaled by
                            its decimal digits (225 with 1 digit = 22.5 C)

Not used: inletActiveEnergy (reads 0 on site), the deprecated unit and line
objects (.1.3, .1.4, .1.6) and the log tables (.1.7, .1.10, .1.12-.1.15),
which are large and slow: never walk them.

Run directly to test one PDU (name from pdus.toml, community from config.toml):
    python -m dcmonitor.collectors.raritan_px1 RACK16-PDU02
"""

import sys
from dataclasses import dataclass

try:
    from ..snmp_v1 import SnmpError, snmp_get
    from .raritan_px2 import (BreakerReading, CollectorError, OutletReading, PduReading,
                              _from_project, _text)
except ImportError:  # running the file directly rather than as a module
    from dcmonitor.snmp_v1 import SnmpError, snmp_get
    from dcmonitor.collectors.raritan_px2 import (BreakerReading, CollectorError,
                                                  OutletReading, PduReading,
                                                  _from_project, _text)

PX1 = "1.3.6.1.4.1.13742.4"
CHUNK = 20
TEMPERATURE = 10
DEGREE_F = 14
BREAKER_STATES = {0: "closed", 1: "open"}     # MIB: ok(0), tripped(1)


@dataclass(frozen=True)
class Probe:
    index: int
    serial: str
    name: str
    fahrenheit: bool
    decimals: int


@dataclass(frozen=True)
class Layout:
    name: str
    model: str
    inlet_rated: float | None
    inlet_poles: int
    breakers: tuple             # (index, label, rated amps)
    probes: tuple
    outlets: int = 0            # outlet count; every outlet has a current meter


def _oid(*parts) -> str:
    return ".".join([PX1] + [str(part) for part in parts])


def _get(ip, oids, community, timeout, retries) -> dict:
    values = {}
    try:
        for start in range(0, len(oids), CHUNK):
            values.update(snmp_get(ip, oids[start:start + CHUNK], community=community,
                                   timeout=timeout, retries=retries, version="2c"))
    except SnmpError as error:
        raise CollectorError(str(error)) from None
    return values


def _milli(value):
    return value / 1000 if isinstance(value, int) else None


def describe(ip: str, community: str, timeout: float = 3.0, retries: int = 1) -> Layout:
    """Read the PDU's layout: inlet rating, breakers and temperature probes."""
    head = _get(ip, [_oid(1, 1, 12, 0), _oid(1, 1, 13, 0), _oid(1, 20, 1, 0),
                     _oid(1, 20, 2, 1, 3, 1), _oid(1, 20, 2, 1, 4, 1), _oid(1, 5, 1, 0),
                     _oid(3, 1, 0), _oid(1, 2, 1, 0)], community, timeout, retries)
    inlets = head.get(_oid(1, 20, 1, 0))
    if not isinstance(inlets, int):
        raise CollectorError("no PDU-MIB inlet count: not a Raritan PX1?")
    if inlets != 1:
        raise CollectorError(f"{inlets} inlets: only single-inlet PDUs are handled")
    count = head.get(_oid(1, 5, 1, 0))
    count = count if isinstance(count, int) else 0
    sensors = head.get(_oid(3, 1, 0))
    sensors = sensors if isinstance(sensors, int) else 0

    oids = []
    for k in range(1, count + 1):
        oids += [_oid(1, 5, 2, 1, 2, k), _oid(1, 5, 2, 1, 4, k)]
    for s in range(1, sensors + 1):
        oids += [_oid(3, 3, 1, column, s) for column in (2, 3, 4, 16, 17)]
    values = _get(ip, oids, community, timeout, retries) if oids else {}

    breakers = tuple(
        (k, _text(values.get(_oid(1, 5, 2, 1, 2, k))) or f"CB{k}",
         _milli(values.get(_oid(1, 5, 2, 1, 4, k))))
        for k in range(1, count + 1))
    probes = []
    for s in range(1, sensors + 1):
        if values.get(_oid(3, 3, 1, 2, s)) != TEMPERATURE:
            continue
        digits = values.get(_oid(3, 3, 1, 17, s))
        probes.append(Probe(
            index=s,
            serial=_text(values.get(_oid(3, 3, 1, 3, s))),
            name=_text(values.get(_oid(3, 3, 1, 4, s))),
            fahrenheit=values.get(_oid(3, 3, 1, 16, s)) == DEGREE_F,
            decimals=digits if isinstance(digits, int) else 1,
        ))
    poles = head.get(_oid(1, 20, 2, 1, 3, 1))
    outlets = head.get(_oid(1, 2, 1, 0))
    return Layout(
        name=_text(head.get(_oid(1, 1, 13, 0))),
        model=_text(head.get(_oid(1, 1, 12, 0))),
        inlet_rated=_milli(head.get(_oid(1, 20, 2, 1, 4, 1))),
        inlet_poles=poles if isinstance(poles, int) else 1,
        breakers=breakers,
        probes=tuple(probes),
        outlets=outlets if isinstance(outlets, int) else 0,
    )


def read(ip: str, community: str, layout: Layout, timeout: float = 3.0,
         retries: int = 1) -> PduReading:
    """The PDU's readings now, in one GET."""
    oids = [_oid(1, 20, 2, 1, column, 1) for column in (7, 8, 9, 10, 11)]
    line_poles = range(1, layout.inlet_poles + 1) if layout.inlet_poles > 1 else ()
    oids += [_oid(1, 21, 2, 1, 3, 1, pole) for pole in line_poles]
    for k, _label, _rated in layout.breakers:
        oids += [_oid(1, 5, 2, 1, 3, k), _oid(1, 5, 2, 1, 5, k)]
    for probe in layout.probes:
        oids += [_oid(3, 3, 1, 40, probe.index), _oid(3, 3, 1, 41, probe.index)]
    oids += [_oid(1, 2, 2, 1, 4, o) for o in range(1, layout.outlets + 1)]
    values = _get(ip, oids, community, timeout, retries)

    def number(*parts):
        value = values.get(_oid(*parts))
        return value if isinstance(value, int) else None

    factor = number(1, 20, 2, 1, 11, 1)
    breakers = tuple(
        BreakerReading(label, _milli(number(1, 5, 2, 1, 5, k)), rated, None,
                       BREAKER_STATES.get(number(1, 5, 2, 1, 3, k)))
        for k, label, rated in layout.breakers)
    temperatures = []
    for probe in layout.probes:
        state, raw = number(3, 3, 1, 40, probe.index), number(3, 3, 1, 41, probe.index)
        celsius = None
        if state is not None and state != -1 and raw is not None:
            celsius = raw / (10 ** probe.decimals)
            if probe.fahrenheit:
                celsius = (celsius - 32) * 5 / 9
            celsius = round(celsius, 1)
        temperatures.append((probe.serial, probe.name, celsius))
    return PduReading(
        name=layout.name, amps=_milli(number(1, 20, 2, 1, 7, 1)),
        volts=_milli(number(1, 20, 2, 1, 8, 1)), watts=number(1, 20, 2, 1, 9, 1),
        va=number(1, 20, 2, 1, 10, 1), power_factor=None if factor is None else factor / 100,
        energy_wh=None, hz=None, inlet_rated=layout.inlet_rated,
        lines=tuple(_milli(number(1, 21, 2, 1, 3, 1, pole)) for pole in line_poles),
        breakers=breakers, temperatures=tuple(temperatures), reading_utc=None,
        outlets=tuple(OutletReading(o, _milli(number(1, 2, 2, 1, 4, o)), None)
                      for o in range(1, layout.outlets + 1)),
    )


def main() -> int:
    if len(sys.argv) == 2:
        ip, community = _from_project(sys.argv[1])
    elif len(sys.argv) == 3:
        ip, community = sys.argv[1], sys.argv[2]
    else:
        print("Usage: python -m dcmonitor.collectors.raritan_px1 <PDU name in pdus.toml>")
        print("       python -m dcmonitor.collectors.raritan_px1 <IP> <COMMUNITY>")
        return 2
    try:
        layout = describe(ip, community)
        reading = read(ip, community, layout)
    except CollectorError as error:
        print(f"ERROR: {error}")
        return 1

    def show(value, unit, digits=1):
        return "--" if value is None else f"{value:.{digits}f} {unit}"

    print(f"{layout.name}  ({layout.model}), inlet rated {show(layout.inlet_rated, 'A', 0)}, "
          f"{layout.inlet_poles} pole(s)")
    print(f"  current        {show(reading.amps, 'A', 2)}")
    print(f"  voltage        {show(reading.volts, 'V', 0)}")
    print(f"  active power   {show(reading.watts, 'W', 0)}")
    print(f"  apparent power {show(reading.va, 'VA', 0)}")
    print(f"  power factor   {show(reading.power_factor, '', 2)}")
    for number, amps in enumerate(reading.lines, start=1):
        print(f"  line L{number}        {show(amps, 'A', 2)}")
    for breaker in reading.breakers:
        print(f"  breaker {breaker.label:6} {show(breaker.current, 'A', 2):>9} of "
              f"{show(breaker.rated, 'A', 0)}  {breaker.state or 'state unknown'}")
    if not reading.temperatures:
        print("  temperature    no probe connected")
    for serial, name, celsius in reading.temperatures:
        print(f"  temperature    {name} ({serial}): "
              f"{show(celsius, 'C') if celsius is not None else 'unavailable'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
