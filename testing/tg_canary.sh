#!/bin/bash
# Run one canary suite as the user that owns the Hermes home (it needs to read gateway_state.json).
# usage: tg_canary.sh <suite-name>          e.g. tg_canary.sh smoke   (reads suites/<suite-name>.json)
# env:   CANARY_PYTHON   python with telethon installed (default: python3)
#        CANARY_RESULTS  results root (default: ./results)
#        HERMES_HOME     Hermes home (default: ~/.hermes)
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
NAME=${1:?usage: tg_canary.sh <suite-name>}
SUITE=$HERE/suites/$NAME.json
OUT=${CANARY_RESULTS:-$HERE/results}/telegram-$NAME-$(date -u +%Y%m%dT%H%M%SZ)
[ -f "$SUITE" ] || { echo "no suite $SUITE"; exit 2; }
"${CANARY_PYTHON:-python3}" "$HERE/telegram_canary.py" --suite "$SUITE" --output-dir "$OUT"
rc=$?
echo "rc=$rc out=$OUT"
exit $rc
