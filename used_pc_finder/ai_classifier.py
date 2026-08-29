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
_SCOPES = frozenset({"standalone", "bundle", "complete_pc", "accessory", "unknown"})
CLASSIFIER_VERSION = "codex-cli-listing-v2"


@dataclass(frozen=True, slots=True)
class AIClassification:
    is_computer_part: bool
    normalized_product_name: str | None
    condition_status: str
    confidence: float
    reject: bool
    reason: str
    scope: str = "unknown"


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

Accept only a genuine, complete computer part. Reject accessory-only, box-only,
parts-only, repair-needed, incomplete, untested, ambiguous, and unrelated
listings. Use condition_status normal, risky, broken, or unknown. Set reject true
unless the listing is a complete, working computer part with a clear identity.
For normalized_product_name, return a concise canonical product model suitable for
price matching (for example, \"RTX 4070 SUPER\"), or null if uncertain.

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
    if not isinstance(raw, dict) or set(raw) != {
        "is_computer_part",
        "normalized_product_name",
        "condition_status",
        "confidence",
        "reject",
        "reason",
        "scope",
    }:
        raise ValueError("classification does not match the required keys")
    normalized = raw["normalized_product_name"]
    confidence = raw["confidence"]
    if (
        not isinstance(raw["is_computer_part"], bool)
        or not (isinstance(normalized, str) or normalized is None)
        or raw["condition_status"] not in _STATUSES
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
        or not isinstance(raw["reject"], bool)
        or not isinstance(raw["reason"], str)
        or raw["scope"] not in _SCOPES
    ):
        raise ValueError("classification has invalid field values")
    return AIClassification(
        raw["is_computer_part"],
        normalized.strip() if isinstance(normalized, str) else None,
        raw["condition_status"],
        float(confidence),
        raw["reject"],
        raw["reason"].strip(),
        raw["scope"],
    )
