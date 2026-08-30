"""Robust, time-decayed market-price estimation from stored observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from typing import Iterable, Literal


@dataclass(frozen=True, slots=True)
class PriceObservation:
    marketplace: str
    product_id: str
    normalized_product_name: str
    observed_price: int
    observed_at: str
    first_seen_at: str
    source_updated_at: str | None


@dataclass(frozen=True, slots=True)
class MarketPriceEstimate:
    normalized_product_name: str
    price: int | None
    valid_observation_count: int
    estimator: str
    automatic: bool
    oldest_observed_at: str | None
    newest_observed_at: str | None


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _recent_and_non_outlying(
    observations: Iterable[PriceObservation],
    *,
    now: datetime,
    window_days: int,
) -> list[tuple[PriceObservation, float]]:
    recent: list[tuple[PriceObservation, float]] = []
    for observation in observations:
        try:
            age_days = max(0.0, (now - _parse_timestamp(observation.observed_at)).total_seconds() / 86400)
        except ValueError:
            continue
        if age_days <= window_days and observation.observed_price > 0:
            recent.append((observation, age_days))
    if len(recent) < 3:
        return recent

    prices = [observation.observed_price for observation, _age in recent]
    center = median(prices)
    mad = median([abs(price - center) for price in prices])
    if mad == 0:
        # A repeated merchant/stock price can make MAD zero even when other
        # genuine market listings differ.  Keeping the recent sample avoids
        # silently discarding every non-modal observation (as happened for
        # Ryzen 5 7500F).  The weighted median remains robust to a lone extreme.
        return recent
    scale = 1.4826 * mad
    return [
        (observation, age)
        for observation, age in recent
        if abs(observation.observed_price - center) / scale <= 3.5
    ]


def _weighted_median(values: list[tuple[int, float]]) -> int:
    ordered = sorted(values)
    midpoint = sum(weight for _price, weight in ordered) / 2
    cumulative = 0.0
    for price, weight in ordered:
        cumulative += weight
        if cumulative >= midpoint:
            return price
    return ordered[-1][0]


def estimation_observations(
    observations: Iterable[PriceObservation], *, now: datetime, window_days: int
) -> list[PriceObservation]:
    """Return recent, non-outlying observations used by either estimator."""
    return [
        observation
        for observation, _age in _recent_and_non_outlying(
            observations, now=now, window_days=window_days
        )
    ]


def estimate_market_price(
    normalized_product_name: str,
    observations: Iterable[PriceObservation],
    *,
    manual_reference_price: int | None,
    window_days: int = 90,
    half_life_days: float = 21,
    minimum_observations: int = 5,
    estimator: Literal["weighted_median", "weighted_mean"] = "weighted_median",
    now: datetime | None = None,
) -> MarketPriceEstimate:
    """Estimate a current price, otherwise return the manual reference fallback."""
    if window_days < 1 or half_life_days <= 0 or minimum_observations < 1:
        raise ValueError("market-price estimation settings must be positive")
    if estimator not in {"weighted_median", "weighted_mean"}:
        raise ValueError("estimator must be weighted_median or weighted_mean")
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)
    valid = _recent_and_non_outlying(observations, now=current_time, window_days=window_days)
    dates = sorted(observation.observed_at for observation, _age in valid)
    if len(valid) < minimum_observations:
        return MarketPriceEstimate(
            normalized_product_name,
            manual_reference_price,
            len(valid),
            "manual_fallback",
            False,
            dates[0] if dates else None,
            dates[-1] if dates else None,
        )

    weighted = [
        (observation.observed_price, 0.5 ** (age_days / half_life_days))
        for observation, age_days in valid
    ]
    if estimator == "weighted_mean":
        price = round(sum(value * weight for value, weight in weighted) / sum(weight for _value, weight in weighted))
    else:
        price = _weighted_median(weighted)
    return MarketPriceEstimate(
        normalized_product_name,
        price,
        len(valid),
        estimator,
        True,
        dates[0],
        dates[-1],
    )
