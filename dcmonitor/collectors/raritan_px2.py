"""Collector for Raritan PX2 / PX3 PDUs read directly over SNMP (PDU2-MIB).

Read-only: SNMP GET and GETNEXT through dcmonitor.snmp_v1, which has no SET.

OIDs are from Raritan's PDU2-MIB (release 3.6.0, the MIB of firmware 3.6.1),
under enterprises.13742.6 ("pdu2"). Every index starts with pduId, which is
1 on a PDU that is not linked to others. Checked 2 Oct 2026 on RACK10-PDU01
(PX3-5466V, firmware 3.6.1.5-46982): a walk against the PDU's web page.

Configuration (read once, by describe()):
    .3.2.2.1.2.1              inletCount                      1
    .3.2.2.1.3.1              overCurrentProtectorCount       2
    .3.2.2.1.4.1              outletCount                     20
    .3.2.2.1.13.1             pduName                         RACK10-PDU01
    .3.2.1.1.3.1              pduModel                        PX3-5466V
    .3.3.3.1.{2,5,7}.1.i      inlet label, pole count, rated  I1, 2, "32A"
    .3.3.4.1.7.1.i.t          inlet sensor decimal digits     per sensor type t
    .3.4.3.1.{2,5}.1.k        breaker label, rated current    C1, "16A"
    .3.4.3.1.9.1.k            breaker capabilities (BITS)     bit 14 = trip sensor
    .3.4.4.1.7.1.k.1          breaker current decimal digits  3
    .3.4.5.1.2.1.k.1          breaker line (1 = L1)           1
    .3.6.3.1.2.1.s            external sensor type, 10 = temperature; only
                              connected sensors have a row
    .3.6.3.1.{3,4,16,17}.1.s  serial, name, units (7 C, 14 F), decimal digits
    .3.5.3.1.33.1.o           outlet's breaker: an OID pointing to the breaker's
                              label row, .3.4.3.1.2.1.k (k = breaker index)
    .3.5.4.1.7.1.o.1          outlet current decimal digits (outlet-metered PDUs)

Measurements (one GET per poll, by read()):
    .5.2.3.1.{2,4}.1.i.t      inlet: available (1 = yes), value; types:
                              1 current A, 4 voltage V, 5 active power W,
                              6 apparent power VA, 7 power factor, 8 energy Wh,
                              23 frequency Hz
    .5.2.3.1.5.1.i.1          reading time, UNIX seconds UTC
    .5.3.3.1.{2,4}.1.k.1      breaker current: available, value
    .5.3.3.1.{2,3}.1.k.15     breaker trip sensor: available, state
                              (1 = closed, 0 = open = tripped, -1 unavailable)
    .5.5.3.1.{2,4}.1.s        external sensor: available, value
    .5.4.3.1.{2,4}.1.o.1      outlet current: available, value

Outlets (9 Oct 2026, for the cabinet list): the PX3-5466V (46 PDUs) meters
every outlet; checked on RACK11-PDU01, its 20 outlets (3 decimal digits) add
up to 2.91 A against an inlet of 2.914 A. Every PX2/PX3 on site says which
breaker each outlet is on (column 33 above): on RACK11-PDU01 outlets 1-10
are on C1 and add up to 2.36 A = C1's 2.36 A, outlets 11-20 to 0.56 A =
C2's 0.554 A. PX2-1493 (outlets 1-12 C1, 13-24 C2) and PX3-1730* (six per
breaker, C1..C6) give the breaker but no outlet current.

PX2-3120 (four PDUs on site): the inlet has no sensors (capabilities all 0,
plug "AC Terminal") and the PDU has one 32 A outlet that carries the
meter. Its readings are used as the inlet's: decimal digits
.3.5.4.1.7.1.1.t, values .5.4.3.1.{2,4,5}.1.1.t, same sensor types.

A value is scaled by its decimal digits: 489 with 3 digits is 0.489 A.
RACK10-PDU01 against its web page: inlet 489 = 0.5 A of 32 A, 227 V, 80 W,
111 VA, 72 = PF 0.72, 5514827 Wh = 5.51 MWh, 500 = 50.0 Hz; C1 0 A and
C2 489 = 0.5 A of 16 A, both on L1, trip state 1 shown as "closed".

Not yet seen on site: a three-phase PX (inlet poles, .5.2.4, read when the
inlet has 3 or more poles), an open (tripped) breaker, and a working
temperature probe (RACK10-PDU01's "Temperature 1" reads unavailable).

Run directly to test one PDU (name from pdus.toml, community from config.toml):
    python -m dcmonitor.collectors.raritan_px2 RACK10-PDU01
or with an address and community:
    python -m dcmonitor.collectors.raritan_px2 <IP> <COMMUNITY>
"""

import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

try:
    from ..snmp_v1 import SnmpError, snmp_get, snmp_walk
except ImportError:  # running the file directly rather than as a module
    from dcmonitor.snmp_v1 import SnmpError, snmp_get, snmp_walk

PDU2 = "1.3.6.1.4.1.13742.6"
PDU_ID = 1
CHUNK = 20                          # OIDs per GET, well inside one UDP packet

# Inlet sensor types used (SensorTypeEnumeration in the MIB).
CURRENT, VOLTAGE, POWER, APPARENT, POWER_FACTOR, ENERGY, FREQUENCY = 1, 4, 5, 6, 7, 8, 23
INLET_TYPES = (CURRENT, VOLTAGE, POWER, APPARENT, POWER_FACTOR, ENERGY, FREQUENCY)
TRIP = 15
TEMPERATURE = 10
TRIP_STATES = {1: "closed", 0: "open"}
TRIP_CAPABILITY_BIT = 14
DEGREE_C, DEGREE_F = 7, 14


class CollectorError(Exception):
    """The PDU did not answer, or is not a PX2/PX3."""


@dataclass(frozen=True)
class Breaker:
    index: int
    label: str                  # C1, C2 ... as printed on the PDU
    rated: float | None         # amps, from the PDU's own rating, e.g. "16A"
    line: int | None            # 1..3 = L1..L3
    decimals: int
    has_trip: bool              # the breaker has a trip (open/closed) sensor


@dataclass(frozen=True)
class Probe:
    index: int
    serial: str
    name: str
    fahrenheit: bool
    decimals: int


@dataclass(frozen=True)
class Outlet:
    index: int
    breaker: str | None         # label of the breaker it is on, if the PDU says
    decimals: int | None        # None: the outlet has no current meter


@dataclass(frozen=True)
class OutletReading:
    number: int
    amps: float | None          # None: no meter on this outlet, or no reading
    breaker: str | None         # C1 ... (as the PDU labels it), if known


@dataclass(frozen=True)
class Layout:
    """What a PDU has, from describe(). Changes only when the PDU is rebuilt."""
    name: str
    model: str
    inlet_label: str
    inlet_rated: float | None
    inlet_poles: int
    inlet_decimals: dict        # sensor type -> decimal digits
    meter: tuple                # measurements table of the inlet values: (5, 2, 3, 1)
                                # for the inlet, (5, 4, 3, 1) for a one-outlet meter
    pole_decimals: int
    breakers: tuple
    probes: tuple
    outlets: tuple = ()         # Outlet, for the cabinet list


@dataclass(frozen=True)
class BreakerReading:
    label: str
    current: float | None
    rated: float | None
    line: int | None
    state: str | None           # "closed", "open" (tripped) or None if not known


@dataclass(frozen=True)
class PduReading:
    name: str
    amps: float | None
    volts: float | None
    watts: float | None
    va: float | None
    power_factor: float | None
    energy_wh: float | None
    hz: float | None
    inlet_rated: float | None
    lines: tuple                # amps per inlet pole (three-phase only), else ()
    breakers: tuple
    temperatures: tuple         # (serial, name, celsius or None if unavailable)
    reading_utc: datetime | None
    outlets: tuple = ()         # OutletReading per outlet, where the PDU tells


# ------------------------------------------------------------------ helpers

def _get(ip, oids, community, timeout, retries) -> dict:
    values = {}
    try:
        for start in range(0, len(oids), CHUNK):
            values.update(snmp_get(ip, oids[start:start + CHUNK], community=community,
                                   timeout=timeout, retries=retries, version="2c"))
    except SnmpError as error:
        raise CollectorError(str(error)) from None
    return values


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("latin-1").strip()
    return "" if value is None else str(value).strip()


def _amps(text) -> float | None:
    """'16A' or '32A' to 16.0 or 32.0."""
    match = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)", _text(text))
    return float(match.group(1)) if match else None


def _bit(bits, number: int) -> bool:
    """SNMP BITS: bit 0 is the top bit of the first byte."""
    if not isinstance(bits, bytes) or number // 8 >= len(bits):
        return False
    return bool(bits[number // 8] & (0x80 >> (number % 8)))


def _scaled(value, decimals: int):
    return None if not isinstance(value, int) else value / (10 ** decimals)


def _oid(*parts) -> str:
    return ".".join([PDU2] + [str(part) for part in parts])


# ----------------------------------------------------------------- describe

def describe(ip: str, community: str, timeout: float = 3.0, retries: int = 1) -> Layout:
    """Read the PDU's layout: its inlet, breakers and temperature probes."""
    head = _get(ip, [_oid(3, 2, 2, 1, 2, PDU_ID), _oid(3, 2, 2, 1, 3, PDU_ID),
                     _oid(3, 2, 2, 1, 13, PDU_ID), _oid(3, 2, 1, 1, 3, PDU_ID),
                     _oid(3, 2, 2, 1, 4, PDU_ID)],
                community, timeout, retries)
    inlets = head.get(_oid(3, 2, 2, 1, 2, PDU_ID))
    if not isinstance(inlets, int):
        raise CollectorError("no PDU2-MIB inlet count: not a Raritan PX2/PX3?")
    if inlets != 1:
        raise CollectorError(f"{inlets} inlets: only single-inlet PDUs are handled")
    count = head.get(_oid(3, 2, 2, 1, 3, PDU_ID))
    count = count if isinstance(count, int) else 0
    outlets = head.get(_oid(3, 2, 2, 1, 4, PDU_ID))

    oids = [_oid(3, 3, 3, 1, column, PDU_ID, 1) for column in (2, 5, 7)]
    oids += [_oid(3, 3, 4, 1, 7, PDU_ID, 1, kind) for kind in INLET_TYPES]
    for k in range(1, count + 1):
        oids += [_oid(3, 4, 3, 1, 2, PDU_ID, k), _oid(3, 4, 3, 1, 5, PDU_ID, k),
                 _oid(3, 4, 3, 1, 9, PDU_ID, k), _oid(3, 4, 4, 1, 7, PDU_ID, k, CURRENT),
                 _oid(3, 4, 5, 1, 2, PDU_ID, k, 1)]
    values = _get(ip, oids, community, timeout, retries)

    poles = values.get(_oid(3, 3, 3, 1, 5, PDU_ID, 1))
    poles = poles if isinstance(poles, int) else 2
    pole_decimals = 3
    if poles >= 3:
        found = _get(ip, [_oid(3, 3, 6, 1, 7, PDU_ID, 1, 1, CURRENT)],
                     community, timeout, retries)
        pole_value = found.get(_oid(3, 3, 6, 1, 7, PDU_ID, 1, 1, CURRENT))
        if isinstance(pole_value, int):
            pole_decimals = pole_value
    decimals = {}
    for kind in INLET_TYPES:
        digits = values.get(_oid(3, 3, 4, 1, 7, PDU_ID, 1, kind))
        if isinstance(digits, int):
            decimals[kind] = digits
    meter = (5, 2, 3, 1)
    if not decimals and outlets == 1:
        # PX2-3120: an inlet without sensors and one metered 32 A outlet,
        # whose readings are the PDU's (RACK12-PDU01: 21 = 2.1 A, 416 W, as
        # Power IQ shows for its inlet).
        meter = (5, 4, 3, 1)
        found = _get(ip, [_oid(3, 5, 4, 1, 7, PDU_ID, 1, kind) for kind in INLET_TYPES],
                     community, timeout, retries)
        for kind in INLET_TYPES:
            digits = found.get(_oid(3, 5, 4, 1, 7, PDU_ID, 1, kind))
            if isinstance(digits, int):
                decimals[kind] = digits

    breakers = []
    for k in range(1, count + 1):
        digits = values.get(_oid(3, 4, 4, 1, 7, PDU_ID, k, CURRENT))
        line = values.get(_oid(3, 4, 5, 1, 2, PDU_ID, k, 1))
        breakers.append(Breaker(
            index=k,
            label=_text(values.get(_oid(3, 4, 3, 1, 2, PDU_ID, k))) or f"C{k}",
            rated=_amps(values.get(_oid(3, 4, 3, 1, 5, PDU_ID, k))),
            line=line if isinstance(line, int) else None,
            decimals=digits if isinstance(digits, int) else 3,
            has_trip=_bit(values.get(_oid(3, 4, 3, 1, 9, PDU_ID, k)), TRIP_CAPABILITY_BIT),
        ))

    # Outlets: the breaker each is on, and whether it has a current meter.
    outlet_list = []
    if meter == (5, 4, 3, 1):
        # PX2-3120: its one outlet is the meter (the whole PDU).
        outlet_list.append(Outlet(1, None, decimals.get(CURRENT)))
    elif isinstance(outlets, int) and outlets > 1:
        numbers = range(1, outlets + 1)
        found = _get(ip, [_oid(3, 5, 3, 1, 33, PDU_ID, o) for o in numbers]
                     + [_oid(3, 5, 4, 1, 7, PDU_ID, o, CURRENT) for o in numbers]
                     + [_oid(5, 4, 3, 1, 2, PDU_ID, o, CURRENT) for o in numbers],
                     community, timeout, retries)
        labels = {b.index: b.label for b in breakers}
        source = ".".join([PDU2, "3", "4", "3", "1", "2", str(PDU_ID)]) + "."
        for o in numbers:
            pointer = str(found.get(_oid(3, 5, 3, 1, 33, PDU_ID, o)) or "").lstrip(".")
            breaker = None
            if pointer.startswith(source) and pointer[len(source):].isdigit():
                breaker = labels.get(int(pointer[len(source):]))
            digits = found.get(_oid(3, 5, 4, 1, 7, PDU_ID, o, CURRENT))
            metered = (isinstance(digits, int)
                       and found.get(_oid(5, 4, 3, 1, 2, PDU_ID, o, CURRENT)) == 1)
            outlet_list.append(Outlet(o, breaker, digits if metered else None))

    # Only connected external sensors have a row; walk one column to find them.
    try:
        kinds = snmp_walk(ip, _oid(3, 6, 3, 1, 2, PDU_ID), community=community,
                          timeout=timeout, retries=retries, version="2c", max_rows=64)
    except SnmpError as error:
        raise CollectorError(str(error)) from None
    sensors = [int(oid.rsplit(".", 1)[1]) for oid, kind in kinds.items()
               if kind == TEMPERATURE]
    probes = []
    if sensors:
        oids = []
        for s in sensors:
            oids += [_oid(3, 6, 3, 1, column, PDU_ID, s) for column in (3, 4, 16, 17)]
        values_s = _get(ip, oids, community, timeout, retries)
        for s in sensors:
            digits = values_s.get(_oid(3, 6, 3, 1, 17, PDU_ID, s))
            probes.append(Probe(
                index=s,
                serial=_text(values_s.get(_oid(3, 6, 3, 1, 3, PDU_ID, s))),
                name=_text(values_s.get(_oid(3, 6, 3, 1, 4, PDU_ID, s))),
                fahrenheit=values_s.get(_oid(3, 6, 3, 1, 16, PDU_ID, s)) == DEGREE_F,
                decimals=digits if isinstance(digits, int) else 1,
            ))

    return Layout(
        name=_text(head.get(_oid(3, 2, 2, 1, 13, PDU_ID))),
        model=_text(head.get(_oid(3, 2, 1, 1, 3, PDU_ID))),
        inlet_label=_text(values.get(_oid(3, 3, 3, 1, 2, PDU_ID, 1))) or "I1",
        inlet_rated=_amps(values.get(_oid(3, 3, 3, 1, 7, PDU_ID, 1))),
        inlet_poles=poles,
        inlet_decimals=decimals,
        meter=meter,
        pole_decimals=pole_decimals,
        breakers=tuple(breakers),
        probes=tuple(probes),
        outlets=tuple(outlet_list),
    )


# --------------------------------------------------------------------- read

def read(ip: str, community: str, layout: Layout, timeout: float = 3.0,
         retries: int = 1) -> PduReading:
    """The PDU's readings now, in one GET (two for large PDUs)."""
    oids = []
    meter = layout.meter
    for kind in layout.inlet_decimals:
        oids += [_oid(*meter, 2, PDU_ID, 1, kind), _oid(*meter, 4, PDU_ID, 1, kind)]
    oids.append(_oid(*meter, 5, PDU_ID, 1, CURRENT))
    line_poles = range(1, 4) if layout.inlet_poles >= 3 else ()
    for pole in line_poles:
        oids += [_oid(5, 2, 4, 1, 2, PDU_ID, 1, pole, CURRENT),
                 _oid(5, 2, 4, 1, 4, PDU_ID, 1, pole, CURRENT)]
    for breaker in layout.breakers:
        k = breaker.index
        oids += [_oid(5, 3, 3, 1, 2, PDU_ID, k, CURRENT), _oid(5, 3, 3, 1, 4, PDU_ID, k, CURRENT)]
        if breaker.has_trip:
            oids += [_oid(5, 3, 3, 1, 2, PDU_ID, k, TRIP), _oid(5, 3, 3, 1, 3, PDU_ID, k, TRIP)]
    for probe in layout.probes:
        oids += [_oid(5, 5, 3, 1, 2, PDU_ID, probe.index),
                 _oid(5, 5, 3, 1, 4, PDU_ID, probe.index)]
    for outlet in layout.outlets:
        if outlet.decimals is not None:
            oids += [_oid(5, 4, 3, 1, 2, PDU_ID, outlet.index, CURRENT),
                     _oid(5, 4, 3, 1, 4, PDU_ID, outlet.index, CURRENT)]
    values = _get(ip, list(dict.fromkeys(oids)), community, timeout, retries)

    def inlet(kind):
        if values.get(_oid(*meter, 2, PDU_ID, 1, kind)) != 1:
            return None
        return _scaled(values.get(_oid(*meter, 4, PDU_ID, 1, kind)),
                       layout.inlet_decimals.get(kind, 0))

    lines = []
    for pole in line_poles:
        available = values.get(_oid(5, 2, 4, 1, 2, PDU_ID, 1, pole, CURRENT)) == 1
        lines.append(_scaled(values.get(_oid(5, 2, 4, 1, 4, PDU_ID, 1, pole, CURRENT)),
                             layout.pole_decimals) if available else None)

    breakers = []
    for breaker in layout.breakers:
        k = breaker.index
        current = None
        if values.get(_oid(5, 3, 3, 1, 2, PDU_ID, k, CURRENT)) == 1:
            current = _scaled(values.get(_oid(5, 3, 3, 1, 4, PDU_ID, k, CURRENT)),
                              breaker.decimals)
        state = None
        if breaker.has_trip and values.get(_oid(5, 3, 3, 1, 2, PDU_ID, k, TRIP)) == 1:
            state = TRIP_STATES.get(values.get(_oid(5, 3, 3, 1, 3, PDU_ID, k, TRIP)))
        breakers.append(BreakerReading(breaker.label, current, breaker.rated,
                                       breaker.line, state))

    temperatures = []
    for probe in layout.probes:
        celsius = None
        if values.get(_oid(5, 5, 3, 1, 2, PDU_ID, probe.index)) == 1:
            celsius = _scaled(values.get(_oid(5, 5, 3, 1, 4, PDU_ID, probe.index)),
                              probe.decimals)
            if celsius is not None and probe.fahrenheit:
                celsius = (celsius - 32) * 5 / 9
            if celsius is not None:
                celsius = round(celsius, 1)
        temperatures.append((probe.serial, probe.name, celsius))

    outlets = []
    for outlet in layout.outlets:
        amps = None
        if (outlet.decimals is not None and
                values.get(_oid(5, 4, 3, 1, 2, PDU_ID, outlet.index, CURRENT)) == 1):
            amps = _scaled(values.get(_oid(5, 4, 3, 1, 4, PDU_ID, outlet.index, CURRENT)),
                           outlet.decimals)
        outlets.append(OutletReading(outlet.index, amps, outlet.breaker))

    stamp = values.get(_oid(*meter, 5, PDU_ID, 1, CURRENT))
    reading_utc = (datetime.fromtimestamp(stamp, timezone.utc)
                   if isinstance(stamp, int) and stamp > 0 else None)
    return PduReading(
        name=layout.name, amps=inlet(CURRENT), volts=inlet(VOLTAGE), watts=inlet(POWER),
        va=inlet(APPARENT), power_factor=inlet(POWER_FACTOR), energy_wh=inlet(ENERGY),
        hz=inlet(FREQUENCY), inlet_rated=layout.inlet_rated, lines=tuple(lines),
        breakers=tuple(breakers), temperatures=tuple(temperatures), reading_utc=reading_utc,
        outlets=tuple(outlets),
    )


# --------------------------------------------------------------------- main

def _from_project(name: str):
    """(ip, community) of a PDU named in pdus.toml: its own community if it
    has one there, else the one config.toml gives all PDUs ([pdus])."""
    from dcmonitor import config as config_module
    settings = config_module.load("config.toml").pdus
    if not settings:
        raise SystemExit("config.toml has no [pdus] section")
    match = [e for e in settings["entries"] if e["name"].upper() == name.upper()]
    if not match:
        raise SystemExit(f"{name} is not in pdus.toml")
    return match[0]["ip"], match[0]["community"]


def main() -> int:
    if len(sys.argv) == 2:
        ip, community = _from_project(sys.argv[1])
    elif len(sys.argv) == 3:
        ip, community = sys.argv[1], sys.argv[2]
    else:
        print("Usage: python -m dcmonitor.collectors.raritan_px2 <PDU name in pdus.toml>")
        print("       python -m dcmonitor.collectors.raritan_px2 <IP> <COMMUNITY>")
        return 2
    try:
        layout = describe(ip, community)
        reading = read(ip, community, layout)
    except CollectorError as error:
        print(f"ERROR: {error}")
        return 1

    def show(value, unit, digits=1):
        return "--" if value is None else f"{value:.{digits}f} {unit}"

    print(f"{layout.name}  ({layout.model}), inlet {layout.inlet_label} "
          f"rated {show(layout.inlet_rated, 'A', 0)}, {layout.inlet_poles} poles")
    when = reading.reading_utc.astimezone().strftime("%d.%m.%Y %H:%M:%S") \
        if reading.reading_utc else "--"
    print(f"  reading time   {when}")
    print(f"  current        {show(reading.amps, 'A', 2)}")
    print(f"  voltage        {show(reading.volts, 'V', 0)}")
    print(f"  active power   {show(reading.watts, 'W', 0)}")
    print(f"  apparent power {show(reading.va, 'VA', 0)}")
    print(f"  power factor   {show(reading.power_factor, '', 2)}")
    print(f"  energy         {show(None if reading.energy_wh is None else reading.energy_wh / 1e6, 'MWh', 2)}")
    print(f"  frequency      {show(reading.hz, 'Hz', 1)}")
    for number, amps in enumerate(reading.lines, start=1):
        print(f"  line L{number}        {show(amps, 'A', 2)}")
    for breaker in reading.breakers:
        line = f"L{breaker.line}" if breaker.line else "--"
        print(f"  breaker {breaker.label:6} {show(breaker.current, 'A', 2):>9} of "
              f"{show(breaker.rated, 'A', 0)}  {line}  {breaker.state or 'state unknown'}")
    if not reading.temperatures:
        print("  temperature    no probe connected")
    for serial, name, celsius in reading.temperatures:
        print(f"  temperature    {name} ({serial}): "
              f"{show(celsius, 'C') if celsius is not None else 'unavailable'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
