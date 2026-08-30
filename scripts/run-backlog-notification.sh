#!/usr/bin/env bash
# One manually-invoked backlog digest pass.  It deliberately shares the
# production scanner's flock, so the ten-minute timer is never overlapped.
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${USED_PC_FINDER_PYTHON:-$PROJECT_ROOT/.venv/bin/python}"
LOCK_FILE="${USED_PC_FINDER_LOCK_FILE:-$PROJECT_ROOT/data/production-scan.lock}"
DURABLE_LOCK_FILE="${USED_PC_FINDER_DURABLE_LOCK_FILE:-$PROJECT_ROOT/data/production-scan.lock}"
LOG_FILE="${USED_PC_FINDER_SCHEDULER_LOG:-$PROJECT_ROOT/logs/production-scan.log}"

timestamp() { date -u +'%Y-%m-%dT%H:%M:%SZ'; }
log() { printf '%s | %s\n' "$(timestamp)" "$*" | tee -a "$LOG_FILE"; }

mkdir -p "$(dirname "$LOCK_FILE")" "$(dirname "$DURABLE_LOCK_FILE")" "$(dirname "$LOG_FILE")"
if ! command -v flock >/dev/null 2>&1 || [[ ! -x "$PYTHON_BIN" ]]; then
    log 'BACKLOG_NOTIFICATION_FAILED | reason=runner_prerequisite_unavailable'
    exit 1
fi

exec 8>"$DURABLE_LOCK_FILE"
if [[ "${USED_PC_FINDER_WAIT_FOR_LOCK:-0}" == "1" ]]; then
    log 'BACKLOG_NOTIFICATION_WAITING | reason=overlapping_production_scan'
    flock 8
elif ! flock -n 8; then
    log 'BACKLOG_NOTIFICATION_SKIPPED | reason=overlapping_production_scan'
    exit 0
fi
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log 'BACKLOG_NOTIFICATION_SKIPPED | reason=overlapping_runtime_scan'
    exit 0
fi

output="$(mktemp -t used-pc-finder-backlog.XXXXXX)"
trap 'rm -f "$output"' EXIT
log "BACKLOG_NOTIFICATION_START | command=$PYTHON_BIN main.py --backlog-notify"
"$PYTHON_BIN" "$PROJECT_ROOT/main.py" --backlog-notify >"$output" 2>&1
exit_code=$?
while IFS= read -r line || [[ -n "$line" ]]; do
    log "BACKLOG_NOTIFICATION_OUTPUT | $line"
done <"$output"
if [[ $exit_code -eq 0 ]]; then
    log 'BACKLOG_NOTIFICATION_END | status=success'
else
    log "BACKLOG_NOTIFICATION_END | status=failure | exit_code=$exit_code"
fi
exit "$exit_code"
