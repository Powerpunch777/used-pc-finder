"""Load user-editable JSON settings."""

import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
DEFAULT_MARKET_PRICES_PATH = PROJECT_ROOT / "data" / "market_prices.json"
DEFAULT_CONDITION_RULES_PATH = PROJECT_ROOT / "config" / "condition_rules.json"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "listings.sqlite3"


def load_json(path: str | Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_settings(path: str | Path = DEFAULT_SETTINGS_PATH) -> dict[str, Any]:
    settings = load_json(path)
    required = {
        "locations", "minimum_discount_percent", "request_delay_seconds",
        "maximum_listing_price", "market_price_estimation",
    }
    missing = required.difference(settings)
    if missing:
        raise ValueError(f"Missing settings: {', '.join(sorted(missing))}")
    if settings["request_delay_seconds"] < 0:
        raise ValueError("request_delay_seconds must not be negative")
    if int(settings["maximum_listing_price"]) <= 0:
        raise ValueError("maximum_listing_price must be positive")
    estimation = settings["market_price_estimation"]
    if not isinstance(estimation, dict):
        raise ValueError("market_price_estimation must be an object")
    required_estimation = {"window_days", "half_life_days", "minimum_observations", "estimator"}
    if missing_estimation := required_estimation.difference(estimation):
        raise ValueError(
            "Missing market-price estimation settings: "
            f"{', '.join(sorted(missing_estimation))}"
        )
    if int(estimation["window_days"]) < 1 or float(estimation["half_life_days"]) <= 0:
        raise ValueError("market-price window and half-life must be positive")
    if int(estimation["minimum_observations"]) < 1:
        raise ValueError("market-price minimum_observations must be positive")
    if estimation["estimator"] not in {"weighted_median", "weighted_mean"}:
        raise ValueError("market-price estimator must be weighted_median or weighted_mean")
    if settings.get("active_marketplace") != "bunjang":
        raise ValueError("active_marketplace must be bunjang")
    tracking = settings.get("sale_status_tracking")
    if not isinstance(tracking, dict):
        raise ValueError("sale_status_tracking must be an object")
    required_tracking = {
        "recent_age_days", "medium_age_days", "recent_interval_hours",
        "medium_interval_hours", "older_interval_hours",
    }
    if missing_tracking := required_tracking.difference(tracking):
        raise ValueError(
            "Missing sale_status_tracking settings: "
            f"{', '.join(sorted(missing_tracking))}"
        )
    if (
        float(tracking["recent_age_days"]) < 0
        or float(tracking["medium_age_days"]) < float(tracking["recent_age_days"])
        or any(float(tracking[key]) <= 0 for key in required_tracking if key.endswith("_hours"))
    ):
        raise ValueError("sale_status_tracking ages and intervals must be ordered and positive")
    sources = settings.get("bunjang_sources", [])
    if not isinstance(sources, list) or not sources:
        raise ValueError("bunjang_sources must be a non-empty list")
    source_keys: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or not str(source.get("key", "")).strip() or not str(source.get("query", "")).strip():
            raise ValueError("Each Bunjang source requires key and query")
        key = str(source["key"])
        if key in source_keys:
            raise ValueError("Bunjang source keys must be unique")
        source_keys.add(key)
        if int(source.get("max_pages", 2)) < 1:
            raise ValueError("Bunjang max_pages must be at least 1")
        if int(source.get("watermark_overlap_pages", 1)) < 1:
            raise ValueError("Bunjang watermark_overlap_pages must be at least 1")
    ai_settings = settings.get("ai_classification", {"enabled": False})
    if not isinstance(ai_settings, dict):
        raise ValueError("ai_classification must be an object")
    if ai_settings.get("enabled", False):
        required_ai_settings = {
            "command",
            "model",
            "reasoning_effort",
            "timeout_seconds",
            "confidence_threshold",
            "ai_concurrency",
            "schema_path",
        }
        missing_ai_settings = required_ai_settings.difference(ai_settings)
        if missing_ai_settings:
            raise ValueError(
                "Missing AI classification settings: "
                f"{', '.join(sorted(missing_ai_settings))}"
            )
        if float(ai_settings["timeout_seconds"]) <= 0:
            raise ValueError("AI classification timeout_seconds must be positive")
        if not 0 <= float(ai_settings["confidence_threshold"]) <= 1:
            raise ValueError("AI classification confidence_threshold must be from 0 to 1")
        if int(ai_settings["ai_concurrency"]) < 1:
            raise ValueError("AI classification ai_concurrency must be positive")
    email_settings = settings.get("email_notifications", {"enabled": False})
    if not isinstance(email_settings, dict):
        raise ValueError("email_notifications must be an object")
    if email_settings.get("enabled", False):
        required_email_settings = {
            "recipient_address",
            "smtp_host",
            "smtp_port",
            "sender_address",
        }
        missing_email_settings = required_email_settings.difference(email_settings)
        if missing_email_settings:
            raise ValueError(
                "Missing email notification settings: "
                f"{', '.join(sorted(missing_email_settings))}"
            )
        if not all(str(email_settings[key]).strip() for key in required_email_settings - {"smtp_port"}):
            raise ValueError("Email notification addresses and SMTP host must not be empty")
        if not 1 <= int(email_settings["smtp_port"]) <= 65535:
            raise ValueError("smtp_port must be between 1 and 65535")
    return settings


def load_market_prices(
    path: str | Path = DEFAULT_MARKET_PRICES_PATH,
) -> dict[str, int]:
    raw = load_json(path)
    prices = {str(name): int(price) for name, price in raw.items()}
    if any(price <= 0 for price in prices.values()):
        raise ValueError("Market prices must be positive integers")
    return prices


def load_condition_rules(
    path: str | Path = DEFAULT_CONDITION_RULES_PATH,
) -> dict[str, list[str]]:
    """Load editable condition-language rules used before deal evaluation."""
    raw = load_json(path)
    required_groups = {"normal_overrides", "broken", "risky", "unknown"}
    if not isinstance(raw, dict) or required_groups.difference(raw):
        raise ValueError("Condition rules must define normal_overrides, broken, risky, and unknown")
    rules: dict[str, list[str]] = {}
    for group in required_groups:
        values = raw[group]
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"Condition rule group {group!r} must be a list of strings")
        rules[group] = values
    return rules
