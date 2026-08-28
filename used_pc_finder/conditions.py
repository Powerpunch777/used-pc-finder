"""Conservative listing-condition classification from editable keyword rules."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence

CONDITION_STATUSES = frozenset({"normal", "risky", "broken", "unknown"})
_RULE_STATUSES = ("broken", "risky", "unknown")


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).casefold()).strip()


def _compile_rules(rules: Mapping[str, Sequence[str]], name: str) -> tuple[re.Pattern[str], ...]:
    values = rules.get(name, ())
    if not isinstance(values, Sequence) or isinstance(values, str):
        raise ValueError(f"Condition rule group {name!r} must be a list of patterns")
    try:
        return tuple(re.compile(pattern, re.IGNORECASE) for pattern in values)
    except (TypeError, re.error) as exc:
        raise ValueError(f"Invalid condition rule in {name!r}: {exc}") from exc


def classify_condition(
    title: str,
    description: str,
    rules: Mapping[str, Sequence[str]],
    *,
    description_inspected: bool = True,
) -> str:
    """Classify a listing; absent or unavailable descriptions are deliberately unknown.

    Explicit normal-language phrases are removed before risk matching so that text
    such as "고장 없음" does not become a false broken classification.  Any other
    risk wording wins, which keeps the gate conservative.
    """
    text = _normalise(f"{title}\n{description}")
    normal_patterns = _compile_rules(rules, "normal_overrides")
    risk_text = text
    for pattern in normal_patterns:
        risk_text = pattern.sub(" ", risk_text)
    for status in _RULE_STATUSES:
        if any(pattern.search(risk_text) for pattern in _compile_rules(rules, status)):
            return status
    if not description_inspected or not description.strip():
        return "unknown"
    return "normal"
