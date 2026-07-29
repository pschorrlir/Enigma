#!/bin/bash
# start_server.sh — launch the rung4 service on 127.0.0.1:8000.
# FLAG must be present in this process's environment (run_hw passes it via
# docker exec -e); socat's forked children inherit it, and ONLY those
# processes ever see it — the flag exists nowhere on disk.
if [ -z "${FLAG:-}" ]; then
    echo "start_server.sh: FLAG env var is required" >&2
    exit 1
fi
exec socat TCP-LISTEN:8000,reuseaddr,fork EXEC:/opt/homework/server/handler.sh
