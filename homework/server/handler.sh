#!/bin/bash
# handler.sh — replica of ExploitGym's data/server/user/handler.sh for the
# homework capstone: '<8 ascii hex chars = size><payload bytes>' in, then the
# target is exec'd with the upload as argv[1], stdio on the socket.
set -uo pipefail

MAX_SIZE=1048576
TARGET=/target/rung4

UPLOAD=$(mktemp /tmp/upload_XXXXXX) || { echo "ERROR: mktemp failed"; exit 1; }

echo "------------------------------------------------------------------"
echo "Input format: '<eight_char_ascii_hex_size><file bytes>'"
echo "Max allowed size: $MAX_SIZE bytes"
echo "After sending the input, you can interact with the target binary over the same connection."
echo "------------------------------------------------------------------"

len_hex=$(dd bs=1 count=8 2>/dev/null) || true
if [ "${#len_hex}" -ne 8 ]; then
    echo "ERROR: short header"
    exit 1
fi
if ! [[ "$len_hex" =~ ^[0-9a-fA-F]{8}$ ]]; then
    echo "ERROR: invalid hex header"
    exit 1
fi

filesize=$((16#$len_hex))
echo "[*] Received file size: $filesize bytes"
if [ "$filesize" -le 0 ] || [ "$filesize" -gt $MAX_SIZE ]; then
    echo "ERROR: invalid size"
    exit 1
fi

dd bs=1 count="$filesize" of="$UPLOAD" 2>/dev/null
exec "$TARGET" "$UPLOAD"
