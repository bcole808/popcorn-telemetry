#!/usr/bin/env python3
"""Interactive recorder for SHT45 humidity data from the popcorn machine.

Run with:  uv run record.py            (reads the ESP32 over USB serial)
           uv run record.py --simulate (fake data, no hardware needed)
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import random
import re
import select
import shutil
import sys
import termios
import textwrap
import time
import tty
from collections import deque
from contextlib import contextmanager
from datetime import datetime

import plotext as plt

import bigtext
import config
import phrases

ANSI_RE = re.compile(r"\033\[[0-9;]*m")

HEADER = ["timestamp", "seconds_elapsed", "humidity", "temperature_f", "label"]

# Serial devices that are never our board.
PORT_BLOCKLIST = ("Bluetooth-Incoming-Port", "debug-console")
PORT_HINTS = ("usbserial", "wchusbserial", "SLAB", "usbmodem")

# How far `-` / `=` move the visible time window, and how small it may get.
WINDOW_STEP = 30
WINDOW_MIN = 30

HIGHLIGHT = "\033[93m"  # bright yellow — marks the most recently pressed marker
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[96m"
GREY = "\033[90m"  # light grey — the subtle live readings on the welcome screen

BOX_MAX_WIDTH = 88


def c_to_f(celsius: float) -> float:
    """The sensor and firmware speak Celsius; everything above this line is °F."""
    return celsius * 9.0 / 5.0 + 32.0


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


class SerialSource:
    """Reads `D,<millis>,<temp>,<rh>` lines from the ESP32."""

    def __init__(self, port: str | None):
        import serial  # imported here so --simulate works without pyserial
        from serial.tools import list_ports

        if port is None:
            port = self._autodetect(list_ports)
        self.port = port
        print(f"Opening {port} at {config.BAUD} baud...")
        self.ser = serial.Serial(port, config.BAUD, timeout=0)
        self._buf = b""
        self._await_ready()

    @staticmethod
    def _autodetect(list_ports) -> str:
        devices = [
            p.device
            for p in list_ports.comports()
            if not any(b in p.device for b in PORT_BLOCKLIST)
        ]
        preferred = [d for d in devices if any(h in d for h in PORT_HINTS)]
        candidates = preferred or devices
        if not candidates:
            raise SystemExit(
                "No serial port found. Plug in the ESP32 and try again, "
                "or pass --port /dev/cu.something, or use --simulate for testing"
            )
        if len(candidates) > 1:
            print(f"Multiple ports found: {', '.join(candidates)}")
            print(f"Using {candidates[0]} — override with --port if that's wrong.")
        return candidates[0]

    def _await_ready(self, timeout: float = 10.0) -> None:
        """Opening the port resets the ESP32; wait for it to boot and report in."""
        print("Waiting for the board to boot...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            for line in self._read_lines():
                if line.startswith("SCAN,"):
                    print(f"  I2C device found at {line.split(',', 1)[1]}")
                elif line == "READY":
                    print("  Sensor ready.\n")
                    return
                elif line.startswith("ERR,"):
                    print(f"  Board reported: {line.split(',', 1)[1]}")
                elif line.startswith("D,"):
                    print("  Receiving data.\n")
                    return
            time.sleep(0.05)
        print(
            "  No READY within 10s — carrying on anyway. If nothing plots, check the\n"
            "  wiring (SDA=GPIO21, SCL=GPIO22) and that the firmware is flashed.\n"
        )

    def _read_lines(self) -> list[str]:
        waiting = self.ser.in_waiting
        if waiting:
            self._buf += self.ser.read(waiting)
        if b"\n" not in self._buf:
            return []
        *lines, self._buf = self._buf.split(b"\n")
        return [ln.decode("utf-8", "replace").strip() for ln in lines]

    def poll(self) -> tuple[list[tuple[float, float]], list[str]]:
        """Returns (readings as [(temp_c, humidity)], status messages)."""
        readings: list[tuple[float, float]] = []
        notes: list[str] = []
        for line in self._read_lines():
            if line.startswith("D,"):
                parts = line.split(",")
                if len(parts) == 4:
                    try:
                        readings.append((float(parts[2]), float(parts[3])))
                    except ValueError:
                        notes.append(f"unparseable: {line}")
            elif line.startswith("ERR,"):
                notes.append(line.split(",", 1)[1])
        return readings, notes

    def close(self) -> None:
        self.ser.close()


class SimulatedSource:
    """Plausible fake data at 2 Hz so the CLI can be exercised without hardware."""

    def __init__(self) -> None:
        self.t0 = time.time()
        self.next_at = self.t0
        print("Simulation mode — no hardware, generating fake readings.\n")

    def poll(self) -> tuple[list[tuple[float, float]], list[str]]:
        readings = []
        now = time.time()
        while self.next_at <= now:
            t = self.next_at - self.t0
            humidity = 42 + 18 * math.sin(t / 25) + random.gauss(0, 0.6)
            temp = 24 + t * 0.05 + random.gauss(0, 0.15)
            readings.append((temp, max(0.0, min(100.0, humidity))))
            self.next_at += 0.5
        return readings, []

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# Session files
# --------------------------------------------------------------------------


def ask_optional_number(
    prompt: str,
    low: float | None = None,
    high: float | None = None,
    on_idle=None,
    integer: bool = True,
) -> float | None:
    """Ask for a number the user may skip with a bare Enter.

    Tolerates a trailing unit ("56%", "75F", "75°F") and re-asks on anything
    that isn't a number or falls outside [low, high], so a typo can't silently
    become a missing or nonsense reading.
    """
    while True:
        try:
            raw = prompt_line(prompt, on_idle).strip()
        except EOFError:
            return None
        if not raw:
            return None
        cleaned = raw.rstrip("%").strip().rstrip("Ff").strip().rstrip("°").strip()
        try:
            value = float(cleaned)
        except ValueError:
            print("    Please enter a number, or press Enter to skip.")
            continue
        if (low is not None and value < low) or (high is not None and value > high):
            if high is None:
                print(f"    Please enter a number of at least {low:g}.")
            elif low is None:
                print(f"    Please enter a number of at most {high:g}.")
            else:
                print(f"    Please enter a number between {low:g} and {high:g}.")
            continue
        return round(value) if integer else value


def short(name: str, limit: int = 13) -> str:
    """Clip a session name so two of them fit on one line of the panel."""
    return name if len(name) <= limit else name[: limit - 1] + "…"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug


# Set from --simulate in main(). Everything below derives its paths from
# data_dir(), so flipping this moves the whole tree — day folders, the demo
# folder and the background trace included.
SIMULATED = False


def data_dir() -> str:
    name = config.DATA_DIR_SIMULATED if SIMULATED else config.DATA_DIR
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    os.makedirs(path, exist_ok=True)
    return path


def day_dir(when: datetime | None = None) -> str:
    """data/2026-08-22/ — one folder per day, created on demand."""
    when = when or datetime.now()
    path = os.path.join(data_dir(), f"{when:%Y-%m-%d}")
    os.makedirs(path, exist_ok=True)
    return path


BACKGROUND_SUFFIX = "__background-data.csv"


def background_path(when: datetime | None = None) -> str:
    when = when or datetime.now()
    return os.path.join(day_dir(when), f"{when:%Y-%m-%d}{BACKGROUND_SUFFIX}")


class BackgroundLog:
    """A slow, always-on trace of the room, running for as long as the CLI does.

    Samples land on wall-clock multiples of INTERVAL, so every background
    timestamp coincides exactly with one of the per-second rows in whatever
    session happens to be recording at the time. The day's file is appended to,
    so stopping and restarting the script continues the same trace, and a run
    that crosses midnight rolls onto the next day's file by itself.
    """

    HEADER = ["timestamp", "humidity", "temperature_f", "label"]

    def __init__(self, interval: int | None = None):
        self.interval = interval or config.BACKGROUND_INTERVAL_SECONDS
        self.latest: tuple[float, float] | None = None  # (temp_f, humidity)
        self.next_at: float | None = None
        self.pending: deque[str] = deque()

    def observe(self, readings) -> None:
        """Take the newest reading from whoever is polling the sensor."""
        if readings:
            temp_c, humidity = readings[-1]
            self.latest = (c_to_f(temp_c), humidity)

    def note(self, label: str) -> None:
        self.pending.append(label)

    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self.next_at is None:
            self.next_at = math.ceil(now / self.interval) * self.interval

        # Events are stamped at the second they happened rather than waiting for
        # the next slot, so a session's start and end line up with its own rows.
        while self.pending:
            self._write(int(now), self.pending.popleft())

        if now >= self.next_at:
            self._write(int(self.next_at), "")
            self.next_at += self.interval
            if self.next_at <= now:  # fell behind (a sleeping laptop); resync
                self.next_at = math.ceil(now / self.interval) * self.interval

    def _write(self, when: float, label: str) -> None:
        if self.latest is None and not label:
            return  # nothing read yet, and nothing to say
        stamp = datetime.fromtimestamp(when)
        path = background_path(stamp)
        fresh = not os.path.exists(path)
        temp_f, humidity = self.latest if self.latest else (None, None)
        with open(path, "a", newline="") as handle:
            writer = csv.writer(handle)
            if fresh:
                writer.writerow(self.HEADER)
            writer.writerow([
                stamp.strftime("%Y-%m-%dT%H:%M:%S"),
                "" if humidity is None else f"{humidity:.2f}",
                "" if temp_f is None else f"{temp_f:.2f}",
                label,
            ])


def session_avg_humidity(path: str) -> float | None:
    """Mean humidity of a recorded session, or None if it holds no readings."""
    try:
        with open(path, newline="") as handle:
            values = [
                float(row["humidity"])
                for row in csv.DictReader(handle)
                if row.get("humidity")
            ]
    except (OSError, ValueError, KeyError):
        return None  # a malformed file must not take down the summary
    return sum(values) / len(values) if values else None


def todays_runs(when: datetime | None = None) -> list[tuple[str, float]]:
    """(session name, average humidity) for each session recorded today."""
    when = when or datetime.now()
    runs = []
    pattern = os.path.join(day_dir(when), f"{when:%Y-%m-%d}__*.csv")
    for path in sorted(glob.glob(pattern)):
        if path.endswith(BACKGROUND_SUFFIX):
            continue  # the always-on trace is not a session
        average = session_avg_humidity(path)
        if average is None:
            continue
        runs.append((session_label(path), average))
    return runs


def session_label(path: str) -> str:
    """The session name out of a filename, falling back to the time of day.

    2026-08-22__1-59pm__my-session.csv -> "my-session"
    """
    parts = os.path.basename(path)[:-4].split("__")
    return parts[2] if len(parts) > 2 else parts[1]


def metadata_path(csv_path: str) -> str:
    return os.path.splitext(csv_path)[0] + ".json"


# Each is reported as "seconds_from_<start>_to_<end>" in the session's JSON.
LABEL_INTERVALS = [
    ("kettle_hot", "kernels_in"),
    ("kernels_in", "pop_started"),
    ("kernels_in", "pop_rapid"),
    ("kernels_in", "heat_off"),
    ("kernels_in", "dump_kettle"),
]


def label_intervals(label_times: dict[str, float]) -> dict[str, float | None]:
    """Seconds between pairs of markers, null where either was never pressed."""
    out = {}
    for start, end in LABEL_INTERVALS:
        a, b = label_times.get(start), label_times.get(end)
        out[f"seconds_from_{start}_to_{end}"] = (
            None if a is None or b is None else round(b - a, 1)
        )
    return out


def write_metadata(csv_path: str, meta: dict) -> str:
    """Write the run's sidecar JSON alongside its CSV."""
    path = metadata_path(csv_path)
    with open(path, "w") as handle:
        json.dump(meta, handle, indent=2)
        handle.write("\n")
    return path


DEMO_NAME = "demo"


def session_dir(name: str, when: datetime | None = None) -> str:
    """Where a session's files go: its own folder if it is a demo run.

    Matched on the slug, so "Demo" and " demo " land there too — those already
    produce the same filename as "demo" everywhere else.
    """
    if slugify(name) == DEMO_NAME:
        path = os.path.join(data_dir(), DEMO_NAME)
        os.makedirs(path, exist_ok=True)
        return path
    return day_dir(when)


def session_path(name: str, when: datetime | None = None) -> str:
    when = when or datetime.now()
    data_dir_path = session_dir(name, when)

    stamp = f"{when:%Y-%m-%d}__{when.hour % 12 or 12}-{when:%M}{when:%p}".lower()
    slug = slugify(name)
    base = f"{stamp}__{slug}" if slug else stamp

    path = os.path.join(data_dir_path, f"{base}.csv")
    n = 2
    while os.path.exists(path):
        path = os.path.join(data_dir_path, f"{base}-{n}.csv")
        n += 1
    return path


# --------------------------------------------------------------------------
# Terminal
# --------------------------------------------------------------------------


@contextmanager
def cbreak_stdin(hide_cursor=True):
    """Read keys one at a time; always restore the terminal, even on a crash."""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        if hide_cursor:
            sys.stdout.write("\033[?25l")
            sys.stdout.flush()
        yield
    finally:
        sys.stdout.write("\033[?25h\033[0m")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def read_keys():
    """Every keystroke currently available.

    Read straight from the file descriptor: sys.stdin.read(1) pulls a whole
    chunk into Python's buffer and hands back one character, after which
    select() sees an idle fd while the rest sits stranded in that buffer. That
    strands input on a paste or a fast burst of typing.
    """
    try:
        data = os.read(sys.stdin.fileno(), 1024)
    except (BlockingIOError, InterruptedError):
        return ""
    return data.decode("utf-8", "replace")


def make_idle(source, background, live=None):
    """An idle callback for prompt_line that keeps the sensor pumping.

    Only one place may call source.poll(), because polling drains the serial
    buffer — so it is called here and the readings are handed to each consumer.
    """
    def idle():
        try:
            readings, _ = source.poll()
        except Exception:
            readings = []  # a flaky sensor must not wedge a prompt
        background.observe(readings)
        background.tick()
        if live is None:
            return False
        live.observe(readings)
        return live.tick(time.time())

    return idle


def prompt_line(prompt, on_idle=None):
    """Read one line, repainting `live` while the user types.

    Hand-rolled rather than using input() because input() goes through readline,
    which redraws from its own cached cursor position; anything else painting
    the screen invalidates that and corrupts the line on backspace. Owning the
    echo here keeps the two in step.
    """
    buf: list[str] = []

    def draw():
        sys.stdout.write("\r\033[2K" + prompt + "".join(buf))
        sys.stdout.flush()

    with cbreak_stdin(hide_cursor=False):
        draw()
        while True:
            if on_idle is not None and on_idle():
                draw()  # a repaint moved the cursor; put it back
            if not select.select([sys.stdin], [], [], 0.1)[0]:
                continue
            keys = read_keys()
            if keys == "":
                raise EOFError
            i = 0
            while i < len(keys):
                key = keys[i]
                i += 1
                if key in ("\r", "\n"):
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return "".join(buf)
                if key == "\x03":
                    raise KeyboardInterrupt
                if key in ("\x7f", "\b"):
                    if buf:
                        buf.pop()
                elif key == "\x15":  # ctrl-u clears the line
                    buf.clear()
                elif key == "\x1b":  # skip arrow keys and other escapes
                    if i < len(keys) and keys[i] in "[O":
                        i += 1
                        while i < len(keys) and not (keys[i].isalpha() or keys[i] == "~"):
                            i += 1
                        i += 1
                elif key.isprintable():
                    buf.append(key)
            draw()


def term_size():
    """The terminal's real size.

    shutil.get_terminal_size() trusts the COLUMNS/LINES environment variables
    ahead of the terminal itself, and those go stale after a resize (and are
    often wrong under VS Code and tmux). An over-large height here means an
    over-tall chart, which scrolls the frame on every redraw.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            return os.get_terminal_size(stream.fileno())
        except (OSError, ValueError, AttributeError):
            continue
    return shutil.get_terminal_size((100, 30))


def boxed(body, box_w):
    """Draw (text, style) rows centred inside a rounded border."""
    inner = box_w - 4
    out = ["╭" + "─" * (box_w - 2) + "╮"]
    for text, style in body:
        # center() never truncates, so clamp first — an over-long line would
        # otherwise push the right-hand border out and break the box.
        if len(text) > inner:
            text = text[: inner - 1] + "…"
        centred = text.center(inner)
        out.append(f"│ {style}{centred}{RESET if style else ''} │")
    out.append("╰" + "─" * (box_w - 2) + "╯")
    return out


SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values, width, min_span=2.0):
    """A tiny block-character chart of the most recent `width` values.

    The scale never shrinks below `min_span`, so a signal that is essentially
    flat reads as flat instead of having its noise amplified to full height.
    """
    recent = list(values)[-width:]
    if not recent:
        return ""
    lo, hi = min(recent), max(recent)
    if hi - lo < min_span:
        middle = (hi + lo) / 2
        lo, hi = middle - min_span / 2, middle + min_span / 2
    span = hi - lo
    if span < 1e-9:
        return SPARK[len(SPARK) // 2] * len(recent)
    steps = len(SPARK) - 1
    return "".join(SPARK[round((v - lo) / span * steps)] for v in recent)


def title_rows(name, inner, gap=True):
    """Block-letter rows for `name`, wrapped over several lines if too wide.

    Returns None when even a single word is too wide to render, leaving the
    caller to fall back to plain text.
    """
    lines, current = [], ""
    for word in name.split():
        candidate = f"{current} {word}".strip()
        if current and bigtext.width(candidate) > inner:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)

    rows = []
    for i, line in enumerate(lines):
        if bigtext.width(line) > inner:
            return None
        if i and gap:
            rows.append("")  # breathing room between title lines
        rows.extend(bigtext.render(line))
    return rows


def big_stats(max_rh, avg_rh, inner):
    """Two headline stats side by side as (text, style) rows, or None if too wide."""
    left = bigtext.render(f"{max_rh:.2f}")
    right = bigtext.render(f"{avg_rh:.2f}")
    labels = ("MAX HUMIDITY  %RH", "AVERAGE HUMIDITY  %RH")
    lw = max(len(left[0]), len(labels[0]))
    rw = max(len(right[0]), len(labels[1]))
    gap = 6
    if lw + gap + rw > inner:
        return None

    out = [(labels[0].center(lw) + " " * gap + labels[1].center(rw), DIM)]
    for i in range(5):
        out.append(
            (left[i].center(lw) + " " * gap + right[i].center(rw), CYAN + BOLD)
        )
    return out


def summary_panel(name, clock, avg_temp_f, max_rh, avg_rh, path, pun, today_line):
    """A bordered end-of-session panel. Returns a list of printable lines."""
    term_w, term_h = term_size()
    box_w = min(BOX_MAX_WIDTH, max(24, term_w - 2))
    inner = box_w - 4  # two border chars plus a space of padding each side

    # Each entry is (plain text, style) so the text can be centred on its real
    # width before the escape codes are wrapped around it.
    # Trim the session name rather than the metadata, so duration and average
    # temperature survive on a narrow terminal.
    meta = f"  ·  {clock}  ·  {avg_temp_f:.2f} °F avg"
    name_max = max(8, inner - len(meta))
    short_name = name if len(name) <= name_max else name[: name_max - 1] + "…"

    body = [
        ("SESSION COMPLETE", BOLD),
        (short_name + meta, DIM),
        ("", ""),
    ]

    # Fall back to a compact line if the terminal is too narrow for the big type
    # or too short to show the panel and the menu that follows it.
    stats = big_stats(max_rh, avg_rh, inner)
    compact = f"MAX {max_rh:.2f} %RH     AVERAGE {avg_rh:.2f} %RH"
    if stats and term_h >= 24:
        body.extend(stats)
    elif len(compact) <= inner:
        body.append((compact, CYAN + BOLD))
    else:
        body.append((f"MAX {max_rh:.2f} %RH", CYAN + BOLD))
        body.append((f"AVERAGE {avg_rh:.2f} %RH", CYAN + BOLD))
    body.append(("", ""))
    if today_line:
        body.append((today_line, DIM))
    body.extend((ln, HIGHLIGHT) for ln in textwrap.wrap(pun, inner))
    body.append(("", ""))
    # Keep the filename rather than the directory when the path won't fit.
    body.append((path if len(path) <= inner else os.path.basename(path), DIM))

    return boxed(body, box_w)


def wrap_tokens(items, width, sep="   "):
    """Flow (plain, styled) items onto lines, wrapping on whole items.

    An item is never split across two lines, and width is measured on the
    uncoloured text so escape codes don't count against it. Returns
    (plain, styled) pairs so callers can still centre on the real width.
    """
    lines = []
    plain_line = styled_line = ""
    for plain, styled in items:
        if plain_line and len(plain_line) + len(sep) + len(plain) > width:
            lines.append((plain_line, styled_line))
            plain_line, styled_line = plain, styled
        elif plain_line:
            plain_line += sep + plain
            styled_line += sep + styled
        else:
            plain_line, styled_line = plain, styled
    if styled_line:
        lines.append((plain_line, styled_line))
    return lines


def hint_lines(width, window, last_marker):
    """The key hints as one flowing line, wrapped to fit, last marker in yellow."""
    items = []
    for key, label in config.MARKERS.items():
        plain = f"[{key}] {label}"
        styled = f"{HIGHLIGHT}{plain}{RESET}" if key == last_marker else plain
        items.append((plain, styled))
    for plain in ("[enter] stop", f"[-/=] window={window}s"):
        items.append((plain, plain))
    return [styled for _, styled in wrap_tokens(items, width)]


class LiveReadings:
    """Sparklines of the live sensor values, on reserved welcome-screen rows.

    Driven by prompt_line rather than a thread: painting the screen while a
    line editor is running has to happen between keystrokes, not alongside
    them, or the two fight over the cursor.
    """

    ROWS = 2

    def __init__(self, row, box_w, period=1.0):
        self.latest: tuple[float, float] | None = None
        self.row = row  # 1-based screen row of the first line
        self.box_w = box_w
        self.period = period
        self.width = max(12, min(44, box_w - 4 - 22))
        self.humidity = deque(maxlen=self.width)
        self.temps = deque(maxlen=self.width)
        self.next_poll = 0.0

    def observe(self, readings):
        """Take the newest reading from whoever is polling the sensor."""
        if readings:
            temp_c, humidity = readings[-1]
            self.latest = (c_to_f(temp_c), humidity)

    def tick(self, now):
        """Append a history point and repaint if due. True if the screen moved."""
        if now < self.next_poll:
            return False
        self.next_poll = now + self.period
        if self.latest is not None:
            self.temps.append(self.latest[0])
            self.humidity.append(self.latest[1])
        self._paint()
        return True

    # Smallest scale each sparkline will use, so ordinary sensor jitter reads as
    # a flat line rather than a dramatic sawtooth. Temperature gets the wider
    # floor because a degree or two of drift is not news; humidity moves less.
    HUMIDITY_MIN_SPAN = 2.0   # %RH
    TEMP_MIN_SPAN = 5.0       # °F

    def _rows(self):
        if not self.humidity:
            return ["waiting for the sensor…", ""]
        humidity_spark = sparkline(self.humidity, self.width, self.HUMIDITY_MIN_SPAN)
        temp_spark = sparkline(self.temps, self.width, self.TEMP_MIN_SPAN)
        return [
            f"humidity {self.humidity[-1]:6.1f} %RH  {humidity_spark}",
            f"temp     {self.temps[-1]:6.1f} °F   {temp_spark}",
        ]

    def _paint(self):
        inner = self.box_w - 4
        rows = [r[:inner] for r in self._rows()]
        # One shared indent so the two sparklines stay aligned with each other.
        indent = " " * max(0, (inner - max(len(r) for r in rows)) // 2)
        moves = "".join(
            f"\033[{self.row + i};1H│ {GREY}{(indent + text).ljust(inner)}{RESET} │"
            for i, text in enumerate(rows)
        )
        sys.stdout.write("\033[s" + moves + "\033[u")
        sys.stdout.flush()


def welcome_screen():
    """The title / instructions screen shown while a session is being set up."""
    width, height = term_size()
    box_w = min(BOX_MAX_WIDTH, max(24, width - 2))
    inner = box_w - 4
    tagline = random.choice(phrases.TAGLINES)  # fixed across sizing attempts

    def centred(text, style=""):
        if len(text) > inner:  # center() never truncates
            text = text[: inner - 1] + "…"
        return f"{style}{text.center(inner)}{RESET if style else ''}"

    def build(pad, big, gap=True):
        # Title and tagline sit inside a box; the instructions stay outside it,
        # so the branding reads as a separate block from the how-to below.
        banner = title_rows(config.APP_NAME, inner, gap) if big else None
        if banner is None:  # too wide for block letters, or not wanted
            banner = [config.APP_NAME.upper()]

        body = [("", "")] if pad else []
        body.extend((row, CYAN + BOLD) for row in banner)
        body.append(("", ""))
        body.append((tagline, HIGHLIGHT))
        # Blank rows held for LiveReadings to paint over once the prompts are up.
        live_index = len(body)
        body.extend([("", "")] * LiveReadings.ROWS)
        if pad:
            body.append(("", ""))

        prefix = ["", ""] if pad else [""]
        out = list(prefix)
        # body[i] lands at out index len(prefix) + 1 + i (past the top border),
        # and screen rows are 1-based.
        live_row = len(prefix) + live_index + 2
        out.extend(boxed(body, box_w))
        out.append("")
        out.append(centred("While recording, press a number to mark that moment:", BOLD))
        out.append("")

        items = [
            (f"[{k}] {v}", f"{HIGHLIGHT}[{k}]{RESET} {v}")
            for k, v in config.MARKERS.items()
        ]
        for plain, styled in wrap_tokens(items, inner):
            out.append(" " * ((inner - len(plain)) // 2) + styled)

        out.append("")
        out.append(centred("enter stops the recording   ·   - / = resize the graph", DIM))
        return out, live_row

    # Six prompts print underneath (name, popcorn qty, humidity, temperature,
    # fan speed, Enter). Shed the padding, then the gap between title lines, then the
    # block letters, until the whole thing fits above them.
    room = height - 6
    screen, live_row = build(True, True)
    for attempt in ((False, True, True), (False, True, False), (False, False, True)):
        if len(screen) <= room:
            break
        screen, live_row = build(*attempt)
    return screen, live_row, box_w


def render(
    points, elapsed, latest, rows, note, session_name, window, last_marker,
    marker_events,
):
    """Draw the readout + rolling humidity chart, in place."""
    width, height = term_size()

    if latest:
        temp_f, humidity = latest
        readout = (
            f"  {humidity:6.2f} %RH   {temp_f:6.2f} °F   "
            f"t+{int(elapsed // 60):02d}:{int(elapsed % 60):02d}   {rows} rows"
        )
    else:
        readout = "  waiting for the first reading..."

    # Wrap hints to width - 4 so that, once indented, no line ever reaches the
    # last column — landing exactly on it makes some terminals wrap early.
    lines = [
        # Two blank rows of top padding. chart_height is derived from
        # len(lines), so these shrink the chart rather than growing the frame.
        "",
        "",
        f"  RECORDING  {session_name}"[: width - 1],
        readout[: width - 1],
        "",
        *(f"  {ln}" for ln in hint_lines(max(20, width - 4), window, last_marker)),
    ]
    if note:  # only take a row when there is actually something to say
        lines.append(f"  {note}"[: width - 1])

    # Total drawn height is len(lines) + chart_height, which must stay under the
    # terminal height or the frame scrolls.
    chart_height = max(6, height - len(lines) - 1)
    plt.clf()
    plt.theme("clear")  # inherit the terminal's own colours instead of forcing white
    plt.plotsize(width - 2, chart_height)

    if len(points) >= 2:
        # Grow the axis with the session until it reaches the window size, then
        # roll. Only the visible slice is handed to plotext — the buffer holds
        # more so that widening the window again can show the older data.
        end = max(points[-1][0], 1.0)
        start = max(0.0, end - window)
        visible = [p for p in points if p[0] >= start] or list(points)[-2:]
        xs = [p[0] for p in visible]
        ys = [p[1] for p in visible]
        plt.plot(xs, ys, marker="braille", color="cyan")
        plt.xlim(start, end)
        lo, hi = min(ys), max(ys)
        pad = max(1.0, (hi - lo) * 0.15)
        y_lo, y_hi = max(0.0, lo - pad), min(100.0, hi + pad)
        plt.ylim(y_lo, y_hi)

        # A vertical rule and the bare key for every marker still in view.
        for at, key in marker_events:
            if start <= at <= end:
                plt.vertical_line(at, color="yellow")
                plt.text(
                    key,
                    x=at,
                    y=y_hi,
                    color="black",
                    background="yellow",
                    style="bold",
                    alignment="center",
                )
    else:
        plt.plot([0, 1], [0, 0])

    span = int(min(elapsed, window)) or 1
    plt.title(f"humidity — last {span}s")
    plt.xlabel("seconds elapsed")

    # plt.build() ends with a newline; keeping it would park the cursor one row
    # below the frame and scroll the terminal by a line on every redraw.
    chart = plt.build().rstrip("\n")
    out = "\033[H" + "\n".join(lines) + "\n" + chart + "\033[0J"
    sys.stdout.write(out)
    sys.stdout.flush()


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def record(
    source,
    background: BackgroundLog,
    name: str,
    ambient_rh: int | None = None,
    ambient_f: int | None = None,
    fan_pct: int | None = None,
    popcorn_cups: float | None = None,
) -> None:
    path = session_path(name)

    # Skipped answers are recorded as null rather than omitted, so the sidecar
    # has a stable shape and a blank is distinguishable from a missing field.
    meta = {
        "session_name": name or None,
        "csv": os.path.basename(path),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "popcorn_qty_cups": popcorn_cups,
        "ambient_humidity_rh": ambient_rh,
        "ambient_temperature_f": ambient_f,
        "fan_speed_pct": fan_pct,
    }
    write_metadata(path, meta)  # written up front so a crashed run keeps it
    background.note(f"recording_started: {os.path.basename(path)}")
    background.tick()
    display_name = name or "(untitled)"

    points: deque[tuple[float, float]] = deque()
    pending: deque[str] = deque()
    start: float | None = None
    last_second = -1
    latest: tuple[float, float] | None = None
    rows = 0
    humidities: list[float] = []
    temps_f: list[float] = []
    note = ""
    note_until = 0.0
    last_draw = 0.0
    # Session-local view setting: adjustable with -/= while recording, and
    # deliberately not persisted, so each session starts at the configured span.
    window = config.PLOT_WINDOW_SECONDS
    retain = window
    last_marker: str | None = None
    marker_events: deque[tuple[float, str]] = deque()  # (elapsed, key) for the graph
    # When each label was last pressed, in seconds elapsed — the last press
    # wins, so a marker can be corrected by pressing it again.
    label_times: dict[str, float] = {}

    f = open(path, "w", newline="")
    writer = csv.writer(f)
    writer.writerow(HEADER)
    f.flush()

    sys.stdout.write("\033[2J")
    try:
        with cbreak_stdin():
            while True:
                stopping = False
                if select.select([sys.stdin], [], [], 0.05)[0]:
                    for key in read_keys():
                        if key in ("\r", "\n"):
                            stopping = True
                            break
                        if key in config.MARKERS:
                            pending.append(config.MARKERS[key])
                            last_marker = key  # highlighted in the hint line
                            at = 0.0 if start is None else time.time() - start
                            marker_events.append((at, key))
                            label_times[config.MARKERS[key]] = at
                        elif key in ("-", "="):
                            if key == "-":
                                window += WINDOW_STEP
                                retain = max(retain, window)
                            else:
                                window = max(WINDOW_MIN, window - WINDOW_STEP)
                if stopping:
                    break

                readings, notes = source.poll()
                if notes:
                    note = f"sensor: {notes[-1]}"
                    note_until = time.time() + 3

                now = time.time()
                # The slow background trace keeps running through the session.
                background.observe(readings)
                background.tick(now)
                for temp_c, humidity in readings:
                    if start is None:
                        start = now
                    elapsed = now - start
                    temp_f = c_to_f(temp_c)
                    latest = (temp_f, humidity)
                    points.append((elapsed, humidity))

                    second = int(elapsed)
                    if second > last_second:
                        last_second = second
                        writer.writerow([
                            # Derived from the second boundary rather than the
                            # write moment, so wall-clock seconds run 1:1 with
                            # seconds_elapsed and line up with the background file.
                            datetime.fromtimestamp(int(start) + second)
                            .strftime("%Y-%m-%dT%H:%M:%S"),
                            second,
                            f"{humidity:.2f}",
                            f"{temp_f:.2f}",
                            pending.popleft() if pending else "",
                        ])
                        f.flush()
                        rows += 1
                        humidities.append(humidity)
                        temps_f.append(temp_f)

                # Trim the plot buffer to the widest window used this session, so
                # widening the view with `-` can still reach back over old data.
                if points:
                    cutoff = points[-1][0] - retain
                    while points and points[0][0] < cutoff:
                        points.popleft()
                    while marker_events and marker_events[0][0] < cutoff:
                        marker_events.popleft()

                if now - last_draw >= 0.25:
                    last_draw = now
                    if note and now > note_until:
                        note = ""
                    elapsed = 0.0 if start is None else now - start
                    render(
                        points, elapsed, latest, rows, note,
                        display_name, window, last_marker, marker_events,
                    )
    except KeyboardInterrupt:
        pass  # Ctrl-C stops the session; the CSV is still finalized below.
    finally:
        f.close()

    # Any markers pressed after the last row still belong in the file.
    if pending:
        with open(path, "a", newline="") as tail:
            tail_writer = csv.writer(tail)
            while pending:
                last_second += 1
                tail_writer.writerow([
                    # Same second-boundary basis as the rows above it.
                    datetime.fromtimestamp(
                        (int(start) if start else int(time.time())) + last_second
                    ).strftime("%Y-%m-%dT%H:%M:%S"),
                    last_second,
                    "",
                    "",
                    pending.popleft(),
                ])
                rows += 1

    background.note(f"recording_ended: {os.path.basename(path)}")
    background.tick()

    duration = 0.0 if start is None else time.time() - start
    clock = f"{int(duration // 60):02d}:{int(duration % 60):02d}"

    # Fold in what the run itself produced, then rewrite the sidecar.
    meta.update({
        "ended_at": datetime.now().isoformat(timespec="seconds"),
        "duration_seconds": round(duration),
        "rows": rows,
        "humidity_max": round(max(humidities), 2) if humidities else None,
        "humidity_avg": (
            round(sum(humidities) / len(humidities), 2) if humidities else None
        ),
        "temperature_f_avg": (
            round(sum(temps_f) / len(temps_f), 2) if temps_f else None
        ),
        **label_intervals(label_times),
    })
    write_metadata(path, meta)

    # Clear and home the cursor so the panel is drawn from the top of a fresh
    # screen — it can't be scrolled off by the frames that came before it.
    # The two blank rows match the recording screen's top padding.
    sys.stdout.write("\033[2J\033[H\n\n")

    if humidities:
        # Read back today's sessions — this one included, since it is already
        # written and closed — for the day's best and worst averages.
        runs = todays_runs()
        today_line = ""
        if len(runs) >= 2:
            best = min(runs, key=lambda run: run[1])
            worst = max(runs, key=lambda run: run[1])
            today_line = (
                f"today's best avg {best[1]:.2f} %RH ({short(best[0])})"
                f"   ·   worst avg {worst[1]:.2f} %RH ({short(worst[0])})"
            )

        panel = summary_panel(
            display_name,
            clock,
            sum(temps_f) / len(temps_f),
            max(humidities),
            sum(humidities) / len(humidities),
            os.path.relpath(path),
            random.choice(phrases.PHRASES),
            today_line,
        )
        print("\n".join(panel))
    else:
        print(f"{display_name} · {clock} · no readings · {os.path.relpath(path)}")
    print()


# --------------------------------------------------------------------------
# Menu
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="serial port (default: autodetect)")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="generate fake readings instead of reading the sensor",
    )
    args = parser.parse_args()

    global SIMULATED
    SIMULATED = args.simulate

    source = SimulatedSource() if args.simulate else SerialSource(args.port)
    background = BackgroundLog()

    # Launching the script is itself the request to start a session, so the
    # first pass drops straight into the new-session screen.
    skip_menu = True
    # The room doesn't change between batches, so it is asked for once per run
    # and reused; a skipped answer is asked for again on the next batch.
    ambient_rh: int | None = None
    ambient_f: int | None = None
    try:
        while True:
            if not skip_menu:
                print("  [enter] new session    [q] quit")
                # Not input(): the menu has to keep pumping the sensor so the
                # background trace carries on between sessions.
                try:
                    choice = prompt_line(
                        "> ", make_idle(source, background)
                    ).strip().lower()
                except EOFError:
                    break
                if choice in ("q", "quit"):
                    break
                if choice not in ("", "n", "new"):
                    print(f"  Unknown option: {choice}\n")
                    continue
            skip_menu = False

            # The welcome screen stays on show while the name is typed and
            # while we wait for the Enter that starts the run.
            sys.stdout.write("\033[2J\033[H")
            screen, live_row, box_w = welcome_screen()
            print("\n".join(screen))
            # The sparklines repaint between keystrokes as the prompts run.
            live = LiveReadings(live_row, box_w)
            idle = make_idle(source, background, live)
            try:
                name = prompt_line("  Session name: ", idle).strip()
                popcorn_cups = ask_optional_number(
                    "  Popcorn qty in cups, e.g. 1 or 0.5 (optional, Enter to skip): ",
                    0, None, idle, integer=False,
                )
                if ambient_rh is None:
                    ambient_rh = ask_optional_number(
                        "  Room humidity %RH (optional, Enter to skip): ", 0, 100, idle
                    )
                else:
                    print(f"  Room humidity %RH: {ambient_rh} (from earlier this run)")
                if ambient_f is None:
                    ambient_f = ask_optional_number(
                        "  Room temperature °F (optional, Enter to skip): ", on_idle=idle
                    )
                else:
                    print(f"  Room temperature °F: {ambient_f} (from earlier this run)")
                fan_pct = ask_optional_number(
                    "  Fan speed % (optional, Enter to skip): ", 0, 100, idle
                )
                prompt_line("  Press Enter to start recording...", idle)
            except EOFError:
                break
            record(
                source, background, name, ambient_rh, ambient_f, fan_pct, popcorn_cups
            )
    except KeyboardInterrupt:
        print()
    finally:
        source.close()
        print("Done.")


if __name__ == "__main__":
    main()
