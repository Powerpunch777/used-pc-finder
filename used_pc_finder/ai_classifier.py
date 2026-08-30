"""Fail-closed Codex CLI classification of crawler-collected listing text."""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .models import Listing

LOGGER = logging.getLogger(__name__)
_STATUSES = frozenset({"normal", "risky", "broken", "unknown"})
_SALE_STATUSES = frozenset({"active", "reserved", "sold", "unavailable", "unknown"})
_SCOPES = frozenset({"standalone", "bundle", "complete_pc", "accessory", "unknown"})
CLASSIFIER_VERSION = "codex-cli-listing-v6-pricing-semantics"


@dataclass(frozen=True, slots=True)
class AIClassification:
    # is_computer_part remains an internal compatibility field.  The model's
    # schema deliberately no longer asks it a vague duplicate question.
    is_computer_part: bool
    normalized_product_name: str | None
    condition_status: str
    confidence: float
    # Legacy storage compatibility only. These are derived by application code,
    # never requested from or trusted as an AI decision.
    reject: bool
    reason: str
    scope: str = "unknown"
    sale_status: str = "active"
    usable_for_market_price: bool = True
    exact_product: bool = False
    listing_intent: str = "unknown"
    model_mismatch: bool = False
    price_bait: bool = False
    displayed_price: int | None = None
    effective_price: int | None = None
    price_source: str = "unknown"
    price_confidence: float = 0.0
    usable_price: bool = False
    hidden_price_condition: bool = False


@dataclass(frozen=True, slots=True)
class ClassificationAttempt:
    classification: AIClassification | None
    execution_seconds: float
    error: str | None


def _prompt(listing: Listing) -> str:
    return f"""You classify one second-hand listing using only the data below.
Do not use tools, commands, web access, browser automation, files, or external
knowledge lookup. Do not browse any marketplace. Return only the JSON required by the
provided schema.

Classify identity, scope, condition, sale status, and pricing together.
For normalized_product_name return a concise canonical model or null. exact_product
is true only when the exact model is clearly stated. model_mismatch is true for any
conflict between the offered item and its claimed model.

Interpret pricing from the title, full description, displayed marketplace price, and
metadata together. Set price_bait true only for a clearly deceptive or placeholder
price: contact-only/attention price, deposit, deliberately incorrect displayed price,
or description text explicitly replacing the marketplace price. Do not set price_bait
true merely because a listing has multiple products, numbered items, or multiple
prices. For multiple items, map the exact target to its one-to-one price when the text
does so unambiguously (for example, “980 PRO 2TB No. 2: 340,000 KRW”); then set
usable_price true and effective_price to that amount even if other items are offered.
Set hidden_price_condition true and usable_price false only when the target price is
actually ambiguous, conditional, negotiable without a fixed amount, bundle-dependent,
or cannot be reliably mapped to the exact target. Set displayed_price to the fetched
marketplace price and effective_price only when that target-specific amount is known.
price_source must be marketplace, description, both, or unknown. Do not decide whether
to email or reject a listing; return factual verification only.

Classify scope from title and description: standalone only for one sellable computer
part by itself; bundle for CPU+motherboard/RAM or other multi-part or ambiguous
offers; complete_pc for a complete desktop/system; accessory for box-only, cable,
cooler, adapter, or accessory-only offers; and unknown when scope is unclear. A
standalone part may include its original box or stock cooler, but a box-only offer
is an accessory.

Listing title: {listing.title}
Listing description: {listing.description}
Listing price (KRW): {listing.price}
Listing location: {listing.location}
Listing source: {listing.source_type}
Listing marketplace: {listing.marketplace}
Listing product id: {listing.product_id or "unknown"}
Listing public status: {listing.listing_status}
"""


def classification_fingerprint(listing: Listing) -> str:
    """Fingerprint only data that can affect a classification decision."""
    import hashlib

    payload = {
        "title": listing.title,
        "description": listing.description,
        "price": listing.price,
        "updated_at": listing.updated_at,
        "source_type": listing.source_type,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class CodexCliClassifier:
    """Use authenticated ``codex exec`` without an API key or crawler access."""

    def __init__(
        self,
        schema_path: str | Path,
        *,
        command: str = "codex",
        model: str = "gpt-5.6-luna",
        reasoning_effort: str = "low",
        timeout_seconds: float = 45.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        self.schema_path = str(Path(schema_path).resolve())
        self.command = command
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        self.calls = 0

    def classify(self, listing: Listing) -> AIClassification | None:
        """Return a validated result, or None on any CLI/output failure."""
        return self.classify_attempt(listing).classification

    def classify_attempt(self, listing: Listing) -> ClassificationAttempt:
        """Run Codex once and retain timing/error details for audit logging."""
        self.calls += 1
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="used-pc-codex-") as directory:
            output_path = Path(directory) / "classification.json"
            args = [
                self.command,
                "--ask-for-approval",
                "never",
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--model",
                self.model,
                "-c",
                f'model_reasoning_effort="{self.reasoning_effort}"',
                "--output-schema",
                self.schema_path,
                "--output-last-message",
                str(output_path),
                _prompt(listing),
            ]
            try:
                completed = self.runner(
                    args,
                    cwd=directory,
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                LOGGER.warning("Codex CLI classification failed: %s", exc)
                return ClassificationAttempt(None, time.monotonic() - started, str(exc))
            if completed.returncode != 0:
                error = completed.stderr.strip() or f"Codex CLI exited {completed.returncode}"
                LOGGER.warning(
                    "Codex CLI classification exited %s: %s",
                    completed.returncode,
                    error,
                )
                return ClassificationAttempt(None, time.monotonic() - started, error)
            try:
                raw = json.loads(output_path.read_text(encoding="utf-8"))
                return ClassificationAttempt(
                    _parse_result(raw), time.monotonic() - started, None
                )
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                LOGGER.warning("Codex CLI returned invalid classification JSON: %s", exc)
                return ClassificationAttempt(None, time.monotonic() - started, str(exc))


def _parse_result(raw: Any) -> AIClassification:
    expected = {
        "normalized_product_name", "exact_product", "condition", "confidence",
        "reason", "scope", "sale_status",
        "model_mismatch", "price_bait", "displayed_price", "effective_price",
        "price_source", "price_confidence", "usable_price", "hidden_price_condition",
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        raise ValueError("classification does not match the required keys")
    normalized = raw["normalized_product_name"]
    confidence = raw["confidence"]
    price_confidence = raw["price_confidence"]
    displayed_price = raw["displayed_price"]
    effective_price = raw["effective_price"]
    if (
        not (isinstance(normalized, str) or normalized is None)
        or not isinstance(raw["exact_product"], bool)
        or raw["condition"] not in _STATUSES
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
        or not isinstance(raw["reason"], str)
        or raw["scope"] not in _SCOPES
        or raw["sale_status"] not in _SALE_STATUSES
        or not isinstance(raw["model_mismatch"], bool)
        or not isinstance(raw["price_bait"], bool)
        or isinstance(displayed_price, bool) or not isinstance(displayed_price, int) or displayed_price <= 0
        or not (isinstance(effective_price, int) or effective_price is None)
        or isinstance(effective_price, bool)
        or (effective_price is not None and effective_price <= 0)
        or raw["price_source"] not in {"marketplace", "description", "both", "unknown"}
        or isinstance(price_confidence, bool) or not isinstance(price_confidence, (int, float)) or not 0 <= price_confidence <= 1
        or not isinstance(raw["usable_price"], bool)
        or not isinstance(raw["hidden_price_condition"], bool)
    ):
        raise ValueError("classification has invalid field values")
    return AIClassification(
        raw["scope"] == "standalone" and normalized is not None,
        normalized.strip() if isinstance(normalized, str) else None,
        raw["condition"],
        float(confidence),
        False,
        raw["reason"].strip(),
        raw["scope"],
        raw["sale_status"],
        False,
        raw["exact_product"], "unknown", raw["model_mismatch"],
        raw["price_bait"], displayed_price, effective_price, raw["price_source"],
        float(price_confidence), raw["usable_price"], raw["hidden_price_condition"],
    )
