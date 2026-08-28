"""Small SQLite repository for de-duplicated listings."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .ai_classifier import AIClassification
from .market_estimator import PriceObservation
from .models import Listing


@dataclass(frozen=True, slots=True)
class CandidateState:
    status: str  # new, updated, unchanged
    previous_price: int | None = None


def _comparison_timestamp(value: str | None) -> str | None:
    """Normalize ISO timestamps to the public search response's second precision."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return parsed.replace(microsecond=0).isoformat()
    return (
        parsed.astimezone(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class ListingDatabase:
    def __init__(self, path: str | Path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row

    def __enter__(self) -> "ListingDatabase":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def initialize(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY,
                listing_id TEXT UNIQUE,
                url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                price INTEGER NOT NULL CHECK (price >= 0),
                location TEXT NOT NULL,
                source_type TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                condition_status TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (condition_status IN ('normal', 'risky', 'broken', 'unknown')),
                ai_is_computer_part INTEGER,
                ai_normalized_product_name TEXT,
                ai_confidence REAL,
                ai_reject INTEGER NOT NULL DEFAULT 0,
                ai_reason TEXT NOT NULL DEFAULT '',
                marketplace TEXT NOT NULL DEFAULT 'karrot',
                product_id TEXT,
                source_key TEXT NOT NULL DEFAULT '',
                updated_at TEXT,
                search_fingerprint TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                notified_at TEXT
            )
            """
        )
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(listings)")
        }
        if "notified_at" not in columns:
            self.connection.execute("ALTER TABLE listings ADD COLUMN notified_at TEXT")
        if "description" not in columns:
            self.connection.execute(
                "ALTER TABLE listings ADD COLUMN description TEXT NOT NULL DEFAULT ''"
            )
        if "condition_status" not in columns:
            self.connection.execute(
                "ALTER TABLE listings ADD COLUMN condition_status TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "ai_is_computer_part" not in columns:
            self.connection.execute("ALTER TABLE listings ADD COLUMN ai_is_computer_part INTEGER")
        if "ai_normalized_product_name" not in columns:
            self.connection.execute("ALTER TABLE listings ADD COLUMN ai_normalized_product_name TEXT")
        if "ai_confidence" not in columns:
            self.connection.execute("ALTER TABLE listings ADD COLUMN ai_confidence REAL")
        if "ai_reject" not in columns:
            self.connection.execute(
                "ALTER TABLE listings ADD COLUMN ai_reject INTEGER NOT NULL DEFAULT 0"
            )
        if "ai_reason" not in columns:
            self.connection.execute(
                "ALTER TABLE listings ADD COLUMN ai_reason TEXT NOT NULL DEFAULT ''"
            )
        if "marketplace" not in columns:
            self.connection.execute(
                "ALTER TABLE listings ADD COLUMN marketplace TEXT NOT NULL DEFAULT 'karrot'"
            )
        if "product_id" not in columns:
            self.connection.execute("ALTER TABLE listings ADD COLUMN product_id TEXT")
            self.connection.execute(
                "UPDATE listings SET product_id = listing_id WHERE product_id IS NULL"
            )
        if "source_key" not in columns:
            self.connection.execute(
                "ALTER TABLE listings ADD COLUMN source_key TEXT NOT NULL DEFAULT ''"
            )
        if "updated_at" not in columns:
            self.connection.execute("ALTER TABLE listings ADD COLUMN updated_at TEXT")
        if "search_fingerprint" not in columns:
            self.connection.execute(
                "ALTER TABLE listings ADD COLUMN search_fingerprint TEXT NOT NULL DEFAULT ''"
            )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_marketplace_product "
            "ON listings (marketplace, product_id)"
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS source_watermarks (
                marketplace TEXT NOT NULL,
                source_key TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (marketplace, source_key)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS price_observations (
                id INTEGER PRIMARY KEY,
                marketplace TEXT NOT NULL,
                product_id TEXT NOT NULL,
                normalized_product_name TEXT NOT NULL,
                observed_price INTEGER NOT NULL CHECK (observed_price > 0),
                observed_at TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                source_updated_at TEXT,
                listing_id TEXT,
                UNIQUE (marketplace, product_id, normalized_product_name, observed_at)
            )
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_price_observations_product_date
            ON price_observations (normalized_product_name, observed_at)
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_classifications (
                id INTEGER PRIMARY KEY,
                marketplace TEXT NOT NULL,
                product_id TEXT,
                listing_id TEXT,
                classification_fingerprint TEXT NOT NULL,
                model TEXT NOT NULL,
                reasoning_effort TEXT NOT NULL,
                classifier_version TEXT NOT NULL,
                is_computer_part INTEGER,
                normalized_product_name TEXT,
                condition_status TEXT NOT NULL
                    CHECK (condition_status IN ('normal', 'risky', 'broken', 'unknown')),
                confidence REAL,
                reject INTEGER NOT NULL,
                reason TEXT NOT NULL,
                classification_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                execution_duration_seconds REAL NOT NULL,
                success INTEGER NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ai_classifications_reuse
            ON ai_classifications (
                marketplace, product_id, classification_fingerprint,
                model, reasoning_effort, classifier_version, success, id DESC
            )
            """
        )
        self.connection.commit()

    def add(self, listing: Listing) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO listings
                (listing_id, url, title, price, location, source_type, description, condition_status,
                 ai_is_computer_part, ai_normalized_product_name, ai_confidence, ai_reject, ai_reason,
                 marketplace, product_id, source_key, updated_at, search_fingerprint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing.listing_id,
                listing.url,
                listing.title,
                listing.price,
                listing.location,
                listing.source_type,
                listing.description,
                listing.condition_status,
                listing.ai_is_computer_part,
                listing.ai_normalized_product_name,
                listing.ai_confidence,
                listing.ai_reject,
                listing.ai_reason,
                listing.marketplace,
                listing.product_id,
                listing.source_key,
                listing.updated_at,
                listing.search_fingerprint,
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0])

    def is_known(self, listing: Listing) -> bool:
        """Match a candidate by its stable marketplace ID or canonical URL."""
        if listing.listing_id is None:
            row = self.connection.execute(
                "SELECT 1 FROM listings WHERE url = ?", (listing.url,)
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT 1 FROM listings WHERE listing_id = ? OR url = ?",
                (listing.listing_id, listing.url),
            ).fetchone()
        return row is not None

    def candidate_state(self, listing: Listing) -> CandidateState:
        """Classify a cheap Bunjang search record before detail retrieval."""
        if listing.product_id is None:
            return CandidateState("new" if not self.is_known(listing) else "unchanged")
        row = self.connection.execute(
            """
            SELECT updated_at, price, search_fingerprint, ai_is_computer_part
            FROM listings WHERE marketplace = ? AND product_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (listing.marketplace, listing.product_id),
        ).fetchone()
        if row is None:
            return CandidateState("new")
        if (
            _comparison_timestamp(row["updated_at"])
            != _comparison_timestamp(listing.updated_at)
            or row["price"] != listing.price
            or row["search_fingerprint"] != listing.search_fingerprint
        ):
            return CandidateState("updated", int(row["price"]))
        if row["ai_is_computer_part"] is None:
            return CandidateState("pending_ai", int(row["price"]))
        return CandidateState("unchanged")

    def store_processed(self, listing: Listing, state: CandidateState) -> None:
        """Insert a new listing or refresh a changed listing and its notification state."""
        if state.status == "new":
            if not self.add(listing):
                raise ValueError(f"Cannot insert duplicate listing: {listing.url}")
            return
        if state.status not in {"updated", "pending_ai"} or listing.product_id is None:
            raise ValueError(f"Cannot store candidate state: {state.status}")
        cursor = self.connection.execute(
            """
            UPDATE listings SET
                listing_id = ?, url = ?, title = ?, price = ?, location = ?, source_type = ?,
                description = ?, condition_status = ?, ai_is_computer_part = ?,
                ai_normalized_product_name = ?, ai_confidence = ?, ai_reject = ?, ai_reason = ?,
                source_key = ?, updated_at = ?, search_fingerprint = ?, notified_at = NULL
            WHERE marketplace = ? AND product_id = ?
            """,
            (
                listing.listing_id, listing.url, listing.title, listing.price,
                listing.location, listing.source_type, listing.description,
                listing.condition_status, listing.ai_is_computer_part,
                listing.ai_normalized_product_name, listing.ai_confidence,
                listing.ai_reject, listing.ai_reason, listing.source_key,
                listing.updated_at, listing.search_fingerprint,
                listing.marketplace, listing.product_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Cannot update unknown product: {listing.product_id}")
        self.connection.commit()

    def record_ai_classification(
        self,
        listing: Listing,
        fingerprint: str,
        *,
        model: str,
        reasoning_effort: str,
        classifier_version: str,
        classification: AIClassification | None,
        execution_duration_seconds: float,
        error_reason: str | None = None,
    ) -> None:
        """Append every Codex result or failure for later audit and cache reuse."""
        status = classification.condition_status if classification else "unknown"
        self.connection.execute(
            """
            INSERT INTO ai_classifications
                (marketplace, product_id, listing_id, classification_fingerprint,
                 model, reasoning_effort, classifier_version, is_computer_part,
                 normalized_product_name, condition_status, confidence, reject, reason,
                 execution_duration_seconds, success)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing.marketplace, listing.product_id, listing.listing_id, fingerprint,
                model, reasoning_effort, classifier_version,
                int(classification.is_computer_part) if classification else None,
                classification.normalized_product_name if classification else None,
                status, classification.confidence if classification else None,
                int(classification.reject) if classification else 1,
                classification.reason if classification else (error_reason or "Codex CLI classification failed"),
                execution_duration_seconds, int(classification is not None),
            ),
        )
        self.connection.commit()

    def cached_ai_classification(
        self,
        listing: Listing,
        fingerprint: str,
        *,
        model: str,
        reasoning_effort: str,
        classifier_version: str,
    ) -> AIClassification | None:
        """Return a prior successful result only for unchanged classification content."""
        if not listing.product_id:
            return None
        row = self.connection.execute(
            """
            SELECT is_computer_part, normalized_product_name, condition_status, confidence, reject, reason
            FROM ai_classifications
            WHERE marketplace = ? AND product_id = ? AND classification_fingerprint = ?
              AND model = ? AND reasoning_effort = ? AND classifier_version = ? AND success = 1
            ORDER BY id DESC LIMIT 1
            """,
            (
                listing.marketplace, listing.product_id, fingerprint, model,
                reasoning_effort, classifier_version,
            ),
        ).fetchone()
        if row is None or row["is_computer_part"] is None or row["confidence"] is None:
            return None
        return AIClassification(
            bool(row["is_computer_part"]),
            str(row["normalized_product_name"]) if row["normalized_product_name"] else None,
            str(row["condition_status"]), float(row["confidence"]),
            bool(row["reject"]), str(row["reason"]),
        )

    def has_price_observation(self, marketplace: str, product_id: str, normalized_name: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM price_observations
            WHERE marketplace = ? AND product_id = ? AND normalized_product_name = ?
            """,
            (marketplace, product_id, normalized_name),
        ).fetchone()
        return row is not None

    def record_price_observation(
        self,
        listing: Listing,
        normalized_name: str,
        *,
        observed_at: str | None = None,
    ) -> bool:
        """Append a valid observed price; observations are never overwritten."""
        if not listing.product_id:
            return False
        listing_row = self.connection.execute(
            """
            SELECT first_seen_at FROM listings
            WHERE marketplace = ? AND product_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (listing.marketplace, listing.product_id),
        ).fetchone()
        if listing_row is None:
            raise ValueError(f"Cannot observe un-stored product: {listing.product_id}")
        timestamp = observed_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO price_observations
                (marketplace, product_id, normalized_product_name, observed_price,
                 observed_at, first_seen_at, source_updated_at, listing_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing.marketplace, listing.product_id, normalized_name, listing.price,
                timestamp, str(listing_row["first_seen_at"]), listing.updated_at,
                listing.listing_id,
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def price_observations(self, normalized_name: str) -> list[PriceObservation]:
        rows = self.connection.execute(
            """
            SELECT marketplace, product_id, normalized_product_name, observed_price,
                   observed_at, first_seen_at, source_updated_at
            FROM price_observations
            WHERE normalized_product_name = ?
            ORDER BY observed_at
            """,
            (normalized_name,),
        ).fetchall()
        return [
            PriceObservation(
                str(row["marketplace"]), str(row["product_id"]),
                str(row["normalized_product_name"]), int(row["observed_price"]),
                str(row["observed_at"]), str(row["first_seen_at"]),
                str(row["source_updated_at"]) if row["source_updated_at"] else None,
            )
            for row in rows
        ]

    def get_watermark(self, marketplace: str, source_key: str) -> str | None:
        row = self.connection.execute(
            "SELECT updated_at FROM source_watermarks WHERE marketplace = ? AND source_key = ?",
            (marketplace, source_key),
        ).fetchone()
        return _comparison_timestamp(str(row["updated_at"])) if row else None

    def set_watermark(self, marketplace: str, source_key: str, updated_at: str) -> None:
        normalized_updated_at = _comparison_timestamp(updated_at) or updated_at
        self.connection.execute(
            """
            INSERT INTO source_watermarks (marketplace, source_key, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(marketplace, source_key)
            DO UPDATE SET updated_at = excluded.updated_at
            """,
            (marketplace, source_key, normalized_updated_at),
        )
        self.connection.commit()

    def was_notified(self, listing: Listing) -> bool:
        row = self.connection.execute(
            "SELECT notified_at FROM listings WHERE url = ?", (listing.url,)
        ).fetchone()
        return row is not None and row["notified_at"] is not None

    def mark_notified(self, listing: Listing) -> None:
        cursor = self.connection.execute(
            "UPDATE listings SET notified_at = CURRENT_TIMESTAMP WHERE url = ?",
            (listing.url,),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Cannot mark unknown listing as notified: {listing.url}")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
