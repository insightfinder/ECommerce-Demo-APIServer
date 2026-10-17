#!/usr/bin/env bash
# Nightly connection-leak run (loadgen/leak_locustfile.py), safe to call from cron.
#
# Uses the repo's Python venv (.venv) directly, so cron's bare environment is fine.
# Every setting is defined in the "Settings" block below; values from the
# environment or the repo's .env override those defaults. Extra arguments are
# passed through to locust.
#
# crontab -e, every night at 02:00:
#   0 2 * * * /path/to/ECommerce-Demo-APIServer/loadgen/leak.sh
set -euo pipefail

REPO_DIR="${REPO_DIR:-/home/maoyuwang/Documents/Code/ECommerce-Demo-APIServer}" # checkout of this repo
cd "$REPO_DIR"

if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

# ---- Settings: edit here. Values already set in the environment or .env win. ----

# Script
VENV="${VENV:-$REPO_DIR/.venv}"                                     # venv to run locust from
APISERVER_URL="${APISERVER_URL:-http://localhost:${APP_PORT:-8000}}" # apiserver, not the UI's nginx
LOG_DIR="${LOG_DIR:-$REPO_DIR/loadgen/logs}"                        # one log file per run

# Leak profile (read by leak_locustfile.py)
export LEAK_DELAY="${LEAK_DELAY:-120}"            # seconds from the deploy event to the first leak
export LEAK_DURATION="${LEAK_DURATION:-10800}"     # seconds from the first leak to the last
export LEAK_COUNT="${LEAK_COUNT:-${POOL_MAX_SIZE:-20}}" # = POOL_MAX_SIZE ends with an exhausted pool
export MISS_ID_BASE="${MISS_ID_BASE:-10000000}"   # first product id; must be above the real catalog

# InsightFinder change events (skipped unless LICENSE_KEY, PROJECT and USER are set)
export IF_URL="${IF_URL:-https://app.insightfinder.com}"
export IF_LICENSE_KEY="${IF_LICENSE_KEY:-}"
export IF_PROJECT="${IF_PROJECT:-}"
export IF_USER="${IF_USER:-}"
export IF_INSTANCE="${IF_INSTANCE:-build-server}"

# ---------------------------------------------------------------------------------

if [[ ! -x "$VENV/bin/locust" ]]; then
    echo "locust not found in $VENV; run: $VENV/bin/pip install -r loadgen/requirements.txt" >&2
    exit 1
fi

# shellcheck disable=SC1091
. "$VENV/bin/activate"

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/leak-$(date +%Y%m%d-%H%M%S).log"

# Don't start a second run if last night's is somehow still going.
exec 9>"$LOG_DIR/.leak.lock"
if ! flock -n 9; then
    echo "$(date '+%F %T') another leak run is in progress; skipping" >>"$LOG_FILE"
    exit 0
fi

{
    echo "$(date '+%F %T') leak run: host=$APISERVER_URL count=$LEAK_COUNT python=$(command -v python)"
    # One user only: each extra user would leak at the same rate on its own.
    python -m locust -f "$REPO_DIR/loadgen/leak_locustfile.py" --host "$APISERVER_URL" \
        --headless -u 1 -r 1 --only-summary "$@" && rc=0 || rc=$?
    echo "$(date '+%F %T') leak run finished (exit $rc)"
    exit "$rc"
} >>"$LOG_FILE" 2>&1
