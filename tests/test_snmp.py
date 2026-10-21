"""Test snmp_v1.py against one UPS.

Usage:
    python test_snmp.py <UPS-IP>

Part 1 sends one GET per OID and prints each value with its response time.
Part 2 sends all OIDs in a single GET, to find out whether the NetMan
agent accepts multi-OID requests.
"""

import sys
import time

from snmp_v1 import SnmpError, snmp_get

UPS_OIDS = {
    "1.3.6.1.2.1.33.1.1.5.0": "UPS name",
    "1.3.6.1.2.1.33.1.2.4.0": "Battery charge %",
    "1.3.6.1.2.1.33.1.2.7.0": "Battery temperature C",
    "1.3.6.1.2.1.33.1.4.4.1.5.1": "Output load % phase 1",
    "1.3.6.1.2.1.33.1.4.4.1.5.2": "Output load % phase 2",
    "1.3.6.1.2.1.33.1.4.4.1.5.3": "Output load % phase 3",
}


def show(value) -> str:
    if isinstance(value, bytes):
        return repr(value.decode("latin-1"))
    return repr(value)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python test_snmp.py <UPS-IP>")
        return 2
    host = sys.argv[1]

    print("Part 1: one GET per OID")
    for oid, label in UPS_OIDS.items():
        started = time.monotonic()
        try:
            result = snmp_get(host, [oid])
            elapsed_ms = (time.monotonic() - started) * 1000
            print(f"  {label:24} {show(result.get(oid))}  ({elapsed_ms:.0f} ms)")
        except SnmpError as error:
            print(f"  {label:24} ERROR: {error}")

    print()
    print("Part 2: all OIDs in one GET")
    started = time.monotonic()
    try:
        result = snmp_get(host, list(UPS_OIDS))
        elapsed_ms = (time.monotonic() - started) * 1000
        print(f"  reply in {elapsed_ms:.0f} ms, {len(result)} values")
        for oid, value in result.items():
            print(f"  {UPS_OIDS.get(oid, oid):24} {show(value)}")
    except SnmpError as error:
        print(f"  ERROR: {error}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
