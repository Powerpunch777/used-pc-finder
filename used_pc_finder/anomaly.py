"""Fail-safe alert suppression that deliberately tolerates market volatility."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AnomalyDecision:
    price_warnings: int
    safety_halt: bool
    reasons: tuple[str, ...]


def assess_scan_anomalies(
    *, price_change_percents: list[float], search_records: int,
    valid_observations: int, prior_valid_observations: int,
    ai_candidates: int, ai_failures: int, parsing_anomalies: int = 0,
) -> AnomalyDecision:
    """Require severe price movement *and* independent pipeline-failure evidence.

    Price changes are warnings only: used PC parts can legitimately move quickly.
    """
    warnings = sum(abs(change) >= 30 for change in price_change_percents)
    severe = sum(abs(change) >= 60 for change in price_change_percents)
    broad_severe = severe >= 3 and severe / max(1, len(price_change_percents)) >= 0.5
    evidence: list[str] = []
    if search_records == 0:
        evidence.append("search_results_zero")
    if prior_valid_observations >= 10 and valid_observations * 2 < prior_valid_observations:
        evidence.append("valid_observations_collapsed")
    if ai_candidates >= 10 and ai_failures / ai_candidates >= 0.8:
        evidence.append("abnormal_ai_failure_distribution")
    if parsing_anomalies > 0:
        evidence.append("parsing_or_schema_anomaly")
    return AnomalyDecision(warnings, broad_severe and bool(evidence), tuple(evidence))
