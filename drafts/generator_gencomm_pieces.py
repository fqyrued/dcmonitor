"""Draft, NOT deployed: generator mode, engine, load and alarm flags from
GenComm pages 3, 4 and 6 (see CLAUDE.md 6.4). Held back on 1 Oct 2026 until
the running values are compared with Scada Suite at a mains failure or the
monthly generator maintenance run. Tested against a simulated DSE8610.

To bring it in: drafts/generator_gencomm.py replaces collectors/generator.py;
the pieces below go back into alarms.py (AlarmEngine method plus
ALARM_KIND_NOTICE_GENERATOR = "generator_notice" in NOTICE_KINDS and
GENERATOR_ON_LOAD_A = 1.0), panel.py (generator_content, called with the
unit's states; "generator_notice" in ALARM_KIND_LABELS) and main.py.
"""

# ---- alarms.py, AlarmEngine
    def evaluate_generator_status(self, device: str, metrics: dict, states: dict) -> None:
        """The controller's own alarm flags, its control mode, and whether the
        engine runs and feeds the load (GenComm page 3 and page 4).

        Shutdown, electrical trip, controlled shutdown and control unit failure
        are real alarms. A warning, a mode other than Auto (the set will not
        start by itself on a mains failure), running and on load are notices.
        Nothing is changed while the controller gave no status this poll.
        """
        flags = {
            "flag_shutdown": ("gen_shutdown", "Shutdown alarm active on the controller"),
            "flag_electrical_trip": ("gen_electrical_trip",
                                     "Electrical trip active on the controller"),
            "flag_controlled_shutdown": ("gen_controlled_shutdown",
                                         "Controlled shutdown alarm active on the controller"),
            "flag_control_unit_failure": ("gen_unit_failure", "Controller reports a failure"),
        }
        if "control_mode_code" in states:
            for flag, (key, text) in flags.items():
                if states.get(flag):
                    self._raise(device, "generator", key, text)
                else:
                    self._clear(device, key, text + ": cleared")
            if states.get("flag_warning"):
                self._raise(device, ALARM_KIND_NOTICE_GENERATOR, "gen_warning",
                            "Warning alarm active on the controller")
            else:
                self._clear(device, "gen_warning", "Controller warning cleared")

            mode = states.get("control_mode_code")
            if mode not in (1, 0xFFFF):
                self._raise(device, ALARM_KIND_NOTICE_GENERATOR, "gen_not_auto",
                            f"Not in Auto: {states.get('control_mode')} mode")
            else:
                self._clear(device, "gen_not_auto", "Back in Auto")

        speed = metrics.get("engine_speed_rpm")
        if speed is not None:
            currents = [metrics.get(f"generator_current_a_l{phase}") or 0 for phase in (1, 2, 3)]
            on_load = speed > 0 and max(currents) >= GENERATOR_ON_LOAD_A
            if speed > 0:
                self._raise(device, ALARM_KIND_NOTICE_GENERATOR, "gen_running",
                            f"Engine running, {speed:g} rpm")
            else:
                self._clear(device, "gen_running", "Engine stopped")
            if on_load:
                kw = metrics.get("generator_kw")
                pct = metrics.get("generator_load_pct")
                load = f", {kw:.0f} kW" if kw is not None else ""
                load += f" ({pct:.0f} %)" if pct is not None else ""
                self._raise(device, ALARM_KIND_NOTICE_GENERATOR, "gen_on_load",
                            f"Running on load{load}, up to {max(currents):.0f} A")
            else:
                self._clear(device, "gen_on_load", "Off load")


# ---- panel.py
def generator_content(unit, values: dict, states: dict, alarms: list, age) -> dict:
    """What a generator card shows: fuel as the headline, with a meter, the
    control mode next to the name, then engine, load (kW and % of the set's
    rated power, from the controller), coolant and battery."""
    fuel = values.get("fuel_level_pct")
    battery = values.get("battery_v")
    speed = values.get("engine_speed_rpm")
    if speed is None:
        engine = "engine  --"
    elif speed == 0:
        engine = "engine  stopped"
    else:
        engine = f"engine  running {speed:g} rpm"
    currents = [values.get(f"generator_current_a_l{phase}") for phase in (1, 2, 3)]
    known = [value for value in currents if value is not None]
    kw = values.get("generator_kw")
    if speed is None or not known:
        load = "load  --"
    elif speed == 0:
        load = "load  none"
    elif max(known) < 1.0:
        load = "load  off load"
    else:
        parts = []
        if kw is not None:
            parts.append(f"{kw:.0f} kW")
        if values.get("generator_load_pct") is not None:
            parts.append(f"{values['generator_load_pct']:.0f} %")
        load = "load  " + (" \u00b7 ".join(parts) or f"{max(known):.0f} A")
    coolant = values.get("coolant_temp_c")
    if coolant is not None:
        coolant_text = f"coolant  {coolant:g} \u00b0C"
    else:
        coolant_text = f"coolant  {states.get('coolant_note', '--')}"
    return {
        "title": unit.name,
        "state": state_of(alarms, age),
        "headline": f"{fuel:g}" if fuel is not None else "--",
        "unit_text": "% fuel",
        "status": states.get("control_mode", ""),
        "lines": [
            engine,
            load,
            coolant_text,
            f"battery  {battery:.1f} V" if battery is not None else "battery  --",
        ],
        "footer": footer_text(alarms, age),
        "meter": None if fuel is None else max(0.0, min(1.0, fuel / 100)),
    }



# ---- main.py, Monitor
    def _generator(self, unit) -> str:
        """Instruments, control mode and alarm flags (GenComm pages 4 and 3)."""
        data = generator.read(unit.ip, unit=unit.unit)
        values, states = data["metrics"], data["states"]
        self._store_values(unit.name, values)
        self._store_states(unit.name, states)
        self.alarms.evaluate_generator(unit.name, values)
        self.alarms.evaluate_generator_status(unit.name, values, states)
        speed = values.get("engine_speed_rpm")
        engine = "--" if speed is None else ("stopped" if speed == 0 else f"{speed:g} rpm")
        text = (f"fuel {values.get('fuel_level_pct', '--')} %, "
                f"battery {values.get('battery_v', '--')} V, "
                f"{states.get('control_mode', 'mode --')}, engine {engine}")
        if values.get("generator_kw"):
            text += f", {values['generator_kw']:.0f} kW"
        if states.get("status_error"):
            text += f", status read failed: {states['status_error']}"
        return text

