"""Minimal SNMP v1 GET client using only the Python standard library.

Supports GET requests for one or more OIDs with a community string.
Values are returned as Python types:
    INTEGER                     -> int (signed)
    Counter32, Gauge32, TimeTicks -> int (unsigned)
    OCTET STRING                -> bytes
    OBJECT IDENTIFIER           -> str
    IpAddress                   -> str
    NULL                        -> None
"""

import secrets
import socket
import time

TAG_INTEGER = 0x02
TAG_OCTET_STRING = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_SEQUENCE = 0x30
TAG_IP_ADDRESS = 0x40
TAG_COUNTER32 = 0x41
TAG_GAUGE32 = 0x42
TAG_TIMETICKS = 0x43
TAG_GET_REQUEST = 0xA0
TAG_GET_NEXT_REQUEST = 0xA1
TAG_GET_RESPONSE = 0xA2

SNMP_VERSION_1 = 0
SNMP_VERSION_2C = 1

# SNMP v2c answers a missing OID with one of these in place of a value.
TAG_NO_SUCH_OBJECT = 0x80
TAG_NO_SUCH_INSTANCE = 0x81
TAG_END_OF_MIB_VIEW = 0x82

ERROR_STATUS_NAMES = {
    0: "noError",
    1: "tooBig",
    2: "noSuchName",
    3: "badValue",
    4: "readOnly",
    5: "genErr",
}


class SnmpError(Exception):
    """The agent answered with an error, or the reply could not be parsed."""


class SnmpTimeout(SnmpError):
    """No valid reply arrived within timeout and retries."""


# ---------------------------------------------------------------- encoding

def _encode_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _encode_length(len(value)) + value


def _encode_integer(value: int) -> bytes:
    size = max(1, (value.bit_length() + 8) // 8)
    return _tlv(TAG_INTEGER, value.to_bytes(size, "big", signed=True))


def _encode_arc(arc: int) -> bytes:
    chunks = [arc & 0x7F]
    arc >>= 7
    while arc:
        chunks.append(0x80 | (arc & 0x7F))
        arc >>= 7
    return bytes(reversed(chunks))


def _encode_oid(oid: str) -> bytes:
    arcs = [int(part) for part in oid.strip(".").split(".")]
    if len(arcs) < 2:
        raise ValueError(f"OID needs at least two arcs: {oid!r}")
    body = _encode_arc(arcs[0] * 40 + arcs[1])
    body += b"".join(_encode_arc(arc) for arc in arcs[2:])
    return _tlv(TAG_OID, body)


def _build_get_request(community: str, request_id: int, oids: list[str],
                       version: int = SNMP_VERSION_1,
                       pdu_tag: int = TAG_GET_REQUEST) -> bytes:
    varbinds = b"".join(
        _tlv(TAG_SEQUENCE, _encode_oid(oid) + _tlv(TAG_NULL, b""))
        for oid in oids
    )
    pdu = _tlv(
        pdu_tag,
        _encode_integer(request_id)
        + _encode_integer(0)  # error-status
        + _encode_integer(0)  # error-index
        + _tlv(TAG_SEQUENCE, varbinds),
    )
    return _tlv(
        TAG_SEQUENCE,
        _encode_integer(version)
        + _tlv(TAG_OCTET_STRING, community.encode("ascii"))
        + pdu,
    )


# ---------------------------------------------------------------- decoding

def _read_tlv(data: bytes, pos: int) -> tuple[int, bytes, int]:
    """Read one TLV at pos. Returns (tag, value, position after the TLV)."""
    if pos + 2 > len(data):
        raise SnmpError("reply truncated in TLV header")
    tag = data[pos]
    first = data[pos + 1]
    pos += 2
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if count == 0 or pos + count > len(data):
            raise SnmpError("invalid length field in reply")
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    end = pos + length
    if end > len(data):
        raise SnmpError("reply truncated in TLV value")
    return tag, data[pos:end], end


def _expect(data: bytes, pos: int, expected_tag: int) -> tuple[bytes, int]:
    tag, value, end = _read_tlv(data, pos)
    if tag != expected_tag:
        raise SnmpError(f"expected tag 0x{expected_tag:02X}, got 0x{tag:02X}")
    return value, end


def _decode_oid(raw: bytes) -> str:
    arcs = []
    value = 0
    for byte in raw:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            arcs.append(value)
            value = 0
    if not arcs:
        raise SnmpError("empty OID in reply")
    first = arcs[0]
    if first < 40:
        head = [0, first]
    elif first < 80:
        head = [1, first - 40]
    else:
        head = [2, first - 80]
    return ".".join(str(arc) for arc in head + arcs[1:])


def _decode_value(tag: int, raw: bytes):
    if tag == TAG_INTEGER:
        return int.from_bytes(raw, "big", signed=True)
    if tag in (TAG_COUNTER32, TAG_GAUGE32, TAG_TIMETICKS):
        return int.from_bytes(raw, "big", signed=False)
    if tag == TAG_OCTET_STRING:
        return bytes(raw)
    if tag == TAG_NULL:
        return None
    if tag == TAG_OID:
        return _decode_oid(raw)
    if tag == TAG_IP_ADDRESS and len(raw) == 4:
        return ".".join(str(b) for b in raw)
    if tag in (TAG_NO_SUCH_OBJECT, TAG_NO_SUCH_INSTANCE, TAG_END_OF_MIB_VIEW):
        return None       # v2c: this OID does not exist on the agent
    raise SnmpError(f"unsupported value type 0x{tag:02X}")


def _parse_get_response(data: bytes):
    message, _ = _expect(data, 0, TAG_SEQUENCE)
    _, pos = _expect(message, 0, TAG_INTEGER)        # version
    _, pos = _expect(message, pos, TAG_OCTET_STRING)  # community
    pdu, _ = _expect(message, pos, TAG_GET_RESPONSE)

    raw, pos = _expect(pdu, 0, TAG_INTEGER)
    request_id = int.from_bytes(raw, "big", signed=True)
    raw, pos = _expect(pdu, pos, TAG_INTEGER)
    error_status = int.from_bytes(raw, "big", signed=True)
    raw, pos = _expect(pdu, pos, TAG_INTEGER)
    error_index = int.from_bytes(raw, "big", signed=True)
    varbind_list, _ = _expect(pdu, pos, TAG_SEQUENCE)

    varbinds = []
    pos = 0
    while pos < len(varbind_list):
        varbind, pos = _expect(varbind_list, pos, TAG_SEQUENCE)
        oid_raw, inner = _expect(varbind, 0, TAG_OID)
        value_tag, value_raw, _ = _read_tlv(varbind, inner)
        varbinds.append((_decode_oid(oid_raw), value_tag, value_raw))

    return request_id, error_status, error_index, varbinds


# ---------------------------------------------------------------- public API

def _request(host, oids, community, port, timeout, retries, version, pdu_tag):
    """Send one request and return (error_status, error_index, varbinds)."""
    if version not in ("1", "2c"):
        raise ValueError('version must be "1" or "2c"')
    target_ip = socket.gethostbyname(host)
    request_id = secrets.randbelow(0x7FFFFFFF) + 1
    message = _build_get_request(
        community, request_id, oids,
        SNMP_VERSION_2C if version == "2c" else SNMP_VERSION_1, pdu_tag,
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _attempt in range(retries + 1):
            sock.sendto(message, (target_ip, port))
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    data, (sender_ip, _) = sock.recvfrom(65535)
                except TimeoutError:
                    break
                except ConnectionResetError:
                    raise SnmpError(f"{host}: port {port} unreachable") from None
                if sender_ip != target_ip:
                    continue
                try:
                    reply_id, error_status, error_index, varbinds = _parse_get_response(data)
                except SnmpError:
                    continue
                if reply_id == request_id:
                    return error_status, error_index, varbinds
    raise SnmpTimeout(f"{host}: no reply after {retries + 1} attempts ({timeout}s each)")


def snmp_walk(host: str, base_oid: str, community: str = "public", port: int = 161,
              timeout: float = 2.0, retries: int = 2, version: str = "1",
              max_rows: int = 200) -> dict:
    """Every value below base_oid, by repeated GETNEXT. {oid: value}.

    Used for tables whose rows come and go, such as the UPS alarm table,
    which only has rows while alarms are active. Read only, like snmp_get.
    """
    prefix = base_oid.strip(".") + "."
    current = base_oid.strip(".")
    found = {}
    for _step in range(max_rows):
        status, _index, varbinds = _request(host, [current], community, port, timeout,
                                            retries, version, TAG_GET_NEXT_REQUEST)
        if status == 2:                      # v1 noSuchName: past the end of the MIB
            break
        if status != 0:
            name = ERROR_STATUS_NAMES.get(status, str(status))
            raise SnmpError(f"{host}: agent returned {name} walking {base_oid}")
        if not varbinds:
            break
        oid, tag, raw = varbinds[0]
        if not oid.startswith(prefix):       # left the subtree: done
            break
        value = _decode_value(tag, raw)
        if tag in (TAG_END_OF_MIB_VIEW,):
            break
        found[oid] = value
        current = oid
    return found


def snmp_get(
    host: str,
    oids: list[str],
    community: str = "public",
    port: int = 161,
    timeout: float = 2.0,
    retries: int = 2,
    version: str = "1",
) -> dict[str, object]:
    """Send one SNMP GET (v1 by default, or "2c") for all oids.

    Returns {oid: value}. Under v2c an OID the agent does not have comes
    back as None rather than failing the whole request.

    OID keys are returned without a leading dot.
    Raises SnmpTimeout if no reply arrives, SnmpError on agent errors.
    """
    if not oids:
        raise ValueError("oids must not be empty")

    target_ip = socket.gethostbyname(host)
    request_id = secrets.randbelow(0x7FFFFFFF) + 1
    if version not in ("1", "2c"):
        raise ValueError('version must be "1" or "2c"')
    message = _build_get_request(
        community, request_id, oids,
        SNMP_VERSION_2C if version == "2c" else SNMP_VERSION_1,
    )

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _attempt in range(retries + 1):
            sock.sendto(message, (target_ip, port))
            deadline = time.monotonic() + timeout

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    data, (sender_ip, _) = sock.recvfrom(65535)
                except TimeoutError:
                    break
                except ConnectionResetError:
                    # Windows reports an ICMP port-unreachable this way.
                    raise SnmpError(f"{host}: port {port} unreachable") from None

                if sender_ip != target_ip:
                    continue
                try:
                    reply_id, error_status, error_index, varbinds = (
                        _parse_get_response(data)
                    )
                except SnmpError:
                    continue
                if reply_id != request_id:
                    continue

                if error_status != 0:
                    name = ERROR_STATUS_NAMES.get(error_status, str(error_status))
                    where = ""
                    if 1 <= error_index <= len(oids):
                        where = f" on {oids[error_index - 1]}"
                    raise SnmpError(f"{host}: agent returned {name}{where}")

                return {
                    oid: _decode_value(tag, raw) for oid, tag, raw in varbinds
                }

    raise SnmpTimeout(
        f"{host}: no reply after {retries + 1} attempts ({timeout}s each)"
    )
