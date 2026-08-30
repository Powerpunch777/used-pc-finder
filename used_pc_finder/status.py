"""Read-only production status reporting."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

from .database import ListingDatabase


def _systemctl(args: list[str], runner: Callable[..., subprocess.CompletedProcess[str]]) -> str:
    try:
        result = runner(["systemctl", *args], text=True, capture_output=True, check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def production_status(database: ListingDatabase, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> dict[str, Any]:
    data = database.operational_status_rows()
    data["scheduler_state"] = _systemctl(["is-active", "used-pc-finder.timer"], runner)
    data["scheduler_next"] = _systemctl(
        ["show", "used-pc-finder.timer", "--property=NextElapseUSecRealtime", "--value"], runner
    )
    data["ai_queue"] = database.ai_review_queue_counts()
    return data


def format_production_status(data: dict[str, Any]) -> str:
    counts = ",".join(f"{name}={count}" for name, count in sorted(data["listing_counts"].items())) or "none"
    queue = ",".join(f"{name}={count}" for name, count in sorted(data["ai_queue"].items()))
    run = data["last_successful_scan"] or {}
    notice = data["last_notification"] or {}
    return (
        f"STATUS | scheduler={data['scheduler_state']} | next_scan={data['scheduler_next'] or 'unavailable'} | "
        f"last_successful_scan={run.get('completed_at', 'none')} | pending_ai={data['ai_queue']['pending'] + data['ai_queue']['retry']} | "
        f"ai_queue={queue} | recent_ai_failures={data['recent_ai_failures']} | "
        f"last_notification={notice.get('delivered_at', 'none')} | last_notification_count={notice.get('listing_count', 0)} | "
        f"listing_counts={counts} | recent_pricing_update={data['recent_pricing_update'] or 'none'}"
    )
