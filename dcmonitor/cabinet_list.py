"""The cabinet list: which devices sit in each rack and which PDU outlets feed
them, from the site's Excel workbook on the file share (read only).

The workbook (.xlsx) has one sheet per rack, named by the rack number ("21").
Every other sheet (overviews, rows, customers, other sites) is skipped. A rack
sheet looks like this (the columns are found by their header text, so small
differences between sheets do not matter):

    U/Cabinet | 21***T-CLOUD*** | PDU                       | IP
    42        | Cabinet Switch  | PDU2 /1                   |
    31        | DATADOMAIN-09   | PDU1/5 PDU2/5             |      (merged over U31-30)
    16        | HOST-7          | PDU1/13 PDU2/13           |
    14        | HP Blade Server | PDU1/8-14-18 PDU2/8-16-17 |      (merged over U14-5)

- The column between "U/Cabinet" and "PDU" holds the devices; its header is
  the rack's title (shown as written). A device is one merged block (or one
  cell) in that column; its U range comes from the U/Cabinet column.
- Its PDU cell is the PDU column beside it. "PDU1/5" = PDU 1, outlet 5;
  "PDU1/8-14-18" = PDU 1, outlets 8, 14 and 18 (9 Oct 2026: a dash
  lists outlets, it is not a range). PDU 1 of sheet 21 is the PDU of rack 21
  whose name ends in PDU01 (matched in main.py). Groups may be separated by
  spaces, commas or dashes ("PDU1/5 - PDU2/5", "PDU1/5-PDU2/5"), and may
  carry a power-supply label ("PS1:PDU1/5"); a cell of dashes only is empty.
  A device fed from another rack's PDU names the rack: "RACK1 PDU1/20" (rack
  2's sheet, 9 Oct 2026) is rack 1's PDU01, outlet 20.
- A PDU cell that does not read like that is kept as written and reported;
  nothing is guessed.
- The IP column and the patch panel table to the right are not read.

Reading is read only: the file is opened for reading, copied into memory and
closed at once; it is never written or locked, so saving it in Excel always
works. watch() only asks for the file's time and size; the workbook is read
again only when they change.

Standard library only: an .xlsx file is a zip of XML files. The old .xls
format is not supported.

Check a workbook without showing its contents (counts, rack numbers, and the
shape of PDU cells not understood, letters as A and digits as 9):
    python -m dcmonitor.cabinet_list "<path to the .xlsx>"
"""

import io
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree

RACK_SHEET = re.compile(r"^\s*(\d+)\s*$")
# "PDU1/5", "PDU1/8-14-18", and another rack's PDU: "RACK1 PDU1/20".
FEED = re.compile(r"(?:RACK\s*-?\s*0*(\d+)\s*[-_]?\s*)?"
                  r"PDU\s*-?\s*0*(\d+)\s*/\s*(\d+(?:\s*[-,;]\s*\d+)*)", re.IGNORECASE)
# A power-supply label before a group: "PS1:PDU1/5", also "PS:1PDU1/5".
LABEL = re.compile(r"\b[A-Z]{1,4}\s*\d*\s*:\s*\d*(?=\s*PDU)", re.IGNORECASE)
DASHES = "-\u2013\u2014"
SEPARATORS = re.compile(r"[\s,;/&+\-\u2013\u2014]")
HEADER_ROWS = 10        # the header row is looked for in the first rows only


class CabinetListError(Exception):
    """The file cannot be read as a workbook."""


@dataclass(frozen=True)
class Device:
    name: str
    u_top: int | None
    u_bottom: int | None
    feeds: tuple            # ((rack, pdu number, outlet), ...); rack = the sheet's
                            # own unless the cell names another
    pdu_text: str           # the PDU cell as written
    understood: bool        # False: the PDU cell did not read as PDUn/outlets


@dataclass(frozen=True)
class RackSheet:
    rack: int
    sheet: str
    title: str
    devices: tuple
    u_max: int | None = None    # the highest U on the sheet (the rack's height)


@dataclass
class Workbook:
    racks: dict = field(default_factory=dict)       # rack number -> RackSheet
    skipped: list = field(default_factory=list)     # rack sheets without the header
    skipped_labels: dict = field(default_factory=dict)   # rack -> header_labels()
    other_sheets: int = 0


# ------------------------------------------------------------- xlsx reading

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element, name):
    return [child for child in element if _local(child.tag) == name]


def _text_of(element) -> str:
    """Every <t> below element, joined (rich text runs), phonetic runs left out."""
    parts = []

    def walk(node):
        for child in node:
            name = _local(child.tag)
            if name == "rPh":
                continue
            if name == "t":
                parts.append(child.text or "")
            else:
                walk(child)

    walk(element)
    return "".join(parts)


def _cell_ref(ref: str) -> tuple:
    """"B7" -> (7, 2)."""
    match = re.match(r"([A-Z]+)(\d+)", ref.upper())
    if not match:
        raise CabinetListError(f"bad cell reference {ref!r}")
    column = 0
    for letter in match.group(1):
        column = column * 26 + ord(letter) - 64
    return int(match.group(2)), column


def _xml(archive, name):
    try:
        return ElementTree.fromstring(archive.read(name))
    except KeyError:
        return None
    except ElementTree.ParseError as error:
        raise CabinetListError(f"{name}: {error}") from None


def read_file(path) -> bytes:
    """The whole file, read once and closed (read only, no lock kept)."""
    with open(path, "rb") as handle:
        return handle.read()


def sheets(data: bytes, wanted=None) -> dict:
    """{sheet name: (cells, merges)} for the sheets whose name passes wanted().
    cells: {(row, column): text}; merges: [(row1, col1, row2, col2)]."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise CabinetListError("not an .xlsx workbook (an old .xls file, or still "
                               "being saved)") from None
    with archive:
        workbook = _xml(archive, "xl/workbook.xml")
        rels = _xml(archive, "xl/_rels/workbook.xml.rels")
        if workbook is None or rels is None:
            raise CabinetListError("not an .xlsx workbook")
        targets = {rel.get("Id"): rel.get("Target") for rel in rels}
        shared_xml = _xml(archive, "xl/sharedStrings.xml")
        shared = ([] if shared_xml is None
                  else [_text_of(si) for si in _children(shared_xml, "si")])

        result = {}
        for sheet in (e for e in workbook.iter() if _local(e.tag) == "sheet"):
            name = sheet.get("name", "")
            if wanted is not None and not wanted(name):
                result.setdefault(None, []).append(name)
                continue
            rel_id = next((v for k, v in sheet.attrib.items() if _local(k) == "id"), None)
            target = targets.get(rel_id, "")
            target = target.lstrip("/") if target.startswith("/") else "xl/" + target
            root = _xml(archive, target)
            if root is None:
                continue
            cells = {}
            for cell in (e for e in root.iter() if _local(e.tag) == "c"):
                kind = cell.get("t", "n")
                value = next((c for c in cell if _local(c.tag) == "v"), None)
                if kind == "s" and value is not None and value.text is not None:
                    index = int(value.text)
                    text = shared[index] if 0 <= index < len(shared) else ""
                elif kind == "inlineStr":
                    text = _text_of(cell)
                elif value is not None and value.text is not None:
                    text = value.text
                    if kind == "n" and re.fullmatch(r"-?\d+\.0+", text):
                        text = text.split(".")[0]
                else:
                    continue
                if text.strip():
                    cells[_cell_ref(cell.get("r", ""))] = text
            merges = []
            for merge in (e for e in root.iter() if _local(e.tag) == "mergeCell"):
                first, _, last = merge.get("ref", "").partition(":")
                if first and last:
                    merges.append(_cell_ref(first) + _cell_ref(last))
            result[name] = (cells, merges)
        return result


# ------------------------------------------------------------ rack sheets

def _normal(text: str) -> str:
    return re.sub(r"\s+", "", text).upper()


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_feeds(text: str, rack: int) -> tuple:
    """"PDU1/8-14 RACK1 PDU2/20" on rack 2's sheet ->
    (((2, 1, 8), (2, 1, 14), (1, 2, 20)), True). understood is False when
    anything but such groups is left over."""
    feeds = []
    for match in FEED.finditer(text):
        on_rack = int(match.group(1)) if match.group(1) else rack
        pdu = int(match.group(2))
        for outlet in re.findall(r"\d+", match.group(3)):
            if (on_rack, pdu, int(outlet)) not in feeds:
                feeds.append((on_rack, pdu, int(outlet)))
    rest = FEED.sub("", LABEL.sub("", text))
    understood = bool(feeds) and not SEPARATORS.sub("", rest)
    return tuple(feeds), understood


def _is_u_header(text: str) -> bool:
    """The U column's header: "U/Cabinet" on most sheets."""
    normal = _normal(text)
    return normal in ("U", "U/") or normal.startswith(("U/CAB", "U/KAB", "UNIT"))


def header_labels(cells: dict) -> list:
    """For a sheet whose header was not found: the label-like cells of its
    first rows (containing PDU, CABINET/KABIN, or starting with U), as written,
    with their cell reference. Customer names are not label-like."""
    found = []
    for (row, column), text in sorted(cells.items()):
        if row > HEADER_ROWS:
            break
        normal = _normal(text)
        if len(normal) <= 16 and ("PDU" in normal or "CAB" in normal or "KAB" in normal
                                  or re.match(r"U(\W|$)", normal)):
            found.append(f"{_column_name(column)}{row} {text.strip()!r}")
    return found


def _column_name(column: int) -> str:
    name = ""
    while column:
        column, rest = divmod(column - 1, 26)
        name = chr(65 + rest) + name
    return name


def parse_rack(rack: int, sheet: str, cells: dict, merges: list) -> RackSheet | None:
    """The devices on one rack sheet, or None if its header is not found."""
    header = None
    for (row, column), text in sorted(cells.items()):
        if row > HEADER_ROWS:
            break
        if _is_u_header(text):
            pdu_columns = [c for (r, c), t in cells.items()
                           if r == row and c > column + 1 and _normal(t) == "PDU"]
            if pdu_columns:
                header = (row, column, min(pdu_columns))
                break
    if header is None:
        # Some sheets (9 Oct 2026) have no "U/Cabinet"
        # text: then the "PDU" header, with the U numbers two columns left of
        # it (the devices between), as on every other sheet.
        for (row, column), text in sorted(cells.items()):
            if row > HEADER_ROWS:
                break
            if _normal(text) == "PDU" and column >= 3:
                numbers = [t for (r, c), t in cells.items()
                           if c == column - 2 and r > row and t.strip().isdigit()]
                if len(numbers) >= 3:
                    header = (row, column - 2, column)
                    break
    if header is None:
        return None
    header_row, u_column, pdu_column = header
    device_column = u_column + 1
    title = _one_line(cells.get((header_row, device_column), ""))

    merged_at = {}          # (row, column) -> its merge (r1, c1, r2, c2)
    for merge in merges:
        r1, c1, r2, c2 = merge
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                merged_at[(r, c)] = merge

    def block(row, column):
        """The rows of the block that starts at (row, column), or None."""
        merge = merged_at.get((row, column))
        if merge is not None:
            r1, c1, r2, _c2 = merge
            return range(r1, r2 + 1) if (r1, c1) == (row, column) else None
        return range(row, row + 1) if (row, column) in cells else None

    def u_number(row):
        text = cells.get((row, u_column), "").strip()
        return int(text) if text.isdigit() else None

    last_row = max((r for r, c in cells if c == u_column), default=header_row)
    devices = []
    for row in range(header_row + 1, last_row + 1):
        rows = block(row, device_column)
        name = _one_line(cells.get((row, device_column), "")) if rows else ""
        if not name:
            continue
        units = [u for u in (u_number(r) for r in rows) if u is not None]
        texts = []
        for r in rows:
            merge = merged_at.get((r, pdu_column))
            top = (merge[0], merge[1]) if merge else (r, pdu_column)
            text = _one_line(cells.get(top, ""))
            if text and text not in texts:
                texts.append(text)
        pdu_text = " ".join(texts)
        if not pdu_text.strip(" " + DASHES):      # "-": nothing plugged in
            pdu_text = ""
        feeds, understood = parse_feeds(pdu_text, rack)
        devices.append(Device(name, max(units) if units else None,
                              min(units) if units else None, feeds, pdu_text,
                              understood or not pdu_text))
    numbers = [u for u in (u_number(r) for r in range(header_row + 1, last_row + 1))
               if u is not None]
    return RackSheet(rack, sheet, title, tuple(devices), max(numbers) if numbers else None)


def parse_workbook(data: bytes) -> Workbook:
    found = sheets(data, wanted=lambda name: bool(RACK_SHEET.match(name)))
    book = Workbook(other_sheets=len(found.pop(None, [])))
    for name, (cells, merges) in found.items():
        rack = int(RACK_SHEET.match(name).group(1))
        sheet = parse_rack(rack, name, cells, merges)
        if sheet is None:
            book.skipped.append(rack)
            book.skipped_labels[rack] = header_labels(cells)
        else:
            book.racks[rack] = sheet
    return book


# ---------------------------------------------------------------- watcher

class CabinetList:
    """The workbook on the share, read again whenever its time or size changes.
    A failed read keeps the last good copy and is tried again next time."""

    def __init__(self, path: str):
        self.path = path
        self.book = Workbook()
        self.file_time = None       # the file's modified time (epoch s), last good read
        self.error = ""             # why the last check failed, "" when fine
        self._signature = None

    def check(self) -> bool:
        """True when a new version was read."""
        try:
            stat = os.stat(self.path)
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature == self._signature:
                self.error = ""
                return False
            book = parse_workbook(read_file(self.path))
        except OSError as error:
            # strerror only: the full message holds the path, with the
            # server's address.
            self.error = error.strerror or type(error).__name__
            return False
        except (CabinetListError, ValueError) as error:
            self.error = str(error)
            return False
        self.book, self.file_time, self.error = book, stat.st_mtime, ""
        self._signature = signature
        return True


# -------------------------------------------------------------------- check

def _shape(text: str) -> str:
    """The cell's form without its contents: letters A, digits 9."""
    return re.sub(r"\d", "9", re.sub(r"[^\W\d_]", "A", text))


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.rsplit("Check a workbook", 1)[-1].strip())
        return 2
    try:
        book = parse_workbook(read_file(sys.argv[1]))
    except (OSError, CabinetListError) as error:
        print(f"cannot read: {type(error).__name__}: {error}")
        return 1
    devices = [d for sheet in book.racks.values() for d in sheet.devices]
    with_feeds = [d for d in devices if d.feeds]
    unclear = [(sheet.rack, d) for sheet in book.racks.values()
               for d in sheet.devices if not d.understood]
    print(f"rack sheets read: {len(book.racks)}  (racks {_ranges(sorted(book.racks))})")
    print(f"rack sheets without the U/Cabinet ... PDU header: {len(book.skipped)}"
          + (f"  (racks {_ranges(sorted(book.skipped))})" if book.skipped else ""))
    for rack in sorted(book.skipped):
        labels = book.skipped_labels.get(rack) or ["no label-like cell in the first rows"]
        print(f"  rack {rack:>3}: {', '.join(labels)}")
    print(f"other sheets skipped: {book.other_sheets}")
    print(f"devices: {len(devices)}, with PDU outlets: {len(with_feeds)}, "
          f"outlets: {sum(len(d.feeds) for d in devices)}")
    pdus = sorted({p for d in devices for _r, p, _o in d.feeds})
    print(f"PDU numbers used: {', '.join(map(str, pdus)) or '-'}")
    missing = _missing_pdus(book)
    if missing is not None:
        print(f"outlets on a PDU number the rack does not have in pdus.toml: "
              f"{len(missing)}")
        for rack, number in missing:
            print(f"  rack {rack:>3}  PDU{number}")
    print(f"PDU cells not understood: {len(unclear)}")
    for rack, device in unclear:
        where = ("" if device.u_top is None else f"U{device.u_top}"
                 + ("" if device.u_bottom == device.u_top else f"-{device.u_bottom}"))
        rest = _not_understood(device.pdu_text)
        print(f"  rack {rack:>3}  {where:<8} {_shape(device.pdu_text)}"
              + (f"   not understood: {_shape(rest)}" if rest else ""))
    return 0


def _missing_pdus(book) -> list | None:
    """(rack, PDU number) used in the list but not in pdus.toml, when run
    from the program folder (config.toml there); None otherwise."""
    if not os.path.isfile("config.toml"):
        return None
    try:
        from dcmonitor import config as config_module
        pdus = (config_module.load("config.toml").pdus or {}).get("entries", ())
    except Exception:
        return None
    have = set()
    for entry in pdus:
        match = re.search(r"PDU\D*0*(\d+)\s*$", entry["name"], re.IGNORECASE)
        if match and entry["rack"] is not None:
            have.add((entry["rack"], int(match.group(1))))
    return sorted({(rack, number) for sheet in book.racks.values()
                   for device in sheet.devices for rack, number, _outlet in device.feeds
                   if (rack, number) not in have})


def _not_understood(text: str) -> str:
    """What is left of a PDU cell once the groups read are taken out."""
    rest = FEED.sub(" ", LABEL.sub("", text))
    return re.sub(r"[\s,;&+\-\u2013\u2014]+", " ", rest).strip()


def _ranges(numbers) -> str:
    parts, start, prev = [], None, None
    for n in numbers:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            parts.append(f"{start}-{prev}" if prev != start else str(start))
            start = prev = n
    if start is not None:
        parts.append(f"{start}-{prev}" if prev != start else str(start))
    return ", ".join(parts)


if __name__ == "__main__":
    sys.exit(main())
