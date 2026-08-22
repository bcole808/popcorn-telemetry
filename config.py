"""Editable settings for the popcorn telemetry recorder."""

# Press one of these keys while recording to tag that moment in the CSV.
# Add a row to extend the list — any single printable key works, and the
# recorder picks it up for both the keypress handling and the on-screen legend.
MARKERS = {
    "1": "kernels_in",
    "2": "pop_started",
    "3": "heat_off",
    "4": "stir_off",
    "5": "dump_kettle",
    "6": "door_opened",
    "7": "door_closed",
}

# Serial link to the ESP32. Must match Serial.begin() in firmware/src/main.cpp.
BAUD = 115200

# Widest span the live graph shows. Below this the x-axis grows with elapsed
# time (at 30s in, the graph is 30s wide); once the session passes it, the graph
# becomes a rolling window of this length. The CSV always records the full
# session regardless of this.
PLOT_WINDOW_SECONDS = 120

# Where sessions are written, relative to this file.
DATA_DIR = "data"
