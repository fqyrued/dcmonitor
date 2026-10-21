# dcmonitor
DCIM

Desktop monitoring panel for a datacenter NOC: AC units, UPSes, generators
and rack PDUs, read only (HTTP, SNMP GET/GETNEXT, Modbus function 3), with
alarms and sound, a year of history in SQLite, a floor map and a rack view
fed by an Excel cabinet list. Python 3.11+ standard library only (tkinter).

## Try the demo

No devices needed: the demo runs the real program against simulated AC
units, UPSes, a generator and PDUs, with a sample floor map and cabinet
list. It makes no network connections and keeps its database in a
temporary folder.

    python -m dcmonitor.demo

Run it from the repository folder. On the Floor map tab, click a rack, then
Devices.

## Real use

Copy `config.example.toml` to `config.toml` and fill in your devices; the
PDU list (`pdus.toml`) and floor layout (`site_map.toml`) follow the
formats in `demo/`. Then `python main.py`.
