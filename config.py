"""Editable settings for the popcorn telemetry recorder."""

# Shown in block letters on the welcome screen.
APP_NAME = "Poptometry Analyzer"

# Press one of these keys while recording to tag that moment in the CSV.
# Add a row to extend the list — any single printable key works, and the
# recorder picks it up for both the keypress handling and the on-screen legend.
MARKERS = {
    "1": "kettle_hot",
    "2": "kernels_in",
    "3": "pop_started",
    "4": "pop_rapid",
    "5": "heat_off",
    "6": "stir_off",
    "7": "dump_kettle",
    "8": "door_opened",
    "9": "door_closed",
}

# Serial link to the ESP32. Must match Serial.begin() in firmware/src/main.cpp.
BAUD = 115200

# Widest span the live graph shows. Below this the x-axis grows with elapsed
# time (at 30s in, the graph is 30s wide); once the session passes it, the graph
# becomes a rolling window of this length. The CSV always records the full
# session regardless of this.
PLOT_WINDOW_SECONDS = 120

# How often the always-on background trace takes a reading, in seconds. It runs
# for as long as the script does, including while a session is recording.
BACKGROUND_INTERVAL_SECONDS = 10

# Where sessions are written, relative to this file. Running with --simulate
# uses the second one instead, so fake runs never mix in with real ones.
DATA_DIR = "data"
DATA_DIR_SIMULATED = "data-simulated"
