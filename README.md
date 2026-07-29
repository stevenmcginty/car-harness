# car-harness

A modular car-diagnostics desktop app + Android companion app.

- **Generic OBD-II on all cars (Torque-style).** Read the VIN, fault codes
  (stored / pending / permanent, with plain-English descriptions), clear codes,
  and stream live data (RPM, speed, coolant, and more) over any ELM327-style
  adapter: USB/serial, Classic Bluetooth, Bluetooth LE (e.g. Veepeak OBDCheck
  BLE) or WiFi.
- A **real, read-only BMW ENET/DoIP connection probe**: it genuinely talks to
  a BMW (F/G-series) over an ENET cable using DoIP (ISO 13400) and honestly
  reports how far it got. When no car is present it says so -- it never fakes
  a "connected" state.
- An **Android app** (`android/`, prebuilt `CarHarness.apk`) that scans cars
  directly from the phone with the same adapters, and **pairs with the
  desktop** over WiFi to drive the desktop's cables for the bigger jobs.
- **Service resets** (the 🔧 Service tab / desktop *Service resets* panel): a
  per-manufacturer catalogue of the after-servicing jobs a dealer does daily --
  oil/inspection counters, brake reminders, battery relearn. Each item is
  either a UDS reset the app sends over the OBD dongle or the official dash
  procedure to follow on the car, with a confidence grade and its source.
  Adding a manufacturer adds its resets. See
  `docs/private/service-resets-research.md` for the provenance.

It is built to grow: manufacturer **profiles** (BMW, Mazda, Peugeot/PSA,
VW/Audi, Mercedes, Fiat/Alfa and Jaguar/Land Rover today; the next brand is a
folder drop-in) and installable/removable **job
modules** are both discovered from the filesystem. A profile folder's
capability files become its API routes and its desktop panel automatically --
the manufacturer dropdown at the top of the desktop app drives everything.

## What it does

- **Read-only probe.** The probe sends a DoIP *Vehicle Identification Request*
  and reads back the car's *Vehicle Announcement* (VIN, logical address, EID,
  GID). That is all. There is **no** routing activation, **no** UDS diagnostic
  session, **no** TCP diagnostic connection -- so the probe **cannot change
  anything on the car**.
- **Honest status.** In real mode the connection status reflects only the actual
  probe result. A separate **sim** mode returns clearly-labelled fake data (VIN
  `SIMULATED000000BMW`) for demos; simulated data never appears in real mode.
- **Pluggable.** Manufacturer profiles live under `backend/profiles/<id>/` and
  job modules under each profile's `modules/` folder (built-in) or in
  `modules_store/` (user-imported, and removable to reclaim disk).

## How to run

**Easiest (Windows):** double-click **`start.bat`**. It creates a `.venv`,
installs the dependencies, opens your browser at http://127.0.0.1:8765 and
starts the server.

**From a terminal:**

```bat
cd C:\Users\steve\Desktop\car-harness
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python run.py
```

Then open http://127.0.0.1:8765. The server listens on all interfaces so the
phone app can reach it over the LAN -- but every non-localhost request is
rejected unless the phone has paired (see below), so the desktop UI at
127.0.0.1 stays the only unauthenticated door.

**Install it as an app.** The web UI is a PWA: open it at 127.0.0.1:8765 and
use Chrome/Edge's install button (address-bar icon, or menu -> Install) to get
Car Harness in its own window with its own icon. It keeps a copy of the shell,
so it still opens if the backend isn't running -- but the cache is deliberately
network-*first* and `/api/*` is never cached, because a diagnostic served from
cache would be a lie about the car in front of you.

Browsers only allow service workers on a secure origin, and `127.0.0.1` counts
as one. Reaching the same page from a tablet over plain `http://192.168.x.x`
does not, so it will run fine there but won't offer to install until it's put
behind HTTPS.

## Generic OBD-II scanning (all cars)

1. Add your adapter once via **Add adapter...** (ELM327 USB / Bluetooth /
   Bluetooth LE / WiFi) and select it in the header.
2. Ignition ON (engine can stay off). BLE dongles like the Veepeak answer by
   themselves, but the *car* only answers with the ignition on.
3. **Read fault codes** -- VIN, battery voltage, protocol, MIL state and the
   codes with descriptions. **Clear codes** asks for confirmation first
   (clearing also resets MOT readiness monitors).
4. Flip **Live data - Stream** for a Torque-style dashboard (RPM, speed,
   coolant, load, throttle, MAF, oil temp...). Only PIDs the car reports as
   supported are shown.

The **Simulate** toggle returns clearly-labelled fake results (VIN
`SIMULATED0CAR0001`); simulated data never appears in real mode.

## Phone app

`CarHarness.apk` in the project root (source in `android/`, rebuild with
`gradle assembleDebug`). Install it on the phone, then:

- **Home tab** -- the whole job off one button. **Scan this car** connects to
  the remembered dongle, reads the VIN, picks the manufacturer profile from
  that VIN, reads the generic fault codes and then walks every module the
  brand supports -- reporting each leg as it goes, and naming the wall it hit
  when a leg can't run. The result lands as three tiles (faults / modules /
  resets) that tap through to the tabs below. Transport setup (Demo / BLE /
  Classic Bluetooth / WiFi) lives collapsed underneath, because after the
  first successful link the app remembers the dongle and never asks again.
- **Faults tab** -- read + clear codes with the same descriptions database.
- **Modules tab** -- BMW F-series deep scan straight from the phone: walks
  every control unit (engine, gearbox, ABS, airbags, cluster, head unit...)
  over the same ELM327 dongle using BMW's 6F1 extended addressing, and shows
  each module's part number, software/hardware version and its own fault
  memory -- with per-module clear. Read-only otherwise; no coding/flashing.
  Every scan (BMW / Mazda / Peugeot / VW-Audi) now identifies the car up front: the
  VIN is read from the modules themselves (UDS DID F190, falling back to
  the emissions-mandated Mode 09 read) and cross-checked between modules --
  a mismatch means a control unit swapped in from another car. An engine
  ECU that ignores UDS is re-probed on the standard OBD channel with the
  Mode 01 request it must answer whenever the ignition is on.
  Peugeot/PSA scans speak both UDS and PSA's KWP dialect per module
  (session `10 C0`, faults `17 FF 00`, ident zone `21 80`) and read the
  Mode 09 VIN *first*, so the car is identified even when a module refuses
  everything else. Body modules (BSI, airbag, cluster...) live on the diag
  CAN at OBD pins 3/8 -- a standard dongle only reaches the engine bus, so
  the report says which modules need a PSA pin-swap adapter instead of
  listing them as absent. Each scanned module carries a request->reply
  "diagnostic trail" for field debugging.
- **Live tab** -- the live dashboard on its own screen: big mono values with a
  trend line under each.
- **Desktop pairing** (the monitor icon in the Home header, not a bottom tab --
  it's a once-a-year errand) -- pair with this app: on the PC click
  **Pair phone...**, then on the phone **Find my PC** (UDP discovery on port
  8766) and type the 6-digit code. Once paired, the phone can run scans
  *through the desktop's adapters* -- the path to the deeper, proprietary-cable
  jobs. Un-pair either side at any time (desktop: Pair phone... -> Un-pair).

Pairing security: codes are one-time and expire after 5 minutes; the phone
gets a bearer token whose SHA-256 (never the token itself) is stored in
`paired_devices.json`.

## Diagnosing a BMW over ENET

1. Plug the ENET cable into the car's OBD-II port and the laptop's Ethernet
   port. Turn the ignition on.
2. BMW ENET provides no DHCP, so the laptop's Ethernet adapter falls back to a
   link-local address in `169.254.x.x`. That address is how the app detects the
   ENET adapter. Give Windows a few seconds to assign it.
3. Open the app and check the connection page.

## Windows Firewall note

The first time the probe sends its UDP broadcast, **Windows Firewall may pop up
asking whether to allow Python**. You must click **Allow** (private networks is
enough) -- otherwise the outgoing discovery datagram is blocked and the probe
can never reach the car. If you dismissed the prompt earlier, allow
`python.exe` for private networks in Windows Defender Firewall settings.

## API

Base URL: `http://127.0.0.1:8765`

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/profiles` | Manufacturer profiles: metadata, `features` (what each brand can do) and the `panel` text the UI renders |
| GET | `/api/status?profile=bmw&mode=real` | Connection status (`mode=sim` for a labelled fake) |
| GET | `/api/interfaces` | Network adapters and which are ENET candidates |
| GET | `/api/modules` | All job modules |
| POST | `/api/modules/import` | Import a module (.zip upload or JSON `{"path": ...}`) |
| DELETE | `/api/modules/{id}` | Remove a non-built-in module and reclaim its bytes |
| GET | `/api/adapter-types` | Built-in adapter kinds and their config fields |
| GET/POST | `/api/adapters` | List / test-and-save configured adapter instances |
| POST | `/api/adapters/test` | Test an adapter config without saving |
| GET | `/api/serial-ports` | Available COM ports |
| GET | `/api/obd/scan?adapter=<id>&mode=real` | VIN + fault codes over an ELM327 adapter (`mode=sim` for a labelled fake) |
| POST | `/api/obd/clear` | Clear codes -- JSON `{"adapter", "mode", "confirm": true}` |
| GET | `/api/obd/live?adapter=<id>&pids=rpm,speed` | Live data readings (default dashboard set if `pids` omitted) |
| GET | `/api/obd/pids?adapter=<id>` | Which live-data keys the car supports |
| GET | `/api/live/catalog?adapter=<id>&brand=<id>` | **Every value this car will give** -- standard OBD-II (asked of the car itself) plus the manufacturer overlay |
| GET | `/api/live/read?adapter=<id>&keys=obd.rpm,obd.coolant` | Read the selected channels once; this is what the dashboard polls |
| GET | `/api/obd/freeze?adapter=<id>` | Freeze frame -- the live data as it was when a fault stored (mode 02) |
| GET | `/api/{brand}/scan` | Walk the brand's modules, read each one's fault memory (generic: any profile folder) |
| POST | `/api/{brand}/clear` | Clear one module (or all) -- confirm-gated |
| GET | `/api/{brand}/live` | Brand-specific live values (Mazda extras, Peugeot health check, VW/Audi extras) |
| GET | `/api/{brand}/snapshot` | Passive broadcast-CAN capture (Mazda) |
| GET | `/api/{brand}/discover` | Find the car on a native cable (BMW ENET) |
| GET | `/api/{brand}/resets` | The brand's service-reset catalogue |
| POST | `/api/{brand}/reset` | Run one service reset -- confirm-gated |
| POST | `/api/pair/start` | (localhost only) new 6-digit phone-pairing code |
| POST | `/api/pair/claim` | Phone exchanges the code for a bearer token |
| GET | `/api/pair/devices` | Paired phones |
| DELETE | `/api/pair/devices/{id}` | Un-pair a phone |

The `/api/{brand}/…` routes are **generic**: they dispatch to whatever
capability files exist under `backend/profiles/<brand>/` (`scan.py`,
`pids.py`, `snapshot.py`, `resets.py`). The desktop UI's manufacturer panel is
built the same way -- from the profile's own `panel` metadata -- so adding a
manufacturer is a folder drop-in, with no route or UI code to write. The
Android app mirrors this with a single brand registry
(`android/.../obd/Brands.kt`).

### Live data, and what a new manufacturer inherits

`/live.html` is the live-data dashboard: it asks the car what it supports,
merges the manufacturer overlay on top, and streams the values you pick, with
sparklines, min/max hold and CSV export. Two layers feed it:

1. **Standard OBD-II (mode 01)** -- `backend/obd/pids.py`, ~120 readings across
   92 PIDs, the full J1979 set. This works on **every** car the dongle can talk
   to, so every manufacturer has live data whether or not anyone has researched
   it. The car's own support bitmap decides what is listed; nothing is assumed
   from the badge.
2. **Manufacturer extras** -- `backend/profiles/<brand>/pids.py`.

**A new brand gets live data for free.** Ship a `pids.py` with

```python
def live(adapter, mode) -> {"ok": bool, "readings": [{"name", "value", "unit"}], "message": str}
```

and it appears in the dashboard automatically -- no edits to the engine, the
routes or the front end. All eight current brands meet this. A brand may also
implement the faster optional interface (`CHANNELS` + `read_channels`), which
lets the dashboard read only the values on screen instead of the module's whole
DID table; over Bluetooth that is the difference between a one-second gauge and
a thirty-second one. See the contract in `backend/obd/livefeed.py`.

A brand with no `pids.py` is not hidden -- the dashboard says so in as many
words, so it is clear whether a missing value is a limit of the car or a gap in
our data.

## Layout

```
backend/
  main.py              FastAPI app (API under /api, serves frontend/ at /);
                       brand routes are generic /api/{brand}/… dispatch
  core/
    models.py          API response models
    interfaces.py      network-adapter detection (psutil)
    doip.py            the real, read-only DoIP probe (ISO 13400)
    hsfz.py            BMW F-series HSFZ transport (UDS over ENET)
    registry.py        discovers profiles + modules from disk
    brands.py          discovers each profile's capabilities (scan/live/…)
    pairing.py         phone pairing: codes, tokens, UDP discovery
  obd/                 generic OBD-II engine (ELM327 driver, transports, DTCs)
  adapters/            adapter types (ELM327 serial/BT/BLE/WiFi, ENET, …)
  profiles/            one folder per manufacturer -- the drop-in unit
    <brand>/
      profile.json     metadata + the "panel" text the desktop UI renders
      profile.py       status(mode) connection probe
      scan.py          module scan + clear            -> /api/<brand>/scan
      pids.py          brand live values (optional)   -> /api/<brand>/live
      snapshot.py      broadcast capture (optional)   -> /api/<brand>/snapshot
      resets.py        service resets (optional)      -> /api/<brand>/resets
      modules/         built-in job modules
  tools/               self-tests; run them all:
                       .venv\Scripts\python -m backend.tools.run_all
modules_store/         user-imported modules live here (removable)
frontend/              static single-page UI (served at /, owned by the UI app)
android/               Compose companion app (brand registry: obd/Brands.kt)
requirements.txt
run.py                 python run.py
start.bat              double-click launcher (Windows)
```

## Data sources

The offline fault-code table (`backend/obd/dtc_db.json`, mirrored in the
Android assets) covers generic SAE J2012 codes only — manufacturer-specific
codes mean different things per make, so the app refuses to guess at them
offline and offers the in-app AI lookup instead. Part of the generic P0 block
comes from [mytrile/obd-trouble-codes](https://github.com/mytrile/obd-trouble-codes)
(MIT license).

The Porsche Taycan live values (`backend/profiles/porsche/taycan.py`) are a
curated subset of the [OBDb Porsche-Taycan](https://github.com/OBDb/Porsche-Taycan)
signal database (CC-BY-SA-4.0), bundled unmodified as
`backend/profiles/porsche/taycan_obdb_signalset.json` — see the NOTICE beside it.
That data file (and any adaptation of it) stays under CC-BY-SA-4.0; the licence
does not extend to the car-harness code that reads it.

> The `frontend/` folder is owned by a separate front-end app. The backend just
> serves whatever static files land there; if it is empty the API still works.
