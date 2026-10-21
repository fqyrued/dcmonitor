"""Floor map for the dcmonitor panel.

Draws the room from site_map.toml: the outline, every rack in its real
position coloured by temperature, the AC units with their live state, and
fixed landmarks for orientation. The layout is drawn on a grid and scaled to
the canvas, so the map fills whatever space the window gives it.

Data the map expects, all optional:

    racks = {75: {"value": 19.4, "low": 19.3, "high": 19.5,
                  "time": "22.09.2026 12:47", "stale": False}}
        A rack missing from this dict has no sensor. A rack with value None
        has a sensor that reports nothing.

    acs = {"AC-3": {"temp": 23.5, "state": "ok", "status": "Unit On"}}
        state is "ok", "alarm" or "stale".

Clicking a rack or AC unit opens a details popup next to it: for a rack its
temperature, power and every circuit as a table. It stays through the
map's redraws until the user clicks elsewhere on the map or its "x". Each
PDU in it has a Maintenance button (End maintenance while it is in
maintenance); a rack with a PDU in maintenance has a grey "M".
"""

import tkinter as tk
import tomllib
from datetime import datetime, timezone
from pathlib import Path

BACKGROUND = "#15171c"
FLOOR = "#1b1e24"
WALL = "#3a404a"
TEXT = "#f2f4f8"
TEXT_DIM = "#9aa3b2"
TEXT_DETAIL = "#c9d0db"      # secondary text in the popup: brighter than TEXT_DIM
TEXT_FAINT = "#5f6672"
NO_SENSOR = "#2a2e36"
NO_SENSOR_EDGE = "#353a44"
CAGE = "#3a2426"
LANDMARK = "#262a32"
AC_FILL = "#20242c"
STATE_COLOURS = {"ok": "#2e9e5b", "alarm": "#e5484d", "stale": "#5f6672",
                 "standby": "#8a93a3",    # standby: grey, dashed frame, "standby"
                 "off": "#6b7280"}        # unit off: dark solid frame, hollow dot, "off"
POPUP_BG = "#101216"
POPUP_EDGE = "#4a515d"
ALARM_TEXT = "#ff7b7f"       # a circuit at or above the breaker alarm level
WARN_TEXT = "#e0a030"        # a circuit at or above the warning level, and stale readings
HIGHLIGHT_TEXT = "#e8d24a"   # a circuit at or above the highlight level
MAINTENANCE = "#8a93a3"      # a PDU in maintenance: its mark, name and note
BUTTON = "#2c313a"

# Breaker mode colours the racks in bands, matching the alarm levels.
BREAKER_BANDS = {"normal": "#3fa34d", "highlight": "#d4b62a",
                 "warning": "#e08a2c", "alarm": "#e0453a"}
POPUP_WIDTH = 440            # the popup's width; a long PDU name widens it

# Temperature colour scale, degrees C to colour, blended between stops.
# Red only from 30 C (2 Oct 2026; was 27).
TEMPERATURE_STOPS = (
    (17.0, (43, 108, 176)),     # cool blue
    (20.0, (47, 158, 143)),     # teal
    (24.0, (63, 163, 77)),      # green
    (27.0, (214, 158, 46)),     # amber
    (29.9, (230, 118, 40)),     # orange: still below the limit
    (30.0, (224, 69, 58)),      # red from 30, a clear step
)


class MapError(Exception):
    """site_map.toml is missing or malformed."""


def load_layout(path) -> dict:
    """Read and check site_map.toml."""
    path = Path(path)
    if not path.is_file():
        raise MapError(f"floor map not found: {path}")
    try:
        with path.open("rb") as handle:
            layout = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise MapError(f"{path}: {error}") from None

    outline = layout.get("room", {}).get("outline")
    if not isinstance(outline, list) or len(outline) < 3:
        raise MapError("[room] outline needs at least three points")

    racks = {}
    for row in layout.get("rows", []):
        for index, item in enumerate(row.get("items", [])):
            position = (row["x"] + index, row["y"])
            if isinstance(item, int):
                if item in racks:
                    raise MapError(f"rack {item} appears twice in the map")
                racks[item] = position
    layout["_racks"] = racks
    return layout


# Power per rack in kW, and the highest breaker load in a rack in % of its
# limit. Breakers turn amber from 60 % and red from 80 %.
POWER_STOPS = (
    (0.0, (43, 108, 176)),
    (1.5, (47, 158, 143)),
    (3.0, (63, 163, 77)),
    (5.0, (214, 158, 46)),
    (7.0, (224, 69, 58)),
)
BREAKER_STOPS = (
    (0.0, (47, 158, 143)),
    (40.0, (63, 163, 77)),
    (60.0, (214, 158, 46)),
    (80.0, (224, 69, 58)),
)

MODES = {
    "temperature": {"stops": TEMPERATURE_STOPS, "ticks": (17, 20, 24, 27, 30),
                    "tick": "{:g}\u00b0", "unit": "\u00b0C", "value": "{:.1f}",
                    "missing": "no temperature sensor"},
    "power": {"stops": POWER_STOPS, "ticks": (0, 1.5, 3, 5, 7),
              "tick": "{:g} kW", "unit": "kW", "value": "{:.1f}",
              "missing": "no PDU power data"},
    "breakers": {"stops": BREAKER_STOPS, "ticks": (0, 40, 60, 80),
                 "tick": "{:g} %", "unit": "%", "value": "{:.0f}",
                 "missing": "no breaker data"},
}


def scale_colour(value: float, stops) -> str:
    """Blend between a scale's stops; values outside clamp to the ends."""
    if value <= stops[0][0]:
        rgb = stops[0][1]
    elif value >= stops[-1][0]:
        rgb = stops[-1][1]
    else:
        for (low, low_rgb), (high, high_rgb) in zip(stops, stops[1:]):
            if low <= value <= high:
                share = (value - low) / (high - low)
                rgb = tuple(round(a + (b - a) * share) for a, b in zip(low_rgb, high_rgb))
                break
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def temperature_colour(value: float) -> str:
    """Blend between the scale's stops; values outside clamp to the ends."""
    stops = TEMPERATURE_STOPS
    if value <= stops[0][0]:
        rgb = stops[0][1]
    elif value >= stops[-1][0]:
        rgb = stops[-1][1]
    else:
        for (low, low_rgb), (high, high_rgb) in zip(stops, stops[1:]):
            if low <= value <= high:
                share = (value - low) / (high - low)
                rgb = tuple(round(a + (b - a) * share) for a, b in zip(low_rgb, high_rgb))
                break
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def readable_on(colour: str) -> str:
    """Dark text on light fills, light text on dark ones."""
    red, green, blue = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
    luminance = 0.299 * red + 0.587 * green + 0.114 * blue
    return "#101216" if luminance > 150 else TEXT


def _local(utc_text: str) -> str:
    """A stored UTC time ("2026-10-02T11:05:00Z") as local "02.10 14:05"."""
    try:
        moment = datetime.strptime(utc_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return str(utc_text)
    return moment.astimezone().strftime("%d.%m %H:%M")


class FloorMap(tk.Canvas):
    MARGIN = 20

    def __init__(self, parent, layout: dict):
        super().__init__(parent, bg=BACKGROUND, highlightthickness=0, bd=0)
        self.layout = layout
        self.racks = {}
        self.acs = {}
        self.power = {}          # rack -> {"value": kW, "lines": [...], ...}
        self.breakers = {}       # rack -> {"value": highest %, "lines": [...], ...}
        self.mode = "temperature"
        self.breaker_alarm_pct = None       # set by the panel from [pdus]; the
        self.breaker_warning_pct = None     # breaker levels colour the racks in
        self.breaker_highlight_pct = None   # breaker mode and the circuit table
        self.pdu_health = {}            # rack -> PDUs that do not answer
        self.follow_alarms = {}         # rack -> open circuit follower messages
        self.normals = {}               # circuit key -> normal amps (follower)
        self.on_accept = None           # callback(circuit key) for the Accept buttons
        self.maintenance = {}           # PDU id -> {"name", "rack", "since_utc"}
        self.on_maintenance = None      # callback(PDU id, on) for the Maintenance buttons
        self.device_counts = {}         # rack -> devices in the cabinet list
        self.on_devices = None          # callback(rack) for the Devices button
        self.pinned = None              # ("rack", number) or ("ac", name) with a popup open
        self._targets = {}              # canvas item -> ("rack", number) / ("ac", name)
        self._anchors = {}              # same key -> its tile's box, to place the popup
        self._ac_lines = {}             # AC name -> its detail lines
        self.bind("<Configure>", lambda _event: self.redraw())
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._pointer)

    def show(self, racks: dict, acs: dict, power: dict | None = None,
             breakers: dict | None = None) -> None:
        self.racks, self.acs = racks, acs
        self.power = power or {}
        self.breakers = breakers or {}
        self.redraw()

    def set_mode(self, mode: str) -> None:
        """Temperature, power or breakers: what the rack tiles show."""
        if mode in MODES and mode != self.mode:
            self.mode = mode
            self.redraw()

    # ----------------------------------------------------------- geometry

    def _extent(self):
        points = list(self.layout["room"]["outline"])
        for item in self.layout.get("ac", []) + self.layout.get("landmarks", []):
            points += [(item["x"], item["y"]), (item["x"] + item["w"], item["y"] + item["h"])]
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return min(xs), min(ys), max(xs) + 1, max(ys) + 1

    def _transform(self):
        min_x, min_y, max_x, max_y = self._extent()
        width = self.winfo_width() - 2 * self.MARGIN
        height = self.winfo_height() - 2 * self.MARGIN - 40    # room for the legend
        cell = min(width / (max_x - min_x), height / (max_y - min_y))
        offset_x = self.MARGIN + (width - cell * (max_x - min_x)) / 2
        offset_y = self.MARGIN

        def to_screen(x, y):
            return offset_x + (x - min_x) * cell, offset_y + (y - min_y) * cell

        self._map_bottom = offset_y + (max_y - min_y) * cell
        self._map_left = offset_x
        return cell, to_screen

    def _rounded(self, x1, y1, x2, y2, radius, **kwargs):
        radius = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]
        return self.create_polygon(points, smooth=True, **kwargs)

    def _fitted_text(self, x, y, text, width, size, colour, bold=False, **kwargs):
        """Draw text no wider than width, shrinking the font if needed."""
        weight = ("bold",) if bold else ()
        while True:
            item = self.create_text(x, y, text=text, fill=colour,
                                    font=("Helvetica", size) + weight, **kwargs)
            x1, _y1, x2, _y2 = self.bbox(item)
            if x2 - x1 <= width or size <= 6:
                return item
            self.delete(item)
            size -= 1

    # ------------------------------------------------------------ drawing

    def redraw(self) -> None:
        self.delete("all")
        self._targets, self._anchors, self._ac_lines = {}, {}, {}
        if self.winfo_width() < 200 or self.winfo_height() < 150:
            return
        cell, to_screen = self._transform()

        # Room: floor and walls.
        outline = [c for point in self.layout["room"]["outline"] for c in to_screen(*point)]
        self.create_polygon(outline, fill=FLOOR, outline=WALL, width=2)

        # Landmarks.
        for item in self.layout.get("landmarks", []):
            x1, y1 = to_screen(item["x"], item["y"])
            x2, y2 = to_screen(item["x"] + item["w"], item["y"] + item["h"])
            if item.get("room"):
                self.create_rectangle(x1, y1, x2, y2, fill=FLOOR, outline=WALL,
                                      width=2, dash=(6, 4))
                self.create_text((x1 + x2) / 2, (y1 + y2) / 2, text=item["label"],
                                 fill=TEXT_FAINT, font=("Helvetica", max(9, int(cell * 0.6)), "bold"))
            else:
                self._rounded(x1, y1, x2, y2, cell * 0.2, fill=LANDMARK, outline="")
                # A label may hold a line break for a narrow box ("UPS\npanel 2").
                self._fitted_text((x1 + x2) / 2, (y1 + y2) / 2, item["label"],
                                  (x2 - x1) - 8, max(7, int(cell * 0.32)), TEXT_FAINT,
                                  justify="center")

        # Racks, with their numbers along the row.
        number_font = ("Helvetica", max(7, int(cell * 0.3)))
        value_font = ("Helvetica", max(7, int(cell * 0.34)), "bold")
        gap = max(1.5, cell * 0.08)
        for row in self.layout.get("rows", []):
            for index, item in enumerate(row["items"]):
                x, y = row["x"] + index, row["y"]
                x1, y1 = to_screen(x, y)
                x2, y2 = to_screen(x + 1, y + 1)
                x1, y1, x2, y2 = x1 + gap, y1 + gap, x2 - gap, y2 - gap
                if item == "gap":
                    continue
                if item == "cage":
                    self._rounded(x1, y1, x2, y2, cell * 0.15, fill=CAGE, outline="")
                    self.create_line(x1 + 3, y2 - 3, x2 - 3, y1 + 3, fill="#6b3a3d", width=1.5)
                    continue
                self._draw_rack(item, x1, y1, x2, y2, cell, number_font, value_font)
                self._draw_health_marker(item, x1, y1, x2, y2, cell)
                self._draw_maintenance_marker(item, x1, y1, x2, y2, cell)

        # AC units.
        for item in self.layout.get("ac", []):
            x1, y1 = to_screen(item["x"], item["y"])
            x2, y2 = to_screen(item["x"] + item["w"], item["y"] + item["h"])
            self._draw_ac(item["name"], x1, y1, x2, y2, cell)

        self._draw_legend(cell)
        if self.pinned is not None:
            self._draw_popup()

    def _draw_rack(self, number, x1, y1, x2, y2, cell, number_font, value_font):
        mode = MODES[self.mode]
        source = {"temperature": self.racks, "power": self.power,
                  "breakers": self.breakers}[self.mode]
        data = source.get(number)
        self.create_text((x1 + x2) / 2, y1 - cell * 0.22, text=str(number),
                         fill=TEXT_FAINT, font=number_font)
        if data is None:
            shape = self._rounded(x1, y1, x2, y2, cell * 0.15, fill=NO_SENSOR,
                                  outline=NO_SENSOR_EDGE)
            self._target(shape, ("rack", number), (x1, y1, x2, y2))
            return

        value = data.get("value")
        if value is None:
            shape = self._rounded(x1, y1, x2, y2, cell * 0.15, fill=NO_SENSOR,
                                  outline="#8a6a2a", width=1.5, dash=(3, 2))
            self.create_text((x1 + x2) / 2, (y1 + y2) / 2, text="?",
                             fill="#c8a24a", font=value_font)
            self._target(shape, ("rack", number), (x1, y1, x2, y2))
            return

        if self.mode == "breakers":
            colour = BREAKER_BANDS[self._breaker_band(value)]
        else:
            colour = scale_colour(value, mode["stops"])
        stale = data.get("stale")
        shape = self._rounded(x1, y1, x2, y2, cell * 0.15,
                              fill=colour if not stale else NO_SENSOR,
                              outline=colour if stale else "",
                              width=1.5 if stale else 1)
        text_colour = readable_on(colour) if not stale else TEXT_DIM
        middle, height = (x1 + x2) / 2, y2 - y1
        self._fitted_text(middle, y1 + height * 0.40, mode["value"].format(value),
                          (x2 - x1) - 3, value_font[1], text_colour, bold=True)
        self.create_text(middle, y1 + height * 0.76, text=mode["unit"], fill=text_colour,
                         font=("Helvetica", max(6, int(cell * 0.24))))

        self._target(shape, ("rack", number), (x1, y1, x2, y2))

    def _draw_health_marker(self, number, x1, y1, x2, y2, cell) -> None:
        """An amber "!" on a rack with a PDU that does not answer, in every
        map mode, since that PDU's power, breakers and temperatures are gaps."""
        if not any(entry["problem"] for entry in self.pdu_health.get(number, [])):
            return
        # Big enough to spot from across the room: about a third of a rack
        # tile, in its top-right corner (low enough to keep the rack number
        # above the tile readable), with a dark ring so it stands out on any
        # tile colour.
        radius = max(9, cell * 0.34)
        cx, cy = x2 - radius * 0.35, y1 + radius * 0.75
        self.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                         fill=WARN_TEXT, outline=BACKGROUND, width=2.5)
        self.create_text(cx, cy, text="!", fill=BACKGROUND,
                         font=("Helvetica", max(10, int(radius * 1.25)), "bold"))

    def _draw_maintenance_marker(self, number, x1, y1, x2, y2, cell) -> None:
        """A grey "M" in the top-left corner of a rack with a PDU in
        maintenance, so nobody forgets it is muted."""
        if not any(entry.get("rack") == number for entry in self.maintenance.values()):
            return
        radius = max(9, cell * 0.34)
        cx, cy = x1 + radius * 0.35, y1 + radius * 0.75
        self.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                         fill=MAINTENANCE, outline=BACKGROUND, width=2.5)
        self.create_text(cx, cy, text="M", fill=BACKGROUND,
                         font=("Helvetica", max(9, int(radius * 1.05)), "bold"))

    def _draw_ac(self, name, x1, y1, x2, y2, cell):
        data = self.acs.get(name, {})
        state = data.get("state", "stale")
        accent = STATE_COLOURS.get(state, STATE_COLOURS["stale"])
        standby, off = state == "standby", state == "off"
        idle = standby or off
        shape = self._rounded(x1, y1, x2, y2, cell * 0.2,
                              fill=BACKGROUND if idle else AC_FILL, outline=accent,
                              width=2 if state in ("alarm", "standby", "off") else 1.2,
                              dash=(4, 3) if standby else None)
        name_colour = TEXT_DIM if idle else TEXT
        temp = data.get("temp")
        temp_text = f"{temp:.1f}\u00b0" if temp is not None else "--"
        # A unit that is not cooling says so in place of its temperature;
        # the temperature is still in its popup.
        shown = "standby" if standby else "off" if off else temp_text
        box_width = x2 - x1
        size = max(7, int(cell * 0.32))
        dot = max(2.5, cell * 0.12)

        if box_width >= cell * 2.8:
            # Wide: name left, temperature right, dot at the far right.
            self._fitted_text(x1 + cell * 0.3, (y1 + y2) / 2, name, box_width * 0.45,
                              size, name_colour, bold=True, anchor="w")
            self.create_text(x2 - cell * 0.55, (y1 + y2) / 2, anchor="e", text=shown,
                             fill=TEXT_DIM, font=("Helvetica", size))
            centre_x, centre_y = x2 - cell * 0.32, (y1 + y2) / 2
        else:
            # Narrow or tall: name above, temperature below, dot in the corner.
            middle = (x1 + x2) / 2
            self._fitted_text(middle, y1 + (y2 - y1) * 0.36, name, box_width - 6,
                              size, name_colour, bold=True)
            self._fitted_text(middle, y1 + (y2 - y1) * 0.7, shown, box_width - 6,
                              size, TEXT_DIM)
            centre_x, centre_y = x2 - dot - 3, y1 + dot + 3
        self.create_oval(centre_x - dot, centre_y - dot, centre_x + dot, centre_y + dot,
                         fill="" if off else accent, outline=accent, width=1.5)

        lines = [name, f"{temp_text}C" if temp is not None else "no reading"]
        if data.get("status"):
            lines.append(data["status"])
        if state == "alarm":
            lines.append("IN ALARM")
        self._ac_lines[name] = lines
        self._target(shape, ("ac", name), (x1, y1, x2, y2))

    def _draw_legend(self, cell):
        mode = MODES[self.mode]
        stops = mode["stops"]
        width = self.winfo_width()
        x, y = self._map_left + 6, self._map_bottom + 18
        bar_width, bar_height = min(260, width * 0.22), 10
        if self.mode == "breakers":
            # One swatch per band, labelled with its level.
            bands = [("normal", "below", self.breaker_highlight_pct)]
            bands += [(band, f"{level:g} %+" + suffix, level) for band, level, suffix in (
                ("highlight", self.breaker_highlight_pct, ""),
                ("warning", self.breaker_warning_pct, " warning"),
                ("alarm", self.breaker_alarm_pct, " alarm")) if level is not None]
            keys = x
            for band, label, level in bands:
                if band == "normal":
                    label = f"below {level:g} %" if level is not None else "normal"
                self._rounded(keys, y - 2, keys + 14, y + 12, 3, fill=BREAKER_BANDS[band],
                              outline="")
                text = self.create_text(keys + 20, y + 5, anchor="w", text=label,
                                        fill=TEXT_DIM, font=("Helvetica", 9))
                keys = self.bbox(text)[2] + 14
            keys += 12
        else:
            low, high = stops[0][0], stops[-1][0]
            steps = 60
            for step in range(steps):
                value = low + (high - low) * step / (steps - 1)
                left = x + bar_width * step / steps
                self.create_rectangle(left, y, left + bar_width / steps + 1, y + bar_height,
                                      fill=scale_colour(value, stops), outline="")
            for value in mode["ticks"]:
                left = x + bar_width * (value - low) / (high - low)
                self.create_text(left, y - 8, text=mode["tick"].format(value), fill=TEXT_DIM,
                                 font=("Helvetica", 9))
            keys = x + bar_width + 26
        for fill, outline, dash, label in (
            (NO_SENSOR, NO_SENSOR_EDGE, None, mode["missing"]),
            (NO_SENSOR, "#8a6a2a", (3, 2), "no reading"),
            (CAGE, "", None, "unusable"),
            (BACKGROUND, STATE_COLOURS["standby"], (4, 3), "AC standby"),
            (BACKGROUND, STATE_COLOURS["off"], None, "AC off"),
        ):
            self._rounded(keys, y - 2, keys + 14, y + 12, 3, fill=fill, outline=outline,
                          dash=dash)
            text = self.create_text(keys + 20, y + 5, anchor="w", text=label,
                                    fill=TEXT_DIM, font=("Helvetica", 9))
            keys = self.bbox(text)[2] + 18
        # The markers for a PDU that is not reporting, and one in maintenance.
        for fill, mark, label in ((WARN_TEXT, "!", "PDU not reporting"),
                                  (MAINTENANCE, "M", "PDU in maintenance")):
            self.create_oval(keys, y - 1, keys + 13, y + 12, fill=fill, outline="")
            self.create_text(keys + 6.5, y + 5.5, text=mark, fill=BACKGROUND,
                             font=("Helvetica", 8, "bold"))
            text = self.create_text(keys + 20, y + 5, anchor="w", text=label,
                                    fill=TEXT_DIM, font=("Helvetica", 9))
            keys = self.bbox(text)[2] + 18

    # ------------------------------------------------------------- popup

    def _target(self, shape, key, box) -> None:
        self._targets[shape] = key
        self._anchors[key] = box

    def pin(self, key) -> None:
        """Open a rack's or AC unit's popup from outside the map, e.g. when
        an alarm is clicked: ("rack", 84) or ("ac", "AC-3")."""
        self.pinned = key
        self.redraw()

    def _pointer(self, event) -> None:
        """A hand over anything that can be clicked."""
        items = self.find_overlapping(event.x, event.y, event.x, event.y)
        clickable = any(item in self._targets or "popup_close" in self.gettags(item)
                        or "popup_button" in self.gettags(item) for item in items)
        self.configure(cursor="hand2" if clickable else "")

    def _click(self, event) -> None:
        """A rack or AC opens its popup; the "x" or anywhere else closes it;
        a click inside the popup leaves it open."""
        items = self.find_overlapping(event.x, event.y, event.x, event.y)
        tags = {tag for item in items for tag in self.gettags(item)}
        accept = next((tag[len("accept:"):] for tag in tags if tag.startswith("accept:")), None)
        if accept is not None:
            if self.on_accept is not None:
                self.on_accept(accept)
            return
        devices = next((tag[len("devices:"):] for tag in tags if tag.startswith("devices:")), None)
        if devices is not None:
            if self.on_devices is not None:
                self.on_devices(int(devices))
            return
        switch = next((tag[len("maint:"):] for tag in tags if tag.startswith("maint:")), None)
        if switch is not None:
            pdu_id, state = switch.split(":")
            if self.on_maintenance is not None:
                self.on_maintenance(int(pdu_id), state == "on")
            return
        if "popup_close" in tags:
            self.pinned = None
        elif "popup" in tags:
            return
        else:
            self.pinned = next((self._targets[item] for item in reversed(items)
                                if item in self._targets), None)
        self.delete("popup")
        if self.pinned is not None:
            self._draw_popup()

    def _breaker_band(self, percent) -> str:
        """normal, highlight, warning or alarm, by the configured levels."""
        for band, level in (("alarm", self.breaker_alarm_pct),
                            ("warning", self.breaker_warning_pct),
                            ("highlight", self.breaker_highlight_pct)):
            if level is not None and percent >= level:
                return band
        return "normal"

    def _circuit_colour(self, percent) -> str:
        if percent is None:
            return TEXT_DIM
        return {"alarm": ALARM_TEXT, "warning": WARN_TEXT,
                "highlight": HIGHLIGHT_TEXT}.get(self._breaker_band(percent), TEXT)

    def _rack_details(self, number) -> list:
        """Everything known about one rack, whatever the map is showing:
        three big numbers, where they come from, then each PDU with its
        circuits in one aligned table."""
        rows = [("title", f"Rack {number}")]
        for entry in self.pdu_health.get(number, []):
            rows.append(("info", entry.get("text") or f"{entry['pdu']}: not answering",
                         WARN_TEXT))
        # Circuit follower messages, each with its Accept button.
        for entry in self.follow_alarms.get(number, []):
            colour = ALARM_TEXT if entry["kind"] == "circuit_drop" else WARN_TEXT
            rows.append(("follow", entry["description"], colour, entry["key"]))
        temp = self.racks.get(number) or {}
        power = self.power.get(number) or {}
        breakers = self.breakers.get(number) or {}
        circuits = breakers.get("circuits") or []
        percents = [c["percent"] for c in circuits if c["percent"] is not None]
        top = max(percents) if percents else None

        temp_value = temp.get("value")
        rows.append(("stats", [
            ("Temperature", "--" if temp_value is None else f"{temp_value:.1f} \u00b0C",
             WARN_TEXT if temp.get("stale") else TEXT),
            ("Power", "--" if power.get("value") is None else f"{power['value']:.2f} kW",
             WARN_TEXT if power.get("stale") else TEXT),
            ("Highest circuit", "--" if top is None else f"{top:.0f} %",
             self._circuit_colour(top)),
        ]))
        if not temp:
            rows.append(("info", "No temperature sensor", TEXT_DIM))
        elif temp_value is None:
            rows.append(("info", "Temperature sensor reports nothing", TEXT_DIM))
        else:
            source = [temp.get("source"), f"read {temp['time']}" if temp.get("time") else None]
            rows.append(("info", "Temperature from " + ", ".join(i for i in source if i),
                         TEXT_DETAIL))
            if temp.get("low") is not None and temp.get("high") is not None:
                rows.append(("info", f"Low {temp['low']:.1f} \u00b0C, high "
                                     f"{temp['high']:.1f} \u00b0C", TEXT_DETAIL))
            if temp.get("stale"):
                rows.append(("info", "Temperature is stale: no recent reading", WARN_TEXT))

        # PDUs in name order, with power and circuits
        # together; then any PDU known only as not answering or in maintenance
        # (one that never answered has no readings), so each has its button.
        pdus = [entry["pdu"] for entry in power.get("pdus", [])]
        ids = {entry["pdu"]: entry.get("pdu_id") for entry in power.get("pdus", [])}
        for circuit in circuits:
            if circuit["pdu"] not in pdus:
                pdus.append(circuit["pdu"])
        extra = [(e["pdu"], e.get("pdu_id")) for e in self.pdu_health.get(number, [])]
        extra += [(e["name"], pdu_id) for pdu_id, e in self.maintenance.items()
                  if e.get("rack") == number]
        for name, pdu_id in extra:
            if name not in pdus:
                pdus.append(name)
            ids.setdefault(name, pdu_id)
        if pdus:
            rows.append(("rule",))
            if circuits:
                rows.append(("header", ["Circuit", "Amps", "Normal", "Limit", "Load"]))
            for name in pdus:
                record = next((e for e in power.get("pdus", []) if e["pdu"] == name), {})
                summary = []
                if record.get("kw") is not None:
                    summary.append(f"{record['kw']:.2f} kW")
                if record.get("inlet_current") is not None and record.get("inlet_limit"):
                    inlet = f"inlet {record['inlet_current']:.1f} / {record['inlet_limit']:g} A"
                    normal = self.normals.get(record.get("key"))
                    if normal is not None:
                        inlet += f", normal {normal:.1f}"
                    summary.append(inlet)
                if not record:
                    summary.append("no reading")
                pdu_id = ids.get(name)
                held = self.maintenance.get(pdu_id)
                rows.append(("pdu", name, "  \u00b7  ".join(summary), pdu_id, held))
                if held:
                    rows.append(("info", f"In maintenance since {_local(held['since_utc'])}: "
                                         "no alarms from this PDU", MAINTENANCE))
                for circuit in (c for c in circuits if c["pdu"] == name):
                    percent = circuit["percent"]
                    normal = self.normals.get(circuit.get("key"))
                    normal_text = "--" if normal is None else f"{normal:.1f} A"
                    if circuit.get("state") == "open":
                        limit = circuit["limit"]
                        rows.append(("cols", [circuit["label"], "--", normal_text,
                                              "--" if limit is None else f"{limit:g} A",
                                              "TRIPPED"],
                                     [ALARM_TEXT, TEXT_DIM, TEXT_DETAIL, TEXT_DETAIL,
                                      ALARM_TEXT]))
                        continue
                    if percent is None:
                        rows.append(("cols", [circuit["label"], "--", normal_text, "--",
                                              "no reading"],
                                     [TEXT, TEXT_DIM, TEXT_DETAIL, TEXT_DIM, TEXT_DIM]))
                        continue
                    rows.append(("cols", [
                        circuit["label"],
                        f"{circuit['current']:.1f} A",
                        normal_text,
                        f"{circuit['limit']:g} A" + ("*" if circuit["limit_from_config"] else ""),
                        f"{percent:.0f} %",
                    ], [TEXT, TEXT, TEXT_DETAIL, TEXT_DETAIL, self._circuit_colour(percent)]))
            rows.append(("rule",))
            by_source = {}
            for record in power.get("pdus", []):
                if record.get("time"):
                    by_source.setdefault(record.get("source", "PDU"), set()).add(record["time"])
            parts = [("from the PDUs, " if source == "PDU" else f"from {source}, ")
                     + ", ".join(sorted(times)) for source, times in sorted(by_source.items())]
            if parts:
                rows.append(("note", "PDU readings " + "; ".join(parts), TEXT_DETAIL))
            if circuits and not any(self.normals.get(c.get("key")) is not None for c in circuits):
                rows.append(("note", "Normal: needs 6 hours of history first", TEXT_DETAIL))
            if any(c["limit_from_config"] for c in circuits):
                rows.append(("note", "* limit from config: no rating given for it", TEXT_DETAIL))
            if power.get("stale") or breakers.get("stale"):
                rows.append(("note", "PDU readings are stale", WARN_TEXT))
        elif not temp:
            rows.append(("info", "No PDU data for this rack", TEXT_DIM))
        return rows

    def _draw_popup(self) -> None:
        """Draw the pinned popup next to its tile, kept inside the map."""
        key = self.pinned
        box = self._anchors.get(key)
        if box is None:              # the rack is no longer on the map
            self.pinned = None
            return
        if key[0] == "rack":
            rows = self._rack_details(key[1])
        else:
            lines = self._ac_lines.get(key[1], [key[1]])
            rows = [("title", lines[0])] + [("info", line, TEXT) for line in lines[1:]]

        pad, width = 18, POPUP_WIDTH
        inner = width - 2 * pad
        right_edges = (None, inner * 0.40, inner * 0.60, inner * 0.80, inner)  # Amps, Normal, Limit, Load
        y = pad
        devices_y = None        # where the Devices button goes: beside the table

        def text(x, y, value, colour, size, bold=False, anchor="w"):
            font = ("Helvetica", size, "bold") if bold else ("Helvetica", size)
            return self.create_text(x, y, anchor=anchor, text=value, fill=colour,
                                    font=font, tags="popup")

        for row in rows:
            kind = row[0]
            if kind == "title":
                text(pad, y + 10, row[1], TEXT, 16, bold=True)
                y += 34
            elif kind == "stats":
                tile = (inner - 16) / 3
                for index, (label, value, colour) in enumerate(row[1]):
                    x = pad + index * (tile + 8)
                    self.create_rectangle(x, y, x + tile, y + 58, fill="#1c1f26",
                                          outline="", tags="popup")
                    text(x + 10, y + 14, label, TEXT_DETAIL, 11)
                    text(x + 10, y + 38, value, colour, 18, bold=True)
                y += 68
                devices_y = y + 8
            elif kind == "info":
                text(pad, y + 9, row[1], row[2], 12)
                y += 24
            elif kind == "rule":
                y += 6
                self.create_line(pad, y, pad + inner, y, fill=POPUP_EDGE, tags="popup")
                y += 10
            elif kind == "header":
                devices_y = y
                text(pad + 10, y + 9, row[1][0], TEXT_DIM, 11)
                for index in (1, 2, 3, 4):
                    text(pad + right_edges[index], y + 9, row[1][index], TEXT_DIM, 11,
                         anchor="e")
                y += 24
            elif kind == "pdu":
                # Name, its Maintenance button, and the summary on the right
                # (on its own line when they would overlap).
                _kind, name, summary, pdu_id, held = row
                head = text(pad, y + 11, name, MAINTENANCE if held else TEXT, 13, bold=True)
                right = self.bbox(head)[2]
                if pdu_id is not None and self.on_maintenance is not None:
                    tags = ("popup", "popup_button", f"maint:{pdu_id}:{'off' if held else 'on'}")
                    label = self.create_text(0, 0, text="End maintenance" if held else "Maintenance",
                                             fill=TEXT, font=("Helvetica", 10, "bold"), tags=tags)
                    lx1, _ly1, lx2, _ly2 = self.bbox(label)
                    bx, bw = right + 10, lx2 - lx1 + 16
                    button = self.create_rectangle(bx, y + 1, bx + bw, y + 22, fill=BUTTON,
                                                   outline=MAINTENANCE if held else POPUP_EDGE,
                                                   tags=tags)
                    self.coords(label, bx + bw / 2, y + 11.5)
                    self.tag_raise(label, button)
                    right = bx + bw
                if summary:
                    item = text(pad + inner, y + 11, summary, TEXT_DETAIL, 12, anchor="e")
                    if self.bbox(item)[0] < right + 12:
                        self.delete(item)
                        y += 24
                        text(pad + inner, y + 11, summary, TEXT_DETAIL, 12, anchor="e")
                y += 28
            elif kind == "cols":
                cells, colours = row[1], row[2]
                text(pad + 10, y + 10, cells[0], colours[0], 14)
                for index in (1, 2, 3, 4):
                    text(pad + right_edges[index], y + 10, cells[index], colours[index],
                         14, bold=index == 4, anchor="e")
                y += 26
            elif kind == "note":
                text(pad, y + 8, row[1], row[2], 11)
                y += 21
            elif kind == "follow":
                # The message, wrapped beside an Accept button: one click
                # takes the circuit's value now as its normal.
                _kind, message, colour, circuit_key = row
                button_w = 92
                item = self.create_text(pad, y + 4, anchor="nw", text=message, fill=colour,
                                        font=("Helvetica", 12), width=inner - button_w - 10,
                                        tags="popup")
                _x1, _y1, _x2, bottom = self.bbox(item)
                tags = ("popup", "popup_button", f"accept:{circuit_key}")
                bx = pad + inner - button_w
                self.create_rectangle(bx, y + 2, bx + button_w, y + 28, fill="#2c313a",
                                      outline=POPUP_EDGE, tags=tags)
                self.create_text(bx + button_w / 2, y + 15, text="Accept", fill=TEXT,
                                 font=("Helvetica", 11, "bold"), tags=tags)
                y = max(bottom, y + 28) + 8
        # The Devices button, right of the circuit table: the rack's devices
        # and their PDU outlets from the cabinet list, in their own window.
        count = self.device_counts.get(key[1]) if key[0] == "rack" else None
        if count is not None and self.on_devices is not None and devices_y is not None:
            tags = ("popup", "popup_button", f"devices:{key[1]}")
            bx, bw = pad + inner + 24, 132
            self.create_rectangle(bx, devices_y, bx + bw, devices_y + 52, fill=BUTTON,
                                  outline=POPUP_EDGE, tags=tags)
            self.create_text(bx + bw / 2, devices_y + 17, text="Devices", fill=TEXT,
                             font=("Helvetica", 12, "bold"), tags=tags)
            self.create_text(bx + bw / 2, devices_y + 36,
                             text=f"{count} in the list" if count else "empty in the list",
                             fill=TEXT_DETAIL, font=("Helvetica", 10), tags=tags)

        # Width follows the content: a long PDU name and its summary can
        # need more than the table.
        x1, _y1, x2, _y2 = self.bbox("popup")
        popup_w = max(width, x2 + pad)
        popup_h = y + pad - 6

        # The close button, top right.
        cross = self.create_text(popup_w - pad - 6, pad + 10, text="\u2715", fill=TEXT_DETAIL,
                                 font=("Helvetica", 14, "bold"), tags=("popup", "popup_close"))
        cx1, cy1, cx2, cy2 = self.bbox(cross)
        hit = self.create_rectangle(cx1 - 8, cy1 - 5, cx2 + 8, cy2 + 5, fill=POPUP_BG,
                                    outline=POPUP_EDGE, tags=("popup", "popup_close"))
        back = self.create_rectangle(0, 0, popup_w, popup_h, fill=POPUP_BG,
                                     outline=POPUP_EDGE, width=1.5, tags="popup")
        self.tag_lower(back, "popup")
        self.tag_raise(hit)
        self.tag_raise(cross)

        # Beside the tile: right of it if there is room, else left; kept inside.
        tx1, ty1, tx2, _ty2 = box
        canvas_w, canvas_h = self.winfo_width(), self.winfo_height()
        left = tx2 + 10 if tx2 + 10 + popup_w <= canvas_w - 6 else tx1 - 10 - popup_w
        left = max(6, min(left, canvas_w - popup_w - 6))
        top = max(6, min(ty1 - 10, canvas_h - popup_h - 6))
        self.move("popup", left, top)
