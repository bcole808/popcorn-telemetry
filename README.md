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

Menu: **Enter** starts a new session, **q** quits. You're asked for a session
name (blank = timestamp only), then Enter begins recording.

While recording:

- **space** — stop
- **1**–**6** — drop a marker at this instant; that marker's hint turns bright
  yellow and stays lit, so the hint line always shows what you marked last
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

Sessions land in `data/` (gitignored) as
`2026-08-21__4-45pm__my-session-name.csv`:

| column | example | notes |
| ------ | ------- | ----- |
| `seconds_elapsed` | `0`, `1`, `2`, … | integer seconds since recording started |
| `humidity` | `58.77` | %RH |
| `temperature_f` | `88.56` | °F — the sensor reports Celsius, converted on the way in |
| `label` | `popping_started` | blank unless a marker was pressed since the last row |
| `timestamp` | `2026-08-21T16:45:03` | local wall-clock |

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
