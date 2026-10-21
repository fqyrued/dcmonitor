"""Collector for rack temperature probes read directly from PDUs.

For racks whose PDU shows a temperature on its own web page that Power IQ
does not collect. The PDUs are listed by hand in config.toml. The make is
recognised from the PDU's sysObjectID, so entries need no make setting.

Vertiv (sysObjectID under enterprises.21239), VERTIV-V5-MIB, confirmed on site:
    1.3.6.1.4.1.21239.5.2.1.7.0         temperatureUnits: 0 = F, 1 = C
    1.3.6.1.4.1.21239.5.2.4.1.2.<n>     probe serial, the Device ID on the web page
    1.3.6.1.4.1.21239.5.2.4.1.4.<n>     available: 1
    1.3.6.1.4.1.21239.5.2.4.1.5.<n>     temperature, tenths of a degree

Schneider / APC (sysObjectID under enterprises.318), confirmed on an AP8881:
    1.3.6.1.4.1.318.1.1.26.10.2.2.1.3.<n>   probe name
    1.3.6.1.4.1.318.1.1.26.10.2.2.1.8.<n>   temperature, tenths of a degree C
    Column 7 of the same row is Fahrenheit: 678 there and 199 here are the
    same 19.9 C, which is how the column and its scale were confirmed.
    A position without a probe reads -1.

A probe is chosen by one of three ways, most reliable first:
    serial   the probe's Device ID (Vertiv) or name (Schneider)
    probe    its SNMP position number
    neither  automatic: the available probe; the warmer one if several

Run directly to test one PDU:
    python -m dcmonitor.collectors.pdu_sensor <IP> <COMMUNITY> [position|serial]
"""

import sys

try:
    from ..snmp_v1 import SnmpError, snmp_get
except ImportError:  # running the file directly rather than as a module
    from dcmonitor.snmp_v1 import SnmpError, snmp_get

OID_SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
VERTIV_PREFIX = "1.3.6.1.4.1.21239."
APC_PREFIX = "1.3.6.1.4.1.318."
POSITIONS = (1, 2, 3, 4)

VERTIV_UNITS = "1.3.6.1.4.1.21239.5.2.1.7.0"
VERTIV_SERIAL = "1.3.6.1.4.1.21239.5.2.4.1.2.{index}"
VERTIV_AVAIL = "1.3.6.1.4.1.21239.5.2.4.1.4.{index}"
VERTIV_TEMP = "1.3.6.1.4.1.21239.5.2.4.1.5.{index}"

APC_NAME = "1.3.6.1.4.1.318.1.1.26.10.2.2.1.3.{index}"
APC_TEMP_C = "1.3.6.1.4.1.318.1.1.26.10.2.2.1.8.{index}"

# The make of each PDU, learned once per address.
_makes = {}


class CollectorError(Exception):
    """The PDU did not answer, is an unknown make, or has no usable probe."""


def _get(ip, oids, community, timeout, retries):
    try:
        return snmp_get(ip, oids, community=community, timeout=timeout,
                        retries=retries, version="2c")
    except SnmpError as error:
        raise CollectorError(str(error)) from None


def make_of(ip: str, community: str, timeout: float = 3.0, retries: int = 1) -> str:
    """'vertiv' or 'apc', from the PDU's sysObjectID. Remembered per address."""
    if ip not in _makes:
        object_id = str(_get(ip, [OID_SYS_OBJECT_ID], community, timeout, retries)
                        .get(OID_SYS_OBJECT_ID) or "")
        if object_id.startswith(VERTIV_PREFIX):
            _makes[ip] = "vertiv"
        elif object_id.startswith(APC_PREFIX):
            _makes[ip] = "apc"
        else:
            raise CollectorError(f"unsupported PDU make (sysObjectID {object_id or 'none'})")
    return _makes[ip]


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("latin-1").strip()
    return str(value or "").strip()


def read_probes(ip: str, community: str, timeout: float = 3.0, retries: int = 1) -> list:
    """Every probe position that exists: (position, identifier, available, celsius)."""
    make = make_of(ip, community, timeout, retries)
    probes = []

    if make == "vertiv":
        oids = [VERTIV_UNITS]
        for index in POSITIONS:
            oids += [VERTIV_SERIAL.format(index=index), VERTIV_AVAIL.format(index=index),
                     VERTIV_TEMP.format(index=index)]
        values = _get(ip, oids, community, timeout, retries)
        fahrenheit = values.get(VERTIV_UNITS) == 0
        for index in POSITIONS:
            raw = values.get(VERTIV_TEMP.format(index=index))
            serial = values.get(VERTIV_SERIAL.format(index=index))
            if raw is None and serial is None:
                continue
            celsius = None
            if raw is not None:
                celsius = raw / 10.0
                if fahrenheit:
                    celsius = (celsius - 32) * 5 / 9
                celsius = round(celsius, 1)
            available = values.get(VERTIV_AVAIL.format(index=index)) == 1
            probes.append((index, _text(serial), available, celsius))
        return probes

    # Schneider / APC
    oids = []
    for index in POSITIONS:
        oids += [APC_NAME.format(index=index), APC_TEMP_C.format(index=index)]
    values = _get(ip, oids, community, timeout, retries)
    for index in POSITIONS:
        raw = values.get(APC_TEMP_C.format(index=index))
        if raw is None:
            continue                       # this position does not exist
        available = raw != -1              # -1 means no probe installed
        celsius = round(raw / 10.0, 1) if available else None
        probes.append((index, _text(values.get(APC_NAME.format(index=index))),
                       available, celsius))
    return probes


def read(ip: str, community: str, index: int | None = None, timeout: float = 3.0,
         retries: int = 1, serial: str | None = None) -> float:
    """Temperature in degrees Celsius, from the probe chosen as described above."""
    probes = read_probes(ip, community, timeout, retries)
    if not probes:
        raise CollectorError("no temperature probe found on this PDU")

    if serial:
        wanted = serial.strip().upper()
        chosen = [p for p in probes if p[1].upper() == wanted]
        if not chosen:
            found = ", ".join(p[1] or "?" for p in probes)
            raise CollectorError(f"probe {serial} not found (this PDU has: {found})")
    elif index is not None:
        chosen = [p for p in probes if p[0] == index]
        if not chosen:
            raise CollectorError(f"probe position {index} not found on this PDU")
    else:
        chosen = [p for p in probes if p[2] and p[3] is not None]
        if not chosen:
            raise CollectorError(f"none of the {len(probes)} probe positions has a probe")

    usable = [p for p in chosen if p[2] and p[3] is not None]
    if not usable:
        raise CollectorError(f"probe {chosen[0][1] or chosen[0][0]} reports unavailable")
    return max(p[3] for p in usable)


def main() -> int:
    if len(sys.argv) not in (3, 4):
        print("Usage: python -m dcmonitor.collectors.pdu_sensor <IP> <COMMUNITY> [position|serial]")
        return 2
    ip, community = sys.argv[1], sys.argv[2]
    try:
        print(f"  make: {make_of(ip, community)}")
        for index, ident, available, celsius in read_probes(ip, community):
            state = "available" if available else "no probe / unavailable"
            value = f"{celsius:.1f} C" if celsius is not None else "--"
            print(f"  position {index}  {ident or '-':<18} {state:<24} {value}")
        choice = sys.argv[3] if len(sys.argv) == 4 else None
        if choice is None:
            print(f"automatic choice: {read(ip, community):.1f} C")
        elif choice.isdigit():
            print(f"position {choice}: {read(ip, community, int(choice)):.1f} C")
        else:
            print(f"probe {choice}: {read(ip, community, serial=choice):.1f} C")
    except CollectorError as error:
        print(f"ERROR: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
