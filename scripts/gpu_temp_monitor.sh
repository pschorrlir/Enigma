#!/usr/bin/env bash
# gpu_temp_monitor.sh — poll a Windows-side LibreHardwareMonitor web server
# from WSL and log GPU temperatures. The GPU (RX 7900 XTX) lives on the
# Windows host where Ollama runs; WSL has no sensor access, so LHM's
# remote web server (Options > Remote Web Server > Run, port 8085) is the
# bridge.
#
# Usage:
#   scripts/gpu_temp_monitor.sh           # single sample, append to log
#   scripts/gpu_temp_monitor.sh --watch [secs]   # loop forever (tmux-friendly)
#
# Log: logs/gpu_temp.log — one CSV line per sample:
#   <iso-time>,<edge-C>,<junction-C>,<mem-junction-C>[,WARNING:<reason>]
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$HERE/logs"
LOG="$LOG_DIR/gpu_temp.log"
HOST="${GPU_TEMP_HOST:-172.18.16.1}"
PORT="${GPU_TEMP_PORT:-8085}"
# RX 7900 XTX guidance: edge ~85C sustained is hot, junction 110C is the
# throttle/shutdown spec. Warn early so a ladder run can be paused first.
EDGE_WARN="${GPU_TEMP_EDGE_WARN:-85}"
JUNC_WARN="${GPU_TEMP_JUNC_WARN:-100}"

mkdir -p "$LOG_DIR"

sample() {
    local json
    if ! json="$(curl -s -m 10 "http://$HOST:$PORT/data.json")" || [ -z "$json" ]; then
        echo "$(date -Iseconds),,,,BRIDGE-DOWN (is LibreHardwareMonitor running with the web server on?)" >> "$LOG"
        return 1
    fi
    python3 - "$LOG" "$EDGE_WARN" "$JUNC_WARN" << 'PYEOF' "$json"
import json, sys, datetime

log_path, edge_warn, junc_warn = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
data = json.loads(sys.argv[4])

temps = {}

def walk(node):
    # LHM tree: Text/Value/Type/Children; sensor Type == "Temperature"
    text = node.get("Text", "")
    if node.get("Type") == "Temperature" and node.get("Value"):
        try:
            val = float(str(node["Value"]).split(" ")[0].replace(",", "."))
        except ValueError:
            val = None
        if val is not None:
            key = text.lower()
            if "junction" in key and "mem" not in key:
                temps.setdefault("junction", val)
            elif "memory" in key or "mem" in key:
                temps.setdefault("mem", val)
            elif "edge" in key or "gpu" in key or "hot spot" in key:
                temps.setdefault("edge", val)
                if "hot spot" in key:
                    temps.setdefault("junction", val)
    for child in node.get("Children", []):
        walk(child)

walk(data)
edge = temps.get("edge")
junc = temps.get("junction")
mem = temps.get("mem")

warn = []
if edge is not None and edge >= edge_warn:
    warn.append("EDGE>=%.0fC" % edge_warn)
if junc is not None and junc >= junc_warn:
    warn.append("JUNCTION>=%.0fC" % junc_warn)

line = "%s,%s,%s,%s%s" % (
    datetime.datetime.now().isoformat(timespec="seconds"),
    "" if edge is None else "%.1f" % edge,
    "" if junc is None else "%.1f" % junc,
    "" if mem is None else "%.1f" % mem,
    (",WARNING:" + "+".join(warn)) if warn else "",
)
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(line + "\n")
if warn:
    print(line)
PYEOF
}

if [ "${1:-}" = "--watch" ]; then
    interval="${2:-300}"
    echo "watching every ${interval}s, logging to $LOG (warn edge>=${EDGE_WARN}C junction>=${JUNC_WARN}C)"
    while true; do
        sample || true
        sleep "$interval"
    done
else
    sample && tail -1 "$LOG"
fi
