"""Read-only Modbus TCP client using only the Python standard library.

Implements exactly one Modbus function: 3, Read Holding Registers. There is
deliberately no code for any write function (5, 6, 15, 16 or others), so this
module cannot change the state of the device it talks to. That matters for
generator controllers, where a write can start or stop the engine.

DSE GenComm addressing: register = 256 * page + offset.

Probe one controller from the command line:
    python -m dcmonitor.modbus_read <IP> --page 4 --count 16
"""

import secrets
import socket
import struct
import sys

READ_HOLDING_REGISTERS = 0x03
MAX_REGISTERS_PER_READ = 125         # Modbus limit for function 3

EXCEPTION_NAMES = {
    1: "illegal function",
    2: "illegal data address",
    3: "illegal data value",
    4: "server device failure",
    5: "acknowledge",
    6: "server device busy",
    10: "gateway path unavailable",
    11: "gateway target failed to respond",
}


class ModbusError(Exception):
    """The device refused the request, or the reply was unusable."""


def _receive_exactly(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ModbusError("connection closed by the device")
        data += chunk
    return data


def read_registers(host: str, start: int, count: int, unit: int = 1,
                   port: int = 502, timeout: float = 5.0) -> list:
    """Read count holding registers from start. Returns a list of 16-bit ints."""
    if not 1 <= count <= MAX_REGISTERS_PER_READ:
        raise ValueError(f"count must be 1 to {MAX_REGISTERS_PER_READ}")
    if not 0 <= start <= 0xFFFF:
        raise ValueError("start must be 0 to 65535")

    transaction = secrets.randbelow(0xFFFF) + 1
    pdu = struct.pack(">BHH", READ_HOLDING_REGISTERS, start, count)
    # MBAP header: transaction, protocol 0, length of unit id + PDU, unit id.
    request = struct.pack(">HHHB", transaction, 0, len(pdu) + 1, unit) + pdu

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)
            header = _receive_exactly(sock, 7)
            reply_transaction, protocol, length, reply_unit = struct.unpack(">HHHB", header)
            body = _receive_exactly(sock, length - 1)
    except (OSError, TimeoutError) as error:
        raise ModbusError(f"{host}:{port}: {error}") from None

    if reply_transaction != transaction or protocol != 0:
        raise ModbusError("reply does not match the request")
    function = body[0]
    if function == READ_HOLDING_REGISTERS | 0x80:
        code = body[1] if len(body) > 1 else 0
        raise ModbusError(f"device refused: {EXCEPTION_NAMES.get(code, f'code {code}')}")
    if function != READ_HOLDING_REGISTERS:
        raise ModbusError(f"unexpected function {function} in reply")

    byte_count = body[1]
    data = body[2:2 + byte_count]
    if byte_count != 2 * count or len(data) != byte_count:
        raise ModbusError(f"expected {2 * count} data bytes, got {len(data)}")
    return list(struct.unpack(f">{count}H", data))


def read_page(host: str, page: int, offset: int, count: int, unit: int = 1,
              port: int = 502, timeout: float = 5.0) -> list:
    """GenComm-style read: page and offset instead of a raw address."""
    return read_registers(host, 256 * page + offset, count, unit, port, timeout)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="read-only Modbus probe")
    parser.add_argument("host")
    parser.add_argument("--page", type=int, default=4)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--count", type=int, default=16)
    parser.add_argument("--units", default="1,10,0,255",
                        help="unit ids to try, comma separated")
    args = parser.parse_args()

    start = 256 * args.page + args.offset
    print(f"reading {args.count} registers from page {args.page} offset "
          f"{args.offset} (address {start}), read-only")
    for unit in [int(value) for value in args.units.split(",")]:
        try:
            values = read_registers(args.host, start, args.count, unit=unit)
        except ModbusError as error:
            print(f"  unit {unit:<3}  {error}")
            continue
        print(f"  unit {unit:<3}  answered")
        for index, value in enumerate(values):
            signed = value - 0x10000 if value & 0x8000 else value
            print(f"    offset {args.offset + index:>3}  {value:>6}  "
                  f"(signed {signed:>6}, hex {value:04X})")
        return 0
    print("no unit id answered")
    return 1


if __name__ == "__main__":
    sys.exit(main())
