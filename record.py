#!/usr/bin/env python3
"""Interactive recorder for SHT45 humidity data from the popcorn machine.

Run with:  uv run record.py            (reads the ESP32 over USB serial)
           uv run record.py --simulate (fake data, no hardware needed)
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import random
import re
import select
import shutil
import sys
import termios
import time
import tty
from collections import deque
from contextlib import contextmanager
from datetime import datetime

import plotext as plt

import config

HEADER = ["seconds_elapsed", "humidity", "temperature_f", "label", "timestamp"]

# Serial devices that are never our board.
PORT_BLOCKLIST = ("Bluetooth-Incoming-Port", "debug-console")
PORT_HINTS = ("usbserial", "wchusbserial", "SLAB", "usbmodem")

# How far `-` / `=` move the visible time window, and how small it may get.
WINDOW_STEP = 30
WINDOW_MIN = 30

HIGHLIGHT = "\033[93m"  # bright yellow — marks the most recently pressed marker
RESET = "\033[0m"


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
                "or pass --port /dev/cu.something"
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


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug


def session_path(name: str, when: datetime | None = None) -> str:
    when = when or datetime.now()
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), config.DATA_DIR)
    os.makedirs(data_dir, exist_ok=True)

    stamp = f"{when:%Y-%m-%d}__{when.hour % 12 or 12}-{when:%M}{when:%p}".lower()
    slug = slugify(name)
    base = f"{stamp}__{slug}" if slug else stamp

    path = os.path.join(data_dir, f"{base}.csv")
    n = 2
    while os.path.exists(path):
        path = os.path.join(data_dir, f"{base}-{n}.csv")
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


def hint_lines(width, window, last_marker):
    """The key hints as one flowing line, wrapped to fit, last marker in yellow.

    Wraps on whole hints rather than on words, so a hint is never split across
    two lines, and measures width on the uncoloured text so the escape codes
    don't count against it.
    """
    items = []
    for key, label in config.MARKERS.items():
        plain = f"[{key}] {label}"
        styled = f"{HIGHLIGHT}{plain}{RESET}" if key == last_marker else plain
        items.append((plain, styled))
    for plain in ("[space] stop", f"[-/=] window={window}s"):
        items.append((plain, plain))

    sep = "   "
    lines: list[str] = []
    plain_line = styled_line = ""
    for plain, styled in items:
        if plain_line and len(plain_line) + len(sep) + len(plain) > width:
            lines.append(styled_line)
            plain_line, styled_line = plain, styled
        elif plain_line:
            plain_line += sep + plain
            styled_line += sep + styled
        else:
            plain_line, styled_line = plain, styled
    if styled_line:
        lines.append(styled_line)
    return lines


def render(points, elapsed, latest, rows, note, session_name, window, last_marker):
    """Draw the readout + rolling humidity chart, in place."""
    width, height = shutil.get_terminal_size((100, 30))

    if latest:
        temp_f, humidity = latest
        readout = (
            f"  {humidity:6.2f} %RH   {temp_f:6.2f} °F   "
            f"t+{int(elapsed // 60):02d}:{int(elapsed % 60):02d}   {rows} rows"
        )
    else:
        readout = "  waiting for the first reading..."

    lines = [
        f"  RECORDING  {session_name}"[:width],
        readout[:width],
        "",
        *(f"  {ln}" for ln in hint_lines(max(20, width - 2), window, last_marker)),
        f"  {note}"[:width] if note else "",
    ]

    chart_height = max(8, height - len(lines) - 2)
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
        plt.ylim(max(0.0, lo - pad), min(100.0, hi + pad))
    else:
        plt.plot([0, 1], [0, 0])

    span = int(min(elapsed, window)) or 1
    plt.title(f"humidity — last {span}s")
    plt.xlabel("seconds elapsed")

    out = "\033[H" + "\n".join(lines) + "\n" + plt.build() + "\033[0J"
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

                if now - last_draw >= 0.25:
                    last_draw = now
                    if note and now > note_until:
                        note = ""
                    elapsed = 0.0 if start is None else now - start
                    render(
                        points, elapsed, latest, rows, note,
                        display_name, window, last_marker,
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
    sys.stdout.write("\033[2J\033[H")

    if humidities:
        stats = (
            f"RH {min(humidities):.2f}/{max(humidities):.2f}/"
            f"{sum(humidities) / len(humidities):.2f} · "
            f"°F {min(temps_f):.2f}/{max(temps_f):.2f}/"
            f"{sum(temps_f) / len(temps_f):.2f} (min/max/avg)"
        )
    else:
        stats = "no readings"

    print(
        f"{display_name} · {clock} · {stats} · {rows} rows · "
        f"{os.path.relpath(path)}\n"
    )


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

            name = input("  Session name (blank = timestamp only): ").strip()
            input("  Press Enter to start recording...")
            record(source, name)
    except KeyboardInterrupt:
        print()
    finally:
        source.close()
        print("Done.")


if __name__ == "__main__":
    main()
