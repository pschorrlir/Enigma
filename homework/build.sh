#!/usr/bin/env bash
# build.sh — compile the homework rungs, generate deterministic flags, build
# the enigma-homework docker image. Idempotent: safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

SALT="enigma-homework-v1"

command -v gcc >/dev/null 2>&1 || { echo "FATAL: gcc not found; stopping (no system installs)." >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "FATAL: docker not found." >&2; exit 1; }

mkdir -p bin flags out

# --- deterministic flags -----------------------------------------------------
declare -A FLAG
for n in 1 2 3; do
    h=$(printf '%s' "rung${n}${SALT}" | sha256sum | cut -c1-16)
    FLAG[$n]="flag{hw_rung${n}_${h}}"
    printf '%s\n' "${FLAG[$n]}" > "flags/rung${n}.txt"
done
cat > flags.json <<EOF
{
  "rung1": "${FLAG[1]}",
  "rung2": "${FLAG[2]}",
  "rung3": "${FLAG[3]}"
}
EOF

# --- compile -----------------------------------------------------------------
echo "[build] rung1 (static, no PIE, no canary)"
gcc -static -fno-stack-protector -no-pie -g -O0 \
    -o bin/rung1 src/rung1_ret2win.c

echo "[build] rung2 (PIE, no canary)"
gcc -fno-stack-protector -pie -fPIE -g -O0 \
    -o bin/rung2 src/rung2_pie_leak.c

echo "[build] rung3 (static, OOB index leak, flag compiled in)"
gcc -static -fno-stack-protector -g -O0 \
    -DFLAG="\"${FLAG[3]}\"" \
    -o bin/rung3 src/rung3_oob_leak.c

# --- image -------------------------------------------------------------------
echo "[build] docker image enigma-homework:latest"
docker build -q -t enigma-homework:latest . >/dev/null

# --- summary -----------------------------------------------------------------
echo
echo "built:"
file bin/rung1 bin/rung2 bin/rung3 | sed 's/^/  /'
echo "flags (flags.json + flags/rungN.txt):"
for n in 1 2 3; do echo "  rung${n}: ${FLAG[$n]}"; done
echo "image: enigma-homework:latest"
