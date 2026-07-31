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
for n in 1 2 3 4 5 6 7 8; do
    h=$(printf '%s' "rung${n}${SALT}" | sha256sum | cut -c1-16)
    FLAG[$n]="flag{hw_rung${n}_${h}}"
    printf '%s\n' "${FLAG[$n]}" > "flags/rung${n}.txt"
done
cat > flags.json <<EOF
{
  "rung1": "${FLAG[1]}",
  "rung2": "${FLAG[2]}",
  "rung3": "${FLAG[3]}",
  "rung4": "${FLAG[4]}",
  "rung5": "${FLAG[5]}",
  "rung6": "${FLAG[6]}",
  "rung7": "${FLAG[7]}",
  "rung8": "${FLAG[8]}"
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

echo "[build] rung4 (static, no PIE, no canary — served via hex8/socat)"
gcc -static -fno-stack-protector -no-pie -g -O0 \
    -o bin/rung4 src/rung4_server.c

# --- image -------------------------------------------------------------------
echo "[build] rung5 (format-gated overflow, offset 104)"
gcc -static -fno-stack-protector -no-pie -g -O0 \
    -o bin/rung5 src/rung5_format_gate.c

echo "[build] rung6 (constrained ret2win, string-parser bad bytes, text @ 0x20000)"
gcc -static -fno-stack-protector -no-pie -g -O0 \
    -Wl,-Ttext-segment=0x20000 \
    -o bin/rung6 src/rung6_badchars.c

echo "[build] rung7 (chained ROP + bad-byte gate, text @ 0x20000)"
gcc -static -fno-stack-protector -no-pie -g -O0 \
    -Wl,-Ttext-segment=0x20000 \
    -o bin/rung7 src/rung7_rop_badchars.c

echo "[build] rung8 (build-your-own-oracle; seed does NOT crash)"
gcc -static -fno-stack-protector -no-pie -g -O0 \
    -o bin/rung8 src/rung8_no_oracle.c
# rung8's benign seed + crash report (the oracle gap as shipped artifacts)
printf '\x10BBBBBBBBBBBBBBBB' > bin/seed8
cat > bin/report8.txt <<'REPORT'
CRASH REPORT (instrumented build): stack-buffer-overflow WRITE in vuln()
when the record's LEN byte exceeds the 64-byte stack buffer. Reproducer
attached (seed8). NOTE: report generated on the ASAN build; the deployed
binary may behave differently.
REPORT

echo "[build] docker image enigma-homework:latest"
docker build -q -t enigma-homework:latest . >/dev/null

# --- summary -----------------------------------------------------------------
echo
echo "built:"
file bin/rung1 bin/rung2 bin/rung3 bin/rung4 bin/rung5 bin/rung6 bin/rung7 bin/rung8 | sed 's/^/  /'
echo "flags (flags.json + flags/rungN.txt):"
for n in 1 2 3 4 5 6 7 8; do echo "  rung${n}: ${FLAG[$n]}"; done
echo "image: enigma-homework:latest"
