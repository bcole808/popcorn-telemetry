# Popcorn telemetry

Records humidity and temperature inside the popcorn machine while it runs, so
runs can be replayed and debugged afterwards. An SHT45 on an ESP32 streams
readings over USB serial; a terminal CLI on the Mac draws a live graph and
writes each session to a labelled CSV in `data/`.

## Hardware

ESP-WROOM-32 38-pin dev board + SHT45 breakout, wired over I2C:

| SHT45 breakout | ESP32 38-pin |
| -------------- | ------------ |
| VIN / VCC      | **3V3** (not VIN/5V — the sensor is a 3.3V part) |
| GND            | GND |
| SDA            | **GPIO21** |
| SCL            | **GPIO22** |

The SHT45 lives at I2C address `0x44`. If your breakout has no pullup resistors
near SDA/SCL, add 4.7kΩ from each to 3V3.

**Placement:** mount the sensor in the chamber air, not touching the kettle.
Direct radiant heat skews the temperature reading, and the breakout PCB and
cable are not rated for kettle-surface temperatures even though the sensor die is.

## Flashing the firmware

PlatformIO lives at `~/.platformio/penv/bin`, which isn't on `PATH` by default:

```bash
export PATH="$HOME/.platformio/penv/bin:$PATH"   # worth adding to ~/.zshrc
cd firmware
pio run -t upload
pio device monitor        # sanity check — see below
```

If upload fails to sync, hold the **BOOT** button while it prints `Connecting…`.

The monitor should show the I2C scan, then a 2 Hz stream:

```
SCAN,0x44
READY
D,14500,31.42,58.77
```

(That's `D,<millis>,<temp_C>,<humidity>` — the firmware speaks Celsius on the
wire; `record.py` converts to Fahrenheit for the display and the CSV.)

No `SCAN,0x44` means the sensor isn't answering — recheck wiring and pullups
before anything else. Breathe on the sensor and humidity should jump 10–30
points within a second or two, then decay; that confirms it's reading the real
world rather than returning a stuck value.

## Recording a session

```bash
uv run record.py              # autodetects the serial port
uv run record.py --port /dev/cu.usbserial-0001
uv run record.py --simulate   # fake data, no hardware needed
```

Menu: **Enter** starts a new session, **q** quits. That brings up the welcome
screen: a box holding the title in block letters with a tag line drawn at random
from the 50 in [phrases.py](phrases.py), and below the box the marker keys with
their labels. It stays on screen while you type the session name (blank =
timestamp only) and while you wait to press Enter to begin.

Under the tag line sit two small grey sparklines — one for humidity, one for
temperature — showing the current reading and its recent history. They repaint
about once a second while you answer the prompts, so you can see the sensor is
alive and the room has settled before starting a run. Their scale never shrinks
below a couple of points (`HUMIDITY_MIN_SPAN` / `TEMP_MIN_SPAN` on
`LiveReadings`), so ordinary jitter reads as a flat line rather than a sawtooth.

The title comes from `APP_NAME` in [config.py](config.py). A multi-word name
wraps onto several lines of block letters when it's too wide to fit on one, and
drops to plain text only if a single word still won't fit. On a short terminal
the screen sheds its padding, and then its block letters, so the prompts below
always stay on screen.

While recording:

- **Enter** — stop
- **number keys** — drop a marker at this instant; that marker's hint turns
  bright yellow and stays lit, so the hint line always shows what you marked
  last, and a yellow vertical rule labelled with the bare number appears on the
  graph at that moment (it scrolls away with the rolling window)
- **-** / **=** — widen / narrow the visible time window by 30s (minimum 30s)
- **Ctrl-C** — stop; the CSV is still finalized

The graph's x-axis grows with the session — 30 seconds in, it spans 30 seconds —
until it reaches the window size (`PLOT_WINDOW_SECONDS`, 120 by default), after
which it becomes a rolling window of that length.

`-` and `=` adjust that window live, and the current value is shown in the hint
line as `window=120s`. The adjustment is view-only and session-local: it is not
written to `config.py`, and every new recording starts back at
`PLOT_WINDOW_SECONDS`. The CSV always records the whole session regardless of
what's on screen.

Stopping clears the plot and draws a boxed summary panel — session name,
duration, average temperature, the session's **max and average humidity** in
large block type, the best and worst average humidity across today's runs, a
random popcorn sign-off, and the file path. The panel is drawn from the top of a
cleared screen so it always stays visible above the menu.

The best/worst line is computed by reading every `data/` CSV whose filename
carries today's date, this session included. "Best" is the lowest average
humidity and "worst" the highest. It only appears once there are two or more
runs on the day — with a single run it would just restate the panel above it.

On a narrow or short terminal the panel falls back to compact type and shrinks to
fit rather than breaking its border.

The sign-off lines live in [phrases.py](phrases.py) — add or edit freely, keeping
them under 160 characters so they wrap to at most two lines.

## Markers

Edit `MARKERS` in [config.py](config.py) to change or extend the list — it drives
both the keypress handling and the on-screen legend, so adding a line is the only
change needed:

```python
MARKERS = {
    "1": "popping_started",
    "2": "popping_max_velocity",
    "3": "heat_off",
    "4": "stir_off",
    "5": "dump_kettle",
    "6": "other",
}
```

## Output

`data/` (gitignored) holds one folder per day. Running with `--simulate` writes
to `data-simulated/` instead — same layout, day folders and background trace and
all — so fake runs never mix in with real ones. Both are gitignored. Each session writes a pair of
files there, sharing one name, alongside that day's background trace:

```
data/2026-08-21/
  2026-08-21__background-data.csv           the always-on slow trace
  2026-08-21__4-45pm__my-session-name.csv   the session's readings
  2026-08-21__4-45pm__my-session-name.json  everything else about the run
```

### The background trace

One file per day, recording continuously for as long as the script is running —
at the menu, while you answer the setup prompts, throughout a session, and after
it ends. It samples once every `BACKGROUND_INTERVAL_SECONDS` (10 by default, in
[config.py](config.py)), which is what makes it useful for seeing what the room
did *after* a pop session finished.

Columns are `timestamp, humidity, temperature_f, label` — no `seconds_elapsed`,
since it isn't tied to any one run.

Samples land on wall-clock multiples of the interval, so every background
timestamp coincides exactly with one of the per-second rows of whatever session
was recording at the time, and the two files can be joined on `timestamp`.

The day's file is appended to, so stopping and restarting the script continues
the same trace rather than starting a new one, and a run that crosses midnight
rolls onto the next day's file by itself.

The `label` column marks session boundaries:

```
timestamp,humidity,temperature_f,label
2026-08-21T16:45:03,48.35,76.08,recording_started: 2026-08-21__4-45pm__my-session-name.csv
2026-08-21T16:52:18,56.70,77.74,recording_ended: 2026-08-21__4-45pm__my-session-name.csv
```

Those two rows are stamped at the exact second the session started and stopped
rather than waiting for the next scheduled slot, so they line up with the
session's own first and last rows. Every other row falls on the interval.

### Setup questions

After the session name you're asked for the room's ambient humidity, the room's
ambient temperature in °F, and the fan speed as a percentage. All three are
optional — press Enter to skip any of them. A trailing unit is tolerated (`56%`,
`75F`, `75°F`); anything that isn't a number, or a percentage outside 0–100,
re-asks rather than being silently dropped.

### The JSON sidecar

Holds the answers to those questions plus what the run produced. Skipped answers
are recorded as `null` rather than being left out, so the shape is the same for
every run and a skipped field is distinguishable from a missing one.

```json
{
  "session_name": "kettle six",
  "csv": "2026-08-22__3-20pm__kettle-six.csv",
  "started_at": "2026-08-22T15:20:28",
  "ambient_humidity_rh": 56,
  "ambient_temperature_f": 75,
  "fan_speed_pct": 40,
  "ended_at": "2026-08-22T15:20:33",
  "duration_seconds": 5,
  "rows": 5,
  "humidity_max": 47.47,
  "humidity_avg": 45.97,
  "temperature_f_avg": 75.8
}
```

It's written once when recording starts, so a run that crashes still leaves its
setup behind, then rewritten with the results when the session ends.

### CSV columns

| column | example | notes |
| ------ | ------- | ----- |
| `timestamp` | `2026-08-21T16:45:03` | local wall-clock |
| `seconds_elapsed` | `0`, `1`, `2`, … | integer seconds since recording started |
| `humidity` | `58.77` | %RH |
| `temperature_f` | `88.56` | °F — the sensor reports Celsius, converted on the way in |
| `label` | `popping_started` | blank unless a marker was pressed since the last row |

The sensor samples at 2 Hz but the CSV is capped at one row per second: the row
for second *N* carries the first reading taken at or after that second. Rows are
flushed to disk as they're written, so pulling the plug mid-run costs at most one
second of data.

If two markers are pressed within the same second, the first lands on that
second's row and the second on the next — every press is preserved and no row
ever carries two labels. Markers pressed after the final data row are appended as
label-only rows with empty readings.

If the sensor drops out for a moment, those seconds are simply absent from the
CSV (visible as a gap in `seconds_elapsed`) rather than being padded with a
fabricated value.

## Known limitation

Inside a running machine, humidity may peg near 100% and condense on the sensor,
which causes slow recovery and drift. The SHT45 has an onboard heater that clears
this; the firmware currently leaves it off, since it self-heats and biases the
temperature channel. If real runs show stuck-at-100% behaviour, adding a periodic
heater pulse between samples is a small change in
[firmware/src/main.cpp](firmware/src/main.cpp).
