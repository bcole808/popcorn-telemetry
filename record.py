#!/usr/bin/env python3
"""Interactive recorder for SHT45 humidity data from the popcorn machine.

Run with:  uv run record.py            (reads the ESP32 over USB serial)
           uv run record.py --simulate (fake data, no hardware needed)
"""

from __future__ import annotations

import argparse
import csv
import glob
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

HEADER =["seconds_elapsed", "humidity", "temperature_f", "label", "timestamp"]

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


def short(name: str, limit: int = 13) -> str:
    """Clip a session name so two of them fit on one line of the panel."""
    return name if len(name) <= limit else name[: limit - 1] + "…"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug


def data_dir() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), config.DATA_DIR)
    os.makedirs(path, exist_ok=True)
    return path


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
    for path in sorted(glob.glob(os.path.join(data_dir(), f"{when:%Y-%m-%d}__*.csv"))):
        average = session_avg_humidity(path)
        if average is None:
            continue
        # 2026-08-22__4-45pm__my-session.csv -> "my-session"
        parts = os.path.basename(path)[:-4].split("__")
        runs.append((parts[2] if len(parts) > 2 else parts[1], average))
    return runs


def session_path(name: str, when: datetime | None = None) -> str:
    when = when or datetime.now()
    data_dir_path = data_dir()

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
def cbreak_stdin():
    """Read keys one at a time; always restore the terminal, even on a crash."""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        sys.stdout.write("\033[?25l")  # hide cursor
        sys.stdout.flush()
        yield
    finally:
        sys.stdout.write("\033[?25h\033[0m")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


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


def title_rows(name, inner):
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
        if i:
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
    for plain in ("[space] stop", f"[-/=] window={window}s"):
        items.append((plain, plain))
    return [styled for _, styled in wrap_tokens(items, width)]


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

    def build(pad, big):
        # Title and tagline sit inside a box; the instructions stay outside it,
        # so the branding reads as a separate block from the how-to below.
        banner = title_rows(config.APP_NAME, inner) if big else None
        if banner is None:  # too wide for block letters, or not wanted
            banner = [config.APP_NAME.upper()]

        body = [("", "")] if pad else []
        body.extend((row, CYAN + BOLD) for row in banner)
        body.append(("", ""))
        body.append((tagline, HIGHLIGHT))
        if pad:
            body.append(("", ""))

        out = ["", ""] if pad else [""]
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
        out.append(centred("space stops the recording   ·   - / = resize the graph", DIM))
        out.append("")
        return out

    # Leave room for the two prompts that print underneath, plus a spare row,
    # then shed padding and finally the block letters until it fits.
    room = height - 3
    screen = build(True, True)
    for pad, big in ((False, True), (False, False)):
        if len(screen) <= room:
            break
        screen = build(pad, big)
    return screen


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


def record(source, name: str) -> None:
    path = session_path(name)
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

    f = open(path, "w", newline="")
    writer = csv.writer(f)
    writer.writerow(HEADER)
    f.flush()

    sys.stdout.write("\033[2J")
    try:
        with cbreak_stdin():
            while True:
                ready, _, _ = select.select([sys.stdin], [], [], 0.05)
                if ready:
                    key = sys.stdin.read(1)
                    if key == " ":
                        break
                    if key in config.MARKERS:
                        pending.append(config.MARKERS[key])
                        last_marker = key  # highlighted in the hint line
                        marker_events.append(
                            (0.0 if start is None else time.time() - start, key)
                        )
                    elif key in ("-", "="):
                        if key == "-":
                            window += WINDOW_STEP
                            retain = max(retain, window)
                        else:
                            window = max(WINDOW_MIN, window - WINDOW_STEP)

                readings, notes = source.poll()
                if notes:
                    note = f"sensor: {notes[-1]}"
                    note_until = time.time() + 3

                now = time.time()
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
                            second,
                            f"{humidity:.2f}",
                            f"{temp_f:.2f}",
                            pending.popleft() if pending else "",
                            datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
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
                    last_second,
                    "",
                    "",
                    pending.popleft(),
                    datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                ])
                rows += 1

    duration = 0.0 if start is None else time.time() - start
    clock = f"{int(duration // 60):02d}:{int(duration % 60):02d}"

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

    source = SimulatedSource() if args.simulate else SerialSource(args.port)

    try:
        while True:
            print("  [enter] new session    [q] quit")
            try:
                choice = input("> ").strip().lower()
            except EOFError:
                break
            if choice in ("q", "quit"):
                break
            if choice not in ("", "n", "new"):
                print(f"  Unknown option: {choice}\n")
                continue

            # The welcome screen stays on show while the name is typed and
            # while we wait for the Enter that starts the run.
            sys.stdout.write("\033[2J\033[H")
            print("\n".join(welcome_screen()))
            name = input("  Session name: ").strip()
            input("  Press Enter to start recording...")
            record(source, name)
    except KeyboardInterrupt:
        print()
    finally:
        source.close()
        print("Done.")


if __name__ == "__main__":
    main()
