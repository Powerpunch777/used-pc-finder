#!/usr/bin/env bash
# Run one locked, incremental production scan. Intended for the systemd timer
# and safe to invoke manually when a single immediate scan is needed.
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${USED_PC_FINDER_PYTHON:-$PROJECT_ROOT/.venv/bin/python}"
LOCK_FILE="${USED_PC_FINDER_LOCK_FILE:-$PROJECT_ROOT/data/production-scan.lock}"
# RuntimeDirectory can be removed between systemd activations.  Keep a stable
# project-local companion lock so a manually queued one-time pass cannot be
# split onto a replaced runtime-lock inode.
DURABLE_LOCK_FILE="${USED_PC_FINDER_DURABLE_LOCK_FILE:-$PROJECT_ROOT/data/production-scan.lock}"
LOG_FILE="${USED_PC_FINDER_SCHEDULER_LOG:-$PROJECT_ROOT/logs/production-scan.log}"

timestamp() {
    date -u +'%Y-%m-%dT%H:%M:%SZ'
}

log() {
    printf '%s | %s\n' "$(timestamp)" "$*" | tee -a "$LOG_FILE"
}

next_run() {
    local current_epoch next_epoch
    current_epoch="$(date +%s)"
    next_epoch=$(( (current_epoch / 600 + 1) * 600 ))
    date -u -d "@$next_epoch" +'%Y-%m-%dT%H:%M:%SZ'
}

mkdir -p "$(dirname "$LOCK_FILE")" "$(dirname "$DURABLE_LOCK_FILE")" "$(dirname "$LOG_FILE")"
if ! command -v flock >/dev/null 2>&1; then
    log 'SCAN_FAILED | reason=flock command is unavailable'
    exit 1
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
    log "SCAN_FAILED | reason=python executable is unavailable | path=$PYTHON_BIN"
    exit 1
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "SCAN_SKIPPED | reason=overlapping_run | next_scheduled_run=$(next_run)"
    exit 0
fi
exec 8>"$DURABLE_LOCK_FILE"
if ! flock -n 8; then
    log "SCAN_SKIPPED | reason=overlapping_durable_run | next_scheduled_run=$(next_run)"
    exit 0
fi

scan_output="$(mktemp -t used-pc-finder-scan.XXXXXX)"
trap 'rm -f "$scan_output"' EXIT
started_epoch="$(date +%s)"
backup_output="$(mktemp -t used-pc-finder-backup.XXXXXX)"
trap 'rm -f "$scan_output" "$backup_output"' EXIT
if ! "$PYTHON_BIN" "$PROJECT_ROOT/main.py" --backup-database >"$backup_output" 2>&1; then
    while IFS= read -r line || [[ -n "$line" ]]; do log "BACKUP_OUTPUT | $line"; done <"$backup_output"
    log 'SCAN_FAILED | reason=daily_backup_failed'
    exit 1
fi
while IFS= read -r line || [[ -n "$line" ]]; do log "BACKUP_OUTPUT | $line"; done <"$backup_output"
log "SCAN_START | mode=incremental_bunjang_live | command=$PYTHON_BIN main.py --live | next_scheduled_run=$(next_run)"

"$PYTHON_BIN" "$PROJECT_ROOT/main.py" --live >"$scan_output" 2>&1
exit_code=$?

while IFS= read -r line || [[ -n "$line" ]]; do
    log "SCAN_OUTPUT | $line"
done <"$scan_output"

found_count="$(grep -c '^FOUND |' "$scan_output" || true)"
qualifying_count="$(grep -c '^DEAL |' "$scan_output" || true)"
email_line="$(grep '^EMAIL |' "$scan_output" | tail -n 1 || true)"
pipeline_line="$(grep '^PIPELINE |' "$scan_output" | tail -n 1 || true)"
ai_calls="$(printf '%s' "$pipeline_line" | sed -n 's/.*ai_calls=\([0-9][0-9]*\).*/\1/p')"
ai_failures="$(printf '%s' "$pipeline_line" | sed -n 's/.*ai_failures=\([0-9][0-9]*\).*/\1/p')"
emails_sent="$(printf '%s' "$pipeline_line" | sed -n 's/.*emails_sent=\([0-9][0-9]*\).*/\1/p')"
elapsed_seconds=$(( $(date +%s) - started_epoch ))

if [[ $exit_code -eq 0 ]]; then
    log "SCAN_END | status=success | listings_found=$found_count | qualifying_candidates=$qualifying_count | ai_calls=${ai_calls:-0} | ai_failures=${ai_failures:-0} | emails_sent=${emails_sent:-0} | email_summary=${email_line:-none} | elapsed_seconds=$elapsed_seconds | next_scheduled_run=$(next_run)"
else
    failure_tail="$(tail -n 1 "$scan_output" | tr '\n' ' ')"
    log "SCAN_END | status=failure | exit_code=$exit_code | listings_found=$found_count | qualifying_candidates=$qualifying_count | ai_calls=${ai_calls:-0} | ai_failures=${ai_failures:-0} | elapsed_seconds=$elapsed_seconds | failure=${failure_tail:-unknown} | next_scheduled_run=$(next_run)"
fi

# Return the scan's status so systemd records a failed run, while the timer
# remains enabled and will activate the next interval normally.
exit "$exit_code"
