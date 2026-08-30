#!/usr/bin/env bash
# Install the system-wide service and timer for the current repository user.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo 'Run with sudo: sudo ./scripts/install-systemd-scheduler.sh' >&2
    exit 1
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-${USED_PC_FINDER_RUN_USER:-}}"
if [[ -z "$RUN_USER" || "$RUN_USER" == 'root' ]]; then
    echo 'Set USED_PC_FINDER_RUN_USER to the non-root account that owns this repository.' >&2
    exit 1
fi
RUN_GROUP="$(id -gn "$RUN_USER")"
SERVICE_TEMPLATE="$PROJECT_ROOT/deploy/systemd/used-pc-finder.service.template"
TIMER_TEMPLATE="$PROJECT_ROOT/deploy/systemd/used-pc-finder.timer"

for required_file in "$SERVICE_TEMPLATE" "$TIMER_TEMPLATE" "$PROJECT_ROOT/scripts/run-production-scan.sh" "$PROJECT_ROOT/.venv/bin/python"; do
    [[ -e "$required_file" ]] || { echo "Missing required file: $required_file" >&2; exit 1; }
done

install -d -m 0755 /etc/systemd/system
sed \
    -e "s|@PROJECT_ROOT@|$PROJECT_ROOT|g" \
    -e "s|@RUN_USER@|$RUN_USER|g" \
    -e "s|@RUN_GROUP@|$RUN_GROUP|g" \
    "$SERVICE_TEMPLATE" > /etc/systemd/system/used-pc-finder.service
install -m 0644 "$TIMER_TEMPLATE" /etc/systemd/system/used-pc-finder.timer
chmod 0755 "$PROJECT_ROOT/scripts/run-production-scan.sh"

systemctl daemon-reload
echo 'Installed used-pc-finder.service and used-pc-finder.timer.'
echo 'Enable it with: sudo systemctl enable --now used-pc-finder.timer'
