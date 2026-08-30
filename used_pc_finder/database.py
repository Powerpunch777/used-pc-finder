"""Small SQLite repository for de-duplicated listings."""

import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .ai_classifier import AIClassification, CLASSIFIER_VERSION
from .bunjang import detail_error_diagnostics, is_transient_detail_error
from .market_estimator import PriceObservation
from .models import Listing
from .parser import exact_model_match, is_pricing_identity


_LISTING_STATUSES = frozenset({"active", "reserved", "sold", "unavailable"})
_LISTING_SCOPES = frozenset({"standalone", "bundle", "complete_pc", "accessory", "unknown"})
_COMPLETE_PC_TERMS = re.compile(r"(?i)(?:본체|완본체|데스크탑|desktop|complete\s*pc)")
_BUNDLE_TERMS = re.compile(
    r"(?i)(?:세트|set\b|bundle|묶음|일괄|보드셋|"
    r"(?:cpu|라이젠|ryzen).{0,30}\+.{0,30}(?:램|ram|ddr|보드|board))"
)
_BOX_ONLY_TERMS = re.compile(r"(?i)(?:box\s*only|empty\s*box|박스\s*(?:만|only)|상자\s*만)")


def _historical_scope(title: str, description: str) -> str:
    """Conservatively label existing evidence where no AI scope was stored."""
    text = f"{title}\n{description}"
    if _BOX_ONLY_TERMS.search(text):
        return "accessory"
    if _COMPLETE_PC_TERMS.search(text):
        return "complete_pc"
    if _BUNDLE_TERMS.search(text):
        return "bundle"
    return "standalone"


def _observation_name_mismatch(normalized_name: str, title: str, description: str) -> bool:
    """Reject legacy evidence that is not textually tied to its exact model."""
    return not is_pricing_identity(normalized_name) or not exact_model_match(
        normalized_name, title, description
    )


@dataclass(frozen=True, slots=True)
class CandidateState:
    status: str  # new, updated, unchanged
    previous_price: int | None = None


@dataclass(frozen=True, slots=True)
class SaleStatusCandidate:
    listing: Listing
    age_days: float
    interval_hours: float


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
                canonical_url TEXT,
                listing_status TEXT NOT NULL DEFAULT 'active'
                    CHECK (listing_status IN ('active', 'reserved', 'sold', 'unavailable')),
                ai_scope TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (ai_scope IN ('standalone', 'bundle', 'complete_pc', 'accessory', 'unknown')),
                ai_sale_status TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (ai_sale_status IN ('active', 'reserved', 'sold', 'unavailable', 'unknown')),
                ai_usable_for_market_price INTEGER NOT NULL DEFAULT 0,
                ai_effective_price INTEGER CHECK (ai_effective_price IS NULL OR ai_effective_price > 0),
                ai_usable_price INTEGER NOT NULL DEFAULT 0,
                last_active_at TEXT,
                first_sold_seen_at TEXT,
                last_active_price INTEGER CHECK (last_active_price IS NULL OR last_active_price >= 0),
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
        if "canonical_url" not in columns:
            self.connection.execute("ALTER TABLE listings ADD COLUMN canonical_url TEXT")
        if "listing_status" not in columns:
            self.connection.execute(
                "ALTER TABLE listings ADD COLUMN listing_status TEXT NOT NULL DEFAULT 'active'"
            )
        if "ai_scope" not in columns:
            self.connection.execute(
                "ALTER TABLE listings ADD COLUMN ai_scope TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "ai_sale_status" not in columns:
            self.connection.execute(
                "ALTER TABLE listings ADD COLUMN ai_sale_status TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "ai_usable_for_market_price" not in columns:
            self.connection.execute(
                "ALTER TABLE listings ADD COLUMN ai_usable_for_market_price INTEGER NOT NULL DEFAULT 0"
            )
        if "ai_effective_price" not in columns:
            self.connection.execute("ALTER TABLE listings ADD COLUMN ai_effective_price INTEGER")
            self.connection.execute(
                "UPDATE listings SET ai_effective_price = price WHERE ai_usable_for_market_price = 1"
            )
        if "ai_usable_price" not in columns:
            self.connection.execute(
                "ALTER TABLE listings ADD COLUMN ai_usable_price INTEGER NOT NULL DEFAULT 0"
            )
            self.connection.execute(
                "UPDATE listings SET ai_usable_price = ai_usable_for_market_price"
            )
        if "last_active_at" not in columns:
            self.connection.execute("ALTER TABLE listings ADD COLUMN last_active_at TEXT")
        if "first_sold_seen_at" not in columns:
            self.connection.execute("ALTER TABLE listings ADD COLUMN first_sold_seen_at TEXT")
        if "last_active_price" not in columns:
            self.connection.execute("ALTER TABLE listings ADD COLUMN last_active_price INTEGER")
        self.connection.execute(
            """
            UPDATE listings
            SET last_active_at = COALESCE(last_active_at, first_seen_at),
                last_active_price = COALESCE(last_active_price, price)
            WHERE listing_status = 'active'
            """
        )
        self.connection.execute(
            """
            UPDATE listings
            SET canonical_url = url
            WHERE marketplace = 'bunjang'
              AND (canonical_url IS NULL OR canonical_url = '')
            """
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
                valid INTEGER NOT NULL DEFAULT 1,
                invalid_reason TEXT,
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
        observation_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(price_observations)")
        }
        if "valid" not in observation_columns:
            self.connection.execute(
                "ALTER TABLE price_observations ADD COLUMN valid INTEGER NOT NULL DEFAULT 1"
            )
        if "invalid_reason" not in observation_columns:
            self.connection.execute("ALTER TABLE price_observations ADD COLUMN invalid_reason TEXT")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS market_price_history (
                id INTEGER PRIMARY KEY,
                marketplace TEXT NOT NULL,
                product_id TEXT NOT NULL,
                normalized_product_name TEXT NOT NULL,
                observed_price INTEGER NOT NULL CHECK (observed_price > 0),
                observed_at TEXT NOT NULL,
                source_updated_at TEXT,
                price_observation_id INTEGER,
                UNIQUE (marketplace, product_id, normalized_product_name, observed_at)
            )
            """
        )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO market_price_history
                (marketplace, product_id, normalized_product_name, observed_price,
                 observed_at, source_updated_at, price_observation_id)
            SELECT marketplace, product_id, normalized_product_name, observed_price,
                   observed_at, source_updated_at, id
            FROM price_observations
            WHERE valid = 1
            """
        )
        # History is derived from eligible evidence; never reintroduce an
        # observation invalidated by the market-price repair pass.
        self.connection.execute(
            """
            DELETE FROM market_price_history
            WHERE price_observation_id IN (
                SELECT id FROM price_observations WHERE valid = 0
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS market_price_backfill_checkpoints (
                query_key TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                cursor TEXT,
                pages_scanned INTEGER NOT NULL DEFAULT 0,
                unique_listings_found INTEGER NOT NULL DEFAULT 0,
                valid_observations INTEGER NOT NULL DEFAULT 0,
                excluded_listings INTEGER NOT NULL DEFAULT 0,
                ai_calls INTEGER NOT NULL DEFAULT 0,
                ai_failures INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS market_price_backfill_seen_products (
                marketplace TEXT NOT NULL,
                product_id TEXT NOT NULL,
                first_query_key TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (marketplace, product_id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS market_price_repair_checkpoints (
                repair_key TEXT PRIMARY KEY,
                last_listing_id INTEGER NOT NULL DEFAULT 0,
                listings_reviewed INTEGER NOT NULL DEFAULT 0,
                deterministic_accepts INTEGER NOT NULL DEFAULT 0,
                deterministic_rejects INTEGER NOT NULL DEFAULT 0,
                ai_calls INTEGER NOT NULL DEFAULT 0,
                ai_failures INTEGER NOT NULL DEFAULT 0,
                observations_rebuilt INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS market_price_backfill_detail_retries (
                marketplace TEXT NOT NULL,
                product_id TEXT NOT NULL,
                query_key TEXT NOT NULL,
                listing_id TEXT,
                title TEXT NOT NULL,
                price INTEGER NOT NULL CHECK (price >= 0),
                url TEXT NOT NULL,
                location TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_key TEXT NOT NULL DEFAULT '',
                updated_at TEXT,
                search_fingerprint TEXT NOT NULL DEFAULT '',
                canonical_url TEXT,
                listing_status TEXT NOT NULL DEFAULT 'active'
                    CHECK (listing_status IN ('active', 'reserved', 'sold', 'unavailable')),
                attempts INTEGER NOT NULL DEFAULT 1,
                last_error TEXT NOT NULL DEFAULT '',
                http_status INTEGER,
                exception_type TEXT NOT NULL DEFAULT '',
                error_category TEXT NOT NULL DEFAULT 'unknown',
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_error_message TEXT NOT NULL DEFAULT '',
                last_error_at TEXT,
                is_terminal INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at_retry TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (marketplace, product_id)
            )
            """
        )
        retry_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(market_price_backfill_detail_retries)"
            )
        }
        # SQLite ALTER TABLE only adds columns, so existing retry rows and their
        # primary keys remain untouched during this diagnostics migration.
        if "http_status" not in retry_columns:
            self.connection.execute(
                "ALTER TABLE market_price_backfill_detail_retries ADD COLUMN http_status INTEGER"
            )
        if "exception_type" not in retry_columns:
            self.connection.execute(
                "ALTER TABLE market_price_backfill_detail_retries "
                "ADD COLUMN exception_type TEXT NOT NULL DEFAULT ''"
            )
        if "error_category" not in retry_columns:
            self.connection.execute(
                "ALTER TABLE market_price_backfill_detail_retries "
                "ADD COLUMN error_category TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "retry_count" not in retry_columns:
            self.connection.execute(
                "ALTER TABLE market_price_backfill_detail_retries "
                "ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
            )
            self.connection.execute(
                "UPDATE market_price_backfill_detail_retries "
                "SET retry_count = MAX(attempts - 1, 0)"
            )
        if "last_error_message" not in retry_columns:
            self.connection.execute(
                "ALTER TABLE market_price_backfill_detail_retries "
                "ADD COLUMN last_error_message TEXT NOT NULL DEFAULT ''"
            )
            self.connection.execute(
                "UPDATE market_price_backfill_detail_retries "
                "SET last_error_message = last_error"
            )
        if "last_error_at" not in retry_columns:
            self.connection.execute(
                "ALTER TABLE market_price_backfill_detail_retries ADD COLUMN last_error_at TEXT"
            )
            self.connection.execute(
                "UPDATE market_price_backfill_detail_retries "
                "SET last_error_at = updated_at_retry"
            )
        if "is_terminal" not in retry_columns:
            self.connection.execute(
                "ALTER TABLE market_price_backfill_detail_retries "
                "ADD COLUMN is_terminal INTEGER NOT NULL DEFAULT 0"
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
                scope TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (scope IN ('standalone', 'bundle', 'complete_pc', 'accessory', 'unknown')),
                sale_status TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (sale_status IN ('active', 'reserved', 'sold', 'unavailable', 'unknown')),
                usable_for_market_price INTEGER NOT NULL DEFAULT 0,
                exact_product INTEGER NOT NULL DEFAULT 0,
                listing_intent TEXT NOT NULL DEFAULT 'unknown',
                model_mismatch INTEGER NOT NULL DEFAULT 0,
                price_bait INTEGER NOT NULL DEFAULT 0,
                hidden_price_condition INTEGER NOT NULL DEFAULT 0,
                displayed_price INTEGER,
                effective_price INTEGER,
                price_source TEXT NOT NULL DEFAULT 'unknown',
                price_confidence REAL,
                usable_price INTEGER NOT NULL DEFAULT 0,
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
        ai_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(ai_classifications)")
        }
        if "scope" not in ai_columns:
            self.connection.execute(
                "ALTER TABLE ai_classifications ADD COLUMN scope TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "sale_status" not in ai_columns:
            self.connection.execute(
                "ALTER TABLE ai_classifications ADD COLUMN sale_status TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "usable_for_market_price" not in ai_columns:
            self.connection.execute(
                "ALTER TABLE ai_classifications ADD COLUMN usable_for_market_price INTEGER NOT NULL DEFAULT 0"
            )
        for column, definition in (
            ("exact_product", "INTEGER NOT NULL DEFAULT 0"),
            ("listing_intent", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("model_mismatch", "INTEGER NOT NULL DEFAULT 0"),
            ("price_bait", "INTEGER NOT NULL DEFAULT 0"),
            ("hidden_price_condition", "INTEGER NOT NULL DEFAULT 0"),
            ("displayed_price", "INTEGER"),
            ("effective_price", "INTEGER"),
            ("price_source", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("price_confidence", "REAL"),
            ("usable_price", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in ai_columns:
                self.connection.execute(f"ALTER TABLE ai_classifications ADD COLUMN {column} {definition}")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS final_email_reviews (
                id INTEGER PRIMARY KEY,
                marketplace TEXT NOT NULL,
                product_id TEXT NOT NULL,
                review_fingerprint TEXT NOT NULL,
                model TEXT NOT NULL,
                reasoning_effort TEXT NOT NULL,
                exact_product INTEGER,
                normalized_product_name TEXT,
                scope TEXT,
                condition_status TEXT,
                sale_status TEXT,
                text_image_match INTEGER,
                visible_damage INTEGER,
                model_mismatch INTEGER,
                displayed_price INTEGER,
                effective_price INTEGER,
                price_bait INTEGER,
                hidden_price_condition INTEGER,
                usable_price INTEGER,
                price_confidence REAL,
                send_email INTEGER,
                confidence REAL,
                suspicious_reason TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL,
                reviewed_price INTEGER,
                image_count INTEGER NOT NULL DEFAULT 0,
                review_status TEXT NOT NULL,
                reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                execution_duration_seconds REAL NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_final_email_reviews_reuse
            ON final_email_reviews (
                marketplace, product_id, review_fingerprint,
                model, reasoning_effort, success, id DESC
            )
            """
        )
        final_review_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(final_email_reviews)")
        }
        if "suspicious_reason" not in final_review_columns:
            self.connection.execute(
                "ALTER TABLE final_email_reviews ADD COLUMN suspicious_reason TEXT NOT NULL DEFAULT ''"
            )
        for column, definition in (
            ("model_mismatch", "INTEGER"),
            ("normalized_product_name", "TEXT"),
            ("displayed_price", "INTEGER"),
            ("effective_price", "INTEGER"),
            ("price_bait", "INTEGER"),
            ("hidden_price_condition", "INTEGER"),
            ("usable_price", "INTEGER"),
            ("price_confidence", "REAL"),
        ):
            if column not in final_review_columns:
                self.connection.execute(
                    f"ALTER TABLE final_email_reviews ADD COLUMN {column} {definition}"
                )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_review_jobs (
                marketplace TEXT NOT NULL,
                product_id TEXT NOT NULL,
                classification_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('pending', 'processing', 'retry', 'completed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (marketplace, product_id)
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_review_jobs_ready "
            "ON ai_review_jobs (state, available_at, updated_at)"
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_deliveries (
                id INTEGER PRIMARY KEY,
                delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                channel TEXT NOT NULL,
                listing_count INTEGER NOT NULL CHECK (listing_count > 0)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                id INTEGER PRIMARY KEY,
                completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL,
                search_records INTEGER NOT NULL DEFAULT 0,
                valid_observations INTEGER NOT NULL DEFAULT 0,
                ai_candidates INTEGER NOT NULL DEFAULT 0,
                ai_failures INTEGER NOT NULL DEFAULT 0,
                safety_halt INTEGER NOT NULL DEFAULT 0,
                detail TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self.connection.commit()

    def add(self, listing: Listing) -> bool:
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        last_active_at = listing.last_active_at or (
            now if listing.listing_status == "active" else None
        )
        last_active_price = listing.last_active_price
        if last_active_price is None and listing.listing_status == "active":
            last_active_price = listing.price
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO listings
                (listing_id, url, title, price, location, source_type, description, condition_status,
                 ai_is_computer_part, ai_normalized_product_name, ai_confidence, ai_reject, ai_reason,
                 marketplace, product_id, source_key, updated_at, search_fingerprint, canonical_url,
                 listing_status, ai_scope, ai_sale_status, ai_usable_for_market_price, ai_effective_price, ai_usable_price,
                 last_active_at, first_sold_seen_at, last_active_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                listing.canonical_url,
                listing.listing_status,
                listing.ai_scope,
                listing.ai_sale_status,
                int(listing.ai_usable_for_market_price),
                listing.effective_price,
                int(listing.ai_usable_price),
                last_active_at,
                listing.first_sold_seen_at,
                last_active_price,
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def backfill_checkpoint(self, query_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM market_price_backfill_checkpoints WHERE query_key = ?",
            (query_key,),
        ).fetchone()

    def update_backfill_checkpoint(
        self,
        query_key: str,
        query: str,
        *,
        cursor: str | None,
        pages_scanned: int,
        unique_listings_found: int,
        valid_observations: int,
        excluded_listings: int,
        ai_calls: int,
        ai_failures: int,
        completed: bool,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO market_price_backfill_checkpoints
                (query_key, query, cursor, pages_scanned, unique_listings_found,
                 valid_observations, excluded_listings, ai_calls, ai_failures,
                 completed, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(query_key) DO UPDATE SET
                query = excluded.query, cursor = excluded.cursor,
                pages_scanned = excluded.pages_scanned,
                unique_listings_found = excluded.unique_listings_found,
                valid_observations = excluded.valid_observations,
                excluded_listings = excluded.excluded_listings,
                ai_calls = excluded.ai_calls, ai_failures = excluded.ai_failures,
                completed = excluded.completed, updated_at = CURRENT_TIMESTAMP
            """,
            (
                query_key, query, cursor, pages_scanned, unique_listings_found,
                valid_observations, excluded_listings, ai_calls, ai_failures, int(completed),
            ),
        )
        self.connection.commit()

    def mark_backfill_product_seen(self, marketplace: str, product_id: str, query_key: str) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO market_price_backfill_seen_products
                (marketplace, product_id, first_query_key)
            VALUES (?, ?, ?)
            """,
            (marketplace, product_id, query_key),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def is_backfill_product_seen(self, marketplace: str, product_id: str) -> bool:
        return self.connection.execute(
            """SELECT 1 FROM market_price_backfill_seen_products
               WHERE marketplace = ? AND product_id = ?""",
            (marketplace, product_id),
        ).fetchone() is not None

    def queue_backfill_detail_retry(
        self, query_key: str, listing: Listing, error: Exception
    ) -> bool:
        """Persist a failed detail request and return whether it remains retryable."""
        if not listing.product_id:
            raise ValueError("Backfill detail retry requires product_id")
        http_status, exception_type, category, request_retry_count, message = (
            detail_error_diagnostics(error)
        )
        transient = is_transient_detail_error(error)
        formatted_error = f"{exception_type}: {message}"
        self.connection.execute(
            """
            INSERT INTO market_price_backfill_detail_retries
                (marketplace, product_id, query_key, listing_id, title, price, url,
                 location, source_type, source_key, updated_at, search_fingerprint,
                 canonical_url, listing_status, attempts, last_error, http_status,
                 exception_type, error_category, retry_count, last_error_message,
                 last_error_at, is_terminal)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?,
                    CURRENT_TIMESTAMP, ?)
            ON CONFLICT(marketplace, product_id) DO UPDATE SET
                query_key = excluded.query_key, listing_id = excluded.listing_id,
                title = excluded.title, price = excluded.price, url = excluded.url,
                location = excluded.location, source_type = excluded.source_type,
                source_key = excluded.source_key, updated_at = excluded.updated_at,
                search_fingerprint = excluded.search_fingerprint,
                canonical_url = excluded.canonical_url,
                listing_status = excluded.listing_status,
                attempts = market_price_backfill_detail_retries.attempts + 1,
                last_error = excluded.last_error,
                http_status = excluded.http_status,
                exception_type = excluded.exception_type,
                error_category = excluded.error_category,
                retry_count = market_price_backfill_detail_retries.retry_count
                              + excluded.retry_count + 1,
                last_error_message = excluded.last_error_message,
                last_error_at = CURRENT_TIMESTAMP,
                is_terminal = excluded.is_terminal,
                updated_at_retry = CURRENT_TIMESTAMP
            """,
            (
                listing.marketplace, listing.product_id, query_key, listing.listing_id,
                listing.title, listing.price, listing.url, listing.location,
                listing.source_type, listing.source_key, listing.updated_at,
                listing.search_fingerprint, listing.canonical_url, listing.listing_status,
                formatted_error, http_status, exception_type, category, request_retry_count,
                message, int(not transient),
            ),
        )
        self.connection.commit()
        return transient

    def backfill_detail_retries(self, query_key: str) -> list[Listing]:
        """Return durable detail retries for one query in their original listing shape."""
        rows = self.connection.execute(
            """
            SELECT * FROM market_price_backfill_detail_retries
            WHERE query_key = ? AND is_terminal = 0
            ORDER BY created_at, marketplace, product_id
            """,
            (query_key,),
        ).fetchall()
        return [
            Listing(
                title=row["title"], price=int(row["price"]), url=row["url"],
                location=row["location"], source_type=row["source_type"],
                listing_id=row["listing_id"], marketplace=row["marketplace"],
                product_id=row["product_id"], source_key=row["source_key"],
                updated_at=row["updated_at"], search_fingerprint=row["search_fingerprint"],
                canonical_url=row["canonical_url"], listing_status=row["listing_status"],
            )
            for row in rows
        ]

    def is_backfill_detail_retry_queued(self, marketplace: str, product_id: str) -> bool:
        return self.connection.execute(
            """SELECT 1 FROM market_price_backfill_detail_retries
               WHERE marketplace = ? AND product_id = ? AND is_terminal = 0""",
            (marketplace, product_id),
        ).fetchone() is not None

    def clear_backfill_detail_retry(self, marketplace: str, product_id: str) -> None:
        self.connection.execute(
            """DELETE FROM market_price_backfill_detail_retries
               WHERE marketplace = ? AND product_id = ?""",
            (marketplace, product_id),
        )
        self.connection.commit()

    def mark_backfill_listing_unavailable(self, listing: Listing) -> None:
        """Record a terminal detail failure without discarding prior listing evidence."""
        if not listing.product_id:
            raise ValueError("Backfill listing requires a product_id")
        cursor = self.connection.execute(
            """UPDATE listings SET listing_status = 'unavailable'
               WHERE marketplace = ? AND product_id = ?""",
            (listing.marketplace, listing.product_id),
        )
        if cursor.rowcount == 0:
            self.store_backfill_listing(replace(listing, listing_status="unavailable"))
            return
        self.connection.commit()

    def backfill_detail_retry_statistics(self, query_key: str | None = None) -> list[sqlite3.Row]:
        """Summarize pending and terminal detail failures by stable category."""
        where = ""
        parameters: tuple[str, ...] = ()
        if query_key is not None:
            where = "WHERE query_key = ?"
            parameters = (query_key,)
        return self.connection.execute(
            f"""
            SELECT error_category,
                   SUM(CASE WHEN is_terminal = 0 THEN 1 ELSE 0 END) AS queued_count,
                   SUM(CASE WHEN is_terminal = 1 THEN 1 ELSE 0 END) AS terminal_count,
                   COUNT(*) AS total_count
            FROM market_price_backfill_detail_retries
            {where}
            GROUP BY error_category
            ORDER BY error_category
            """,
            parameters,
        ).fetchall()

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
        latest_ai = self.connection.execute(
            """
            SELECT success FROM ai_classifications
            WHERE marketplace = ? AND product_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (listing.marketplace, listing.product_id),
        ).fetchone()
        if latest_ai is not None and not bool(latest_ai["success"]):
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
                source_key = ?, updated_at = ?, search_fingerprint = ?, canonical_url = ?,
                listing_status = ?, ai_scope = ?, ai_sale_status = ?, ai_usable_for_market_price = ?,
                ai_effective_price = ?, ai_usable_price = ?,
                last_active_at = CASE WHEN ? = 'active' THEN COALESCE(?, CURRENT_TIMESTAMP) ELSE last_active_at END,
                last_active_price = CASE WHEN ? = 'active' THEN ? ELSE last_active_price END,
                first_sold_seen_at = CASE WHEN ? = 'sold' THEN COALESCE(first_sold_seen_at, CURRENT_TIMESTAMP) ELSE first_sold_seen_at END,
                notified_at = CASE WHEN ? THEN NULL ELSE notified_at END
            WHERE marketplace = ? AND product_id = ?
            """,
            (
                listing.listing_id, listing.url, listing.title, listing.price,
                listing.location, listing.source_type, listing.description,
                listing.condition_status, listing.ai_is_computer_part,
                listing.ai_normalized_product_name, listing.ai_confidence,
                listing.ai_reject, listing.ai_reason, listing.source_key,
                listing.updated_at, listing.search_fingerprint, listing.canonical_url,
                listing.listing_status, listing.ai_scope,
                listing.ai_sale_status, int(listing.ai_usable_for_market_price),
                listing.effective_price, int(listing.ai_usable_price),
                listing.listing_status, listing.last_active_at,
                listing.listing_status,
                listing.last_active_price if listing.last_active_price is not None else listing.price,
                listing.listing_status,
                int(
                    state.status == "updated"
                    and state.previous_price is not None
                    and listing.price < state.previous_price
                ),
                listing.marketplace, listing.product_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Cannot update unknown product: {listing.product_id}")
        self.connection.commit()

    def store_backfill_listing(self, listing: Listing) -> None:
        """Persist backfill metadata without changing notification state.

        Backfill deliberately ignores normal-scan watermarks and known-listing
        decisions, but it must never make a listing newly eligible for an email.
        """
        if not listing.product_id:
            raise ValueError("Backfill listing requires a product_id")
        row = self.connection.execute(
            "SELECT id FROM listings WHERE marketplace = ? AND product_id = ?",
            (listing.marketplace, listing.product_id),
        ).fetchone()
        if row is None:
            if not self.add(listing):
                raise ValueError(f"Cannot insert backfill listing: {listing.product_id}")
            return
        self.connection.execute(
            """
            UPDATE listings SET
                listing_id = ?, url = ?, title = ?, price = ?, location = ?, source_type = ?,
                description = ?, condition_status = ?, ai_is_computer_part = ?,
                ai_normalized_product_name = ?, ai_confidence = ?, ai_reject = ?, ai_reason = ?,
                source_key = ?, updated_at = ?, search_fingerprint = ?, canonical_url = ?,
                listing_status = ?, ai_scope = ?, ai_sale_status = ?, ai_usable_for_market_price = ?
            WHERE id = ?
            """,
            (
                listing.listing_id, listing.url, listing.title, listing.price,
                listing.location, listing.source_type, listing.description,
                listing.condition_status, listing.ai_is_computer_part,
                listing.ai_normalized_product_name, listing.ai_confidence,
                listing.ai_reject, listing.ai_reason, listing.source_key,
                listing.updated_at, listing.search_fingerprint, listing.canonical_url,
                listing.listing_status, listing.ai_scope, listing.ai_sale_status,
                int(listing.ai_usable_for_market_price), int(row["id"]),
            ),
        )
        self.connection.commit()

    def sale_status_candidates(
        self,
        tracking: dict[str, object],
        *,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> list[SaleStatusCandidate]:
        """Return only due, previously qualified active listings for detail checks."""
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        recent_age_days = float(tracking["recent_age_days"])
        medium_age_days = float(tracking["medium_age_days"])
        recent_interval_hours = float(tracking["recent_interval_hours"])
        medium_interval_hours = float(tracking["medium_interval_hours"])
        older_interval_hours = float(tracking["older_interval_hours"])
        rows = self.connection.execute(
            """
            SELECT listing_id, url, title, price, location, source_type, description,
                   condition_status, ai_is_computer_part, ai_normalized_product_name,
                   ai_confidence, ai_reject, ai_reason, marketplace, product_id,
                   source_key, updated_at, search_fingerprint, canonical_url,
                   listing_status, ai_scope, ai_sale_status, ai_usable_for_market_price, ai_effective_price, ai_usable_price,
                   last_active_at, first_sold_seen_at,
                   last_active_price, first_seen_at
            FROM listings
            WHERE marketplace = 'bunjang'
              AND listing_status = 'active'
              AND ai_scope = 'standalone'
              AND condition_status = 'normal'
              AND ai_is_computer_part = 1
              AND ai_reject = 0
              AND ai_normalized_product_name IS NOT NULL
              AND product_id IS NOT NULL
            ORDER BY first_seen_at DESC
            """
        ).fetchall()

        def parse(value: str) -> datetime:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

        candidates: list[SaleStatusCandidate] = []
        for row in rows:
            try:
                age_days = max(0.0, (current - parse(str(row["first_seen_at"]))).total_seconds() / 86400)
                last_active = parse(str(row["last_active_at"] or row["first_seen_at"]))
            except ValueError:
                # A malformed legacy timestamp should be checked once, not silently skipped.
                age_days, last_active = 0.0, datetime.min.replace(tzinfo=UTC)
            interval_hours = (
                recent_interval_hours if age_days <= recent_age_days else
                medium_interval_hours if age_days <= medium_age_days else
                older_interval_hours
            )
            if current - last_active < timedelta(hours=interval_hours):
                continue
            candidates.append(
                SaleStatusCandidate(
                    Listing(
                        title=str(row["title"]), price=int(row["price"]), url=str(row["url"]),
                        location=str(row["location"]), source_type=str(row["source_type"]),
                        listing_id=str(row["listing_id"]) if row["listing_id"] else None,
                        description=str(row["description"]), condition_status=str(row["condition_status"]),
                        ai_is_computer_part=bool(row["ai_is_computer_part"]),
                        ai_normalized_product_name=str(row["ai_normalized_product_name"]),
                        ai_confidence=float(row["ai_confidence"]) if row["ai_confidence"] is not None else None,
                        ai_reject=bool(row["ai_reject"]), ai_reason=str(row["ai_reason"]),
                        marketplace=str(row["marketplace"]), product_id=str(row["product_id"]),
                        source_key=str(row["source_key"]),
                        updated_at=str(row["updated_at"]) if row["updated_at"] else None,
                        search_fingerprint=str(row["search_fingerprint"]),
                        canonical_url=str(row["canonical_url"]) if row["canonical_url"] else None,
                        listing_status=str(row["listing_status"]), ai_scope=str(row["ai_scope"]),
                        ai_sale_status=str(row["ai_sale_status"]),
                        ai_usable_for_market_price=bool(row["ai_usable_for_market_price"]),
                        last_active_at=str(row["last_active_at"]) if row["last_active_at"] else None,
                        first_sold_seen_at=str(row["first_sold_seen_at"]) if row["first_sold_seen_at"] else None,
                        last_active_price=int(row["last_active_price"]) if row["last_active_price"] is not None else None,
                    ),
                    age_days,
                    interval_hours,
                )
            )
            if limit is not None and len(candidates) >= limit:
                break
        return candidates

    def record_sale_status_check(self, listing: Listing, *, checked_at: str | None = None) -> bool:
        """Save a detail-page lifecycle check and return whether it first became sold."""
        if not listing.product_id:
            raise ValueError("Sale-status check requires a product_id")
        checked = checked_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
        row = self.connection.execute(
            """SELECT listing_status, last_active_price FROM listings
               WHERE marketplace = ? AND product_id = ?""",
            (listing.marketplace, listing.product_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"Cannot check unknown product: {listing.product_id}")
        became_sold = str(row["listing_status"]) == "active" and listing.listing_status == "sold"
        if listing.listing_status == "active":
            self.connection.execute(
                """
                UPDATE listings SET title = ?, price = ?, description = ?, condition_status = ?,
                    listing_status = 'active', last_active_at = ?, last_active_price = ?
                WHERE marketplace = ? AND product_id = ?
                """,
                (listing.title, listing.price, listing.description, listing.condition_status,
                 checked, listing.price, listing.marketplace, listing.product_id),
            )
        else:
            self.connection.execute(
                """
                UPDATE listings SET title = ?, description = ?, condition_status = ?,
                    listing_status = ?, first_sold_seen_at = CASE WHEN ? = 'sold'
                        THEN COALESCE(first_sold_seen_at, ?) ELSE first_sold_seen_at END,
                    last_active_price = COALESCE(last_active_price, ?)
                WHERE marketplace = ? AND product_id = ?
                """,
                (listing.title, listing.description, listing.condition_status, listing.listing_status,
                 listing.listing_status, checked, row["last_active_price"] or listing.price,
                 listing.marketplace, listing.product_id),
            )
        self.connection.commit()
        return became_sold

    def bunjang_listings_for_notification_test(self) -> list[Listing]:
        """Return stored, classified Bunjang listings for one explicit test email."""
        rows = self.connection.execute(
            """
            SELECT listing_id, url, title, price, location, source_type, description,
                   condition_status, ai_is_computer_part, ai_normalized_product_name,
                   ai_confidence, ai_reject, ai_reason, marketplace, product_id,
                   source_key, updated_at, search_fingerprint, canonical_url, listing_status, ai_scope
            FROM listings
            WHERE marketplace = 'bunjang'
              AND product_id IS NOT NULL
              AND ai_is_computer_part = 1
              AND condition_status = 'normal'
              AND ai_reject = 0
              AND listing_status = 'active'
              AND ai_scope = 'standalone'
            ORDER BY id DESC
            """
        ).fetchall()
        return [
            Listing(
                title=str(row["title"]),
                price=int(row["price"]),
                url=str(row["url"]),
                location=str(row["location"]),
                source_type=str(row["source_type"]),
                listing_id=str(row["listing_id"]) if row["listing_id"] else None,
                description=str(row["description"]),
                condition_status=str(row["condition_status"]),
                ai_is_computer_part=bool(row["ai_is_computer_part"]),
                ai_normalized_product_name=(
                    str(row["ai_normalized_product_name"])
                    if row["ai_normalized_product_name"] else None
                ),
                ai_confidence=float(row["ai_confidence"]) if row["ai_confidence"] is not None else None,
                ai_reject=bool(row["ai_reject"]),
                ai_reason=str(row["ai_reason"]),
                marketplace=str(row["marketplace"]),
                product_id=str(row["product_id"]),
                source_key=str(row["source_key"]),
                updated_at=str(row["updated_at"]) if row["updated_at"] else None,
                search_fingerprint=str(row["search_fingerprint"]),
                canonical_url=str(row["canonical_url"]) if row["canonical_url"] else None,
                listing_status=str(row["listing_status"]),
                ai_scope=str(row["ai_scope"]),
            )
            for row in rows
        ]

    def backlog_notification_listings(self) -> list[Listing]:
        """Return every never-notified Bunjang row for one bounded digest pass.

        This deliberately does not consult price observations or notification
        history beyond ``notified_at``.  The caller performs the current-detail
        and AI gates before sending anything.
        """
        rows = self.connection.execute(
            """
            SELECT listing_id, url, title, price, location, source_type, description,
                   condition_status, ai_is_computer_part, ai_normalized_product_name,
                   ai_confidence, ai_reject, ai_reason, marketplace, product_id,
                   source_key, updated_at, search_fingerprint, canonical_url,
                   listing_status, ai_scope, ai_sale_status, ai_usable_for_market_price,
                   ai_effective_price, ai_usable_price,
                   last_active_at, first_sold_seen_at, last_active_price
            FROM listings
            WHERE marketplace = 'bunjang'
              AND notified_at IS NULL
              AND product_id IS NOT NULL
            ORDER BY id ASC
            """
        ).fetchall()
        return [self._listing_from_row(row) for row in rows]

    def store_backlog_listing(self, listing: Listing) -> None:
        """Persist current-detail/AI fields without altering notification or prices history."""
        if not listing.product_id:
            raise ValueError("Backlog listing requires a product_id")
        cursor = self.connection.execute(
            """
            UPDATE listings SET
                listing_id = ?, url = ?, title = ?, price = ?, location = ?, source_type = ?,
                description = ?, condition_status = ?, ai_is_computer_part = ?,
                ai_normalized_product_name = ?, ai_confidence = ?, ai_reject = ?, ai_reason = ?,
                source_key = ?, updated_at = ?, search_fingerprint = ?, canonical_url = ?,
                listing_status = ?, ai_scope = ?, ai_sale_status = ?, ai_usable_for_market_price = ?,
                ai_effective_price = ?, ai_usable_price = ?,
                last_active_at = CASE WHEN ? = 'active' THEN CURRENT_TIMESTAMP ELSE last_active_at END,
                last_active_price = CASE WHEN ? = 'active' THEN ? ELSE last_active_price END,
                first_sold_seen_at = CASE WHEN ? = 'sold' THEN COALESCE(first_sold_seen_at, CURRENT_TIMESTAMP) ELSE first_sold_seen_at END
            WHERE marketplace = ? AND product_id = ? AND notified_at IS NULL
            """,
            (
                listing.listing_id, listing.url, listing.title, listing.price,
                listing.location, listing.source_type, listing.description,
                listing.condition_status, listing.ai_is_computer_part,
                listing.ai_normalized_product_name, listing.ai_confidence,
                int(listing.ai_reject), listing.ai_reason, listing.source_key,
                listing.updated_at, listing.search_fingerprint, listing.canonical_url,
                listing.listing_status, listing.ai_scope, listing.ai_sale_status,
                int(listing.ai_usable_for_market_price), listing.effective_price,
                int(listing.ai_usable_price), listing.listing_status,
                listing.listing_status,
                listing.last_active_price if listing.last_active_price is not None else listing.price,
                listing.listing_status, listing.marketplace, listing.product_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Cannot update unnotified backlog product: {listing.product_id}")
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
                 normalized_product_name, condition_status, scope, sale_status,
                 usable_for_market_price, exact_product, listing_intent, model_mismatch,
                 price_bait, hidden_price_condition, displayed_price, effective_price, price_source,
                 price_confidence, usable_price, confidence, reject, reason,
                 execution_duration_seconds, success)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing.marketplace, listing.product_id, listing.listing_id, fingerprint,
                model, reasoning_effort, classifier_version,
                int(classification.is_computer_part) if classification else None,
                classification.normalized_product_name if classification else None,
                status, classification.scope if classification else "unknown",
                classification.sale_status if classification else "unknown",
                0,
                int(classification.exact_product) if classification else 0,
                classification.listing_intent if classification else "unknown",
                int(classification.model_mismatch) if classification else 0,
                int(classification.price_bait) if classification else 0,
                int(classification.hidden_price_condition) if classification else 0,
                classification.displayed_price if classification else None,
                classification.effective_price if classification else None,
                classification.price_source if classification else "unknown",
                classification.price_confidence if classification else None,
                int(classification.usable_price) if classification else 0,
                classification.confidence if classification else None,
                0 if classification else 1,
                classification.reason if classification else (error_reason or "Codex CLI classification failed"),
                execution_duration_seconds, int(classification is not None),
            ),
        )
        self.connection.commit()

    def enqueue_ai_review(self, listing: Listing, fingerprint: str) -> None:
        """Durably retain first-stage work before a Codex subprocess is started."""
        if not listing.product_id:
            raise ValueError("AI review queue requires a product_id")
        self.connection.execute(
            """
            INSERT INTO ai_review_jobs
                (marketplace, product_id, classification_fingerprint, state, attempts, available_at)
            VALUES (?, ?, ?, 'pending', 0, CURRENT_TIMESTAMP)
            ON CONFLICT(marketplace, product_id) DO UPDATE SET
                classification_fingerprint = excluded.classification_fingerprint,
                state = CASE WHEN ai_review_jobs.classification_fingerprint != excluded.classification_fingerprint
                    THEN 'pending' ELSE ai_review_jobs.state END,
                attempts = CASE WHEN ai_review_jobs.classification_fingerprint != excluded.classification_fingerprint
                    THEN 0 ELSE ai_review_jobs.attempts END,
                last_error = CASE WHEN ai_review_jobs.classification_fingerprint != excluded.classification_fingerprint
                    THEN '' ELSE ai_review_jobs.last_error END,
                available_at = CASE WHEN ai_review_jobs.classification_fingerprint != excluded.classification_fingerprint
                    THEN CURRENT_TIMESTAMP ELSE ai_review_jobs.available_at END,
                updated_at = CURRENT_TIMESTAMP
            """,
            (listing.marketplace, listing.product_id, fingerprint),
        )
        self.connection.commit()

    def recover_stale_ai_reviews(self, timeout_seconds: float) -> int:
        """Return abandoned processing work to retry after a crash or reboot."""
        if timeout_seconds <= 0:
            raise ValueError("AI review recovery timeout must be positive")
        cutoff = (datetime.now(UTC) - timedelta(seconds=timeout_seconds)).isoformat().replace("+00:00", "Z")
        cursor = self.connection.execute(
            """
            UPDATE ai_review_jobs
            SET state = 'retry', available_at = CURRENT_TIMESTAMP, started_at = NULL,
                last_error = CASE WHEN last_error = '' THEN 'stale processing recovery' ELSE last_error END,
                updated_at = CURRENT_TIMESTAMP
            WHERE state = 'processing' AND started_at IS NOT NULL AND started_at < ?
            """,
            (cutoff,),
        )
        self.connection.commit()
        return cursor.rowcount

    def ready_ai_review_listings(self, *, limit: int | None = None) -> list[Listing]:
        """Load pending/retry work from the current persisted listing content."""
        sql = """
            SELECT l.listing_id, l.url, l.title, l.price, l.location, l.source_type, l.description,
                   l.condition_status, l.ai_is_computer_part, l.ai_normalized_product_name,
                   l.ai_confidence, l.ai_reject, l.ai_reason, l.marketplace, l.product_id,
                   l.source_key, l.updated_at, l.search_fingerprint, l.canonical_url,
                   l.listing_status, l.ai_scope, l.ai_sale_status, l.ai_usable_for_market_price,
                   l.ai_effective_price, l.ai_usable_price, l.last_active_at, l.first_sold_seen_at,
                   l.last_active_price
            FROM ai_review_jobs j
            JOIN listings l ON l.marketplace = j.marketplace AND l.product_id = j.product_id
            WHERE j.state IN ('pending', 'retry') AND j.available_at <= CURRENT_TIMESTAMP
            ORDER BY j.updated_at ASC
        """
        if limit is not None:
            sql += " LIMIT ?"
            rows = self.connection.execute(sql, (limit,)).fetchall()
        else:
            rows = self.connection.execute(sql).fetchall()
        return [self._listing_from_row(row) for row in rows]

    def mark_ai_review_processing(self, listing: Listing) -> bool:
        if not listing.product_id:
            return False
        cursor = self.connection.execute(
            """
            UPDATE ai_review_jobs SET state = 'processing', attempts = attempts + 1,
                started_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE marketplace = ? AND product_id = ? AND state IN ('pending', 'retry')
            """,
            (listing.marketplace, listing.product_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def finish_ai_review(self, listing: Listing, *, error: str | None = None) -> None:
        if not listing.product_id:
            return
        if error is None:
            state, available = 'completed', 'CURRENT_TIMESTAMP'
            self.connection.execute(
                """UPDATE ai_review_jobs SET state = ?, completed_at = CURRENT_TIMESTAMP,
                       started_at = NULL, last_error = '', updated_at = CURRENT_TIMESTAMP
                   WHERE marketplace = ? AND product_id = ?""",
                (state, listing.marketplace, listing.product_id),
            )
        else:
            # A bounded delay prevents a quota/rate-limit outage from being hammered
            # every ten minutes while retaining the job for future scans.
            self.connection.execute(
                """UPDATE ai_review_jobs SET state = 'retry', started_at = NULL,
                       last_error = ?, available_at = datetime('now', '+10 minutes'),
                       updated_at = CURRENT_TIMESTAMP
                   WHERE marketplace = ? AND product_id = ?""",
                (error[:1000], listing.marketplace, listing.product_id),
            )
        self.connection.commit()

    def ai_review_queue_counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT state, COUNT(*) AS count FROM ai_review_jobs GROUP BY state"
        ).fetchall()
        counts = {state: 0 for state in ('pending', 'processing', 'retry', 'completed')}
        counts.update({str(row['state']): int(row['count']) for row in rows})
        return counts

    def record_notification_delivery(self, listing_count: int, *, channel: str = 'email') -> None:
        self.connection.execute(
            "INSERT INTO notification_deliveries (channel, listing_count) VALUES (?, ?)",
            (channel, listing_count),
        )
        self.connection.commit()

    def record_pipeline_run(
        self, *, status: str, search_records: int, valid_observations: int,
        ai_candidates: int, ai_failures: int, safety_halt: bool, detail: str = '',
    ) -> None:
        self.connection.execute(
            """INSERT INTO pipeline_runs
               (status, search_records, valid_observations, ai_candidates, ai_failures, safety_halt, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (status, search_records, valid_observations, ai_candidates, ai_failures,
             int(safety_halt), detail[:2000]),
        )
        self.connection.commit()

    def operational_status_rows(self) -> dict[str, object]:
        """Small, stable DB portion of the operator status command."""
        listing_rows = self.connection.execute(
            "SELECT marketplace, COUNT(*) AS count FROM listings GROUP BY marketplace"
        ).fetchall()
        latest_run = self.connection.execute(
            "SELECT * FROM pipeline_runs WHERE status = 'success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        latest_delivery = self.connection.execute(
            "SELECT * FROM notification_deliveries ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if latest_delivery is None:
            latest_delivery = self.connection.execute(
                """SELECT notified_at AS delivered_at, COUNT(*) AS listing_count, 'email' AS channel
                   FROM listings WHERE notified_at = (SELECT MAX(notified_at) FROM listings WHERE notified_at IS NOT NULL)
                   GROUP BY notified_at"""
            ).fetchone()
        recent_ai_failures = self.connection.execute(
            """SELECT COUNT(*) FROM ai_classifications
               WHERE success = 0 AND classification_at >= datetime('now', '-24 hours')"""
        ).fetchone()[0]
        latest_pricing = self.connection.execute(
            "SELECT MAX(observed_at) FROM price_observations"
        ).fetchone()[0]
        return {
            'listing_counts': {str(row['marketplace']): int(row['count']) for row in listing_rows},
            'last_successful_scan': dict(latest_run) if latest_run else None,
            'last_notification': dict(latest_delivery) if latest_delivery else None,
            'recent_ai_failures': int(recent_ai_failures),
            'recent_pricing_update': str(latest_pricing) if latest_pricing else None,
        }

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
            SELECT is_computer_part, normalized_product_name, condition_status, scope,
                   sale_status, usable_for_market_price, exact_product, listing_intent,
                   model_mismatch, price_bait, hidden_price_condition, displayed_price, effective_price,
                   price_source, price_confidence, usable_price, confidence, reject, reason
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
        if (
            row is None
            or row["is_computer_part"] is None
            or row["confidence"] is None
            or row["scope"] == "unknown"
        ):
            return None
        return AIClassification(
            bool(row["is_computer_part"]),
            str(row["normalized_product_name"]) if row["normalized_product_name"] else None,
            str(row["condition_status"]), float(row["confidence"]),
            bool(row["reject"]), str(row["reason"]), str(row["scope"]),
            str(row["sale_status"]), bool(row["usable_for_market_price"]),
            bool(row["exact_product"]), str(row["listing_intent"]), bool(row["model_mismatch"]),
            bool(row["price_bait"]), int(row["displayed_price"]) if row["displayed_price"] else None,
            int(row["effective_price"]) if row["effective_price"] else None,
            str(row["price_source"]), float(row["price_confidence"]) if row["price_confidence"] is not None else 0.0,
            bool(row["usable_price"]), bool(row["hidden_price_condition"]),
        )

    def record_final_email_review(
        self,
        listing: Listing,
        fingerprint: str,
        *,
        model: str,
        reasoning_effort: str,
        review: object | None,
        reviewed_price: int | None,
        image_count: int,
        review_status: str,
        execution_duration_seconds: float,
        error_reason: str | None = None,
    ) -> None:
        """Append one final-review decision; failures remain retryable."""
        if not listing.product_id:
            raise ValueError("final email review requires a Bunjang product_id")
        result = review
        self.connection.execute(
            """
            INSERT INTO final_email_reviews
                (marketplace, product_id, review_fingerprint, model, reasoning_effort,
                 exact_product, normalized_product_name, scope, condition_status, sale_status, text_image_match,
                 visible_damage, model_mismatch, displayed_price, effective_price, price_bait,
                 hidden_price_condition, usable_price, price_confidence, send_email, confidence,
                 suspicious_reason, reason, reviewed_price, image_count, review_status,
                 execution_duration_seconds, success)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing.marketplace, listing.product_id, fingerprint, model, reasoning_effort,
                int(getattr(result, "exact_product")) if result is not None else None,
                getattr(result, "normalized_product_name", None), getattr(result, "scope", None), getattr(result, "condition", None),
                getattr(result, "sale_status", None),
                int(getattr(result, "text_image_match", False)) if result is not None else None,
                int(getattr(result, "visible_damage", False)) if result is not None else None,
                int(getattr(result, "model_mismatch", False)) if result is not None else None,
                getattr(result, "displayed_price", None), getattr(result, "effective_price", None),
                int(getattr(result, "price_bait", False)) if result is not None else None,
                int(getattr(result, "hidden_price_condition", False)) if result is not None else None,
                int(getattr(result, "usable_price", False)) if result is not None else None,
                getattr(result, "price_confidence", None),
                # Historical send_email values are retained, but factual v2 reviews never set it.
                None,
                getattr(result, "confidence", None),
                getattr(result, "suspicious_reason", "") if result is not None else "",
                getattr(result, "reason", None) if result is not None else (
                    error_reason or "final Codex review failed"
                ),
                reviewed_price, image_count, review_status, execution_duration_seconds,
                int(result is not None),
            ),
        )
        self.connection.commit()

    def cached_final_email_review(
        self,
        listing: Listing,
        fingerprint: str,
        *,
        model: str,
        reasoning_effort: str,
    ) -> sqlite3.Row | None:
        """Return a schema-valid prior final review for unchanged current detail."""
        if not listing.product_id:
            return None
        return self.connection.execute(
            """
            SELECT exact_product, normalized_product_name, scope, condition_status AS condition, sale_status,
                   text_image_match, visible_damage, model_mismatch, displayed_price,
                   effective_price, price_bait, hidden_price_condition, usable_price,
                   price_confidence, send_email, confidence,
                   suspicious_reason, reason, reviewed_price, image_count, review_status
            FROM final_email_reviews
            WHERE marketplace = ? AND product_id = ? AND review_fingerprint = ?
              AND model = ? AND reasoning_effort = ? AND success = 1
            ORDER BY id DESC LIMIT 1
            """,
            (listing.marketplace, listing.product_id, fingerprint, model, reasoning_effort),
        ).fetchone()

    def final_email_review_metadata(self, listing: Listing) -> dict[str, object]:
        """Return the factual text-review fields for an already-approved listing."""
        if not listing.product_id:
            return {}
        row = self.connection.execute(
            """
            SELECT confidence, reason
            FROM final_email_reviews
            WHERE marketplace = ? AND product_id = ?
              AND review_status = 'approved' AND success = 1
            ORDER BY id DESC LIMIT 1
            """,
            (listing.marketplace, listing.product_id),
        ).fetchone()
        if row is None:
            return {}
        return {
            "second_stage_confidence": float(row["confidence"])
            if row["confidence"] is not None else None,
            "reason": str(row["reason"] or ""),
        }

    def market_price_repair_checkpoint(self, repair_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM market_price_repair_checkpoints WHERE repair_key = ?",
            (repair_key,),
        ).fetchone()

    def update_market_price_repair_checkpoint(
        self,
        repair_key: str,
        *,
        last_listing_id: int,
        listings_reviewed: int,
        deterministic_accepts: int,
        deterministic_rejects: int,
        ai_calls: int,
        ai_failures: int,
        observations_rebuilt: int,
        completed: bool,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO market_price_repair_checkpoints
                (repair_key, last_listing_id, listings_reviewed, deterministic_accepts,
                 deterministic_rejects, ai_calls, ai_failures, observations_rebuilt,
                 completed, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(repair_key) DO UPDATE SET
                last_listing_id = excluded.last_listing_id,
                listings_reviewed = excluded.listings_reviewed,
                deterministic_accepts = excluded.deterministic_accepts,
                deterministic_rejects = excluded.deterministic_rejects,
                ai_calls = excluded.ai_calls, ai_failures = excluded.ai_failures,
                observations_rebuilt = excluded.observations_rebuilt,
                completed = excluded.completed, updated_at = CURRENT_TIMESTAMP
            """,
            (
                repair_key, last_listing_id, listings_reviewed, deterministic_accepts,
                deterministic_rejects, ai_calls, ai_failures, observations_rebuilt,
                int(completed),
            ),
        )
        self.connection.commit()

    def market_price_repair_candidates(self, after_id: int = 0) -> list[tuple[int, Listing]]:
        """Return persisted backfill records, never fetching the marketplace."""
        rows = self.connection.execute(
            """
            SELECT id, listing_id, url, title, price, location, source_type, description,
                   condition_status, ai_is_computer_part, ai_normalized_product_name,
                   ai_confidence, ai_reject, ai_reason, marketplace, product_id,
                   source_key, updated_at, search_fingerprint, canonical_url,
                   listing_status, ai_scope, ai_sale_status, ai_usable_for_market_price,
                   ai_effective_price, ai_usable_price,
                   last_active_at, first_sold_seen_at, last_active_price
            FROM listings
            WHERE id > ? AND source_key LIKE 'all-market-price-backfill:%'
            ORDER BY id
            """,
            (after_id,),
        ).fetchall()
        return [(int(row["id"]), self._listing_from_row(row)) for row in rows]

    @staticmethod
    def _listing_from_row(row: sqlite3.Row) -> Listing:
        return Listing(
            title=str(row["title"]), price=int(row["price"]), url=str(row["url"]),
            location=str(row["location"]), source_type=str(row["source_type"]),
            listing_id=str(row["listing_id"]) if row["listing_id"] else None,
            description=str(row["description"]), condition_status=str(row["condition_status"]),
            ai_is_computer_part=(bool(row["ai_is_computer_part"])
                                 if row["ai_is_computer_part"] is not None else None),
            ai_normalized_product_name=(str(row["ai_normalized_product_name"])
                                        if row["ai_normalized_product_name"] else None),
            ai_confidence=(float(row["ai_confidence"])
                           if row["ai_confidence"] is not None else None),
            ai_reject=bool(row["ai_reject"]), ai_reason=str(row["ai_reason"]),
            marketplace=str(row["marketplace"]),
            product_id=str(row["product_id"]) if row["product_id"] else None,
            source_key=str(row["source_key"]),
            updated_at=str(row["updated_at"]) if row["updated_at"] else None,
            search_fingerprint=str(row["search_fingerprint"]),
            canonical_url=str(row["canonical_url"]) if row["canonical_url"] else None,
            listing_status=str(row["listing_status"]), ai_scope=str(row["ai_scope"]),
            ai_sale_status=str(row["ai_sale_status"]),
            ai_usable_for_market_price=bool(row["ai_usable_for_market_price"]),
            effective_price=(int(row["ai_effective_price"])
                             if row["ai_effective_price"] is not None else None),
            ai_usable_price=bool(row["ai_usable_price"]),
            last_active_at=str(row["last_active_at"]) if row["last_active_at"] else None,
            first_sold_seen_at=(str(row["first_sold_seen_at"])
                                if row["first_sold_seen_at"] else None),
            last_active_price=(int(row["last_active_price"])
                              if row["last_active_price"] is not None else None),
        )

    def listing_by_product_id(self, marketplace: str, product_id: str) -> Listing | None:
        """Return one stored listing for a read/review operation without changing it."""
        row = self.connection.execute(
            """
            SELECT listing_id, url, title, price, location, source_type, description,
                   condition_status, ai_is_computer_part, ai_normalized_product_name,
                   ai_confidence, ai_reject, ai_reason, marketplace, product_id,
                   source_key, updated_at, search_fingerprint, canonical_url,
                   listing_status, ai_scope, ai_sale_status, ai_usable_for_market_price,
                   ai_effective_price, ai_usable_price,
                   last_active_at, first_sold_seen_at, last_active_price
            FROM listings WHERE marketplace = ? AND product_id = ?
            """,
            (marketplace, product_id),
        ).fetchone()
        return self._listing_from_row(row) if row is not None else None

    def store_market_price_repair_listing(self, listing_id: int, listing: Listing) -> None:
        """Update only repair-derived classification fields; preserve notifications."""
        self.connection.execute(
            """
            UPDATE listings SET condition_status = ?, ai_is_computer_part = ?,
                ai_normalized_product_name = ?, ai_confidence = ?, ai_reject = ?,
                ai_reason = ?, ai_scope = ?, ai_sale_status = ?,
                ai_usable_for_market_price = ?
            WHERE id = ?
            """,
            (
                listing.condition_status, listing.ai_is_computer_part,
                listing.ai_normalized_product_name, listing.ai_confidence,
                int(listing.ai_reject), listing.ai_reason, listing.ai_scope,
                listing.ai_sale_status, int(listing.ai_usable_for_market_price), listing_id,
            ),
        )

    def begin_market_price_observation_rebuild(self) -> None:
        """Invalidate derived evidence while retaining every raw listing and row."""
        self.connection.execute(
            """UPDATE price_observations SET valid = 0,
               invalid_reason = 'superseded by offline market-price repair'"""
        )
        # This is derived history, rebuilt from the corrected valid evidence below.
        self.connection.execute("DELETE FROM market_price_history")
        self.connection.commit()

    def rebuild_market_price_observation(self, listing: Listing, normalized_name: str) -> bool:
        """Upsert one corrected derived observation without touching raw listings."""
        if not listing.product_id:
            return False
        row = self.connection.execute(
            "SELECT first_seen_at FROM listings WHERE marketplace = ? AND product_id = ?",
            (listing.marketplace, listing.product_id),
        ).fetchone()
        if row is None:
            return False
        observed_at = listing.updated_at or str(row["first_seen_at"])
        self.connection.execute(
            """
            INSERT INTO price_observations
                (marketplace, product_id, normalized_product_name, observed_price,
                 observed_at, first_seen_at, source_updated_at, listing_id, valid, invalid_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)
            ON CONFLICT(marketplace, product_id, normalized_product_name, observed_at)
            DO UPDATE SET observed_price = excluded.observed_price, first_seen_at = excluded.first_seen_at,
                source_updated_at = excluded.source_updated_at, listing_id = excluded.listing_id,
                valid = 1, invalid_reason = NULL
            """,
            (
                listing.marketplace, listing.product_id, normalized_name, listing.price,
                observed_at, str(row["first_seen_at"]), listing.updated_at, listing.listing_id,
            ),
        )
        return True

    def finish_market_price_observation_rebuild(self) -> int:
        """Recreate market-price history solely from corrected valid observations."""
        cursor = self.connection.execute(
            """
            INSERT INTO market_price_history
                (marketplace, product_id, normalized_product_name, observed_price,
                 observed_at, source_updated_at, price_observation_id)
            SELECT marketplace, product_id, normalized_product_name, observed_price,
                   observed_at, source_updated_at, id
            FROM price_observations WHERE valid = 1
            ORDER BY id
            """
        )
        self.connection.commit()
        return cursor.rowcount

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
        if (
            not listing.product_id
            or listing.listing_status != "active"
            or listing.ai_scope != "standalone"
            or not listing.ai_usable_for_market_price
            or not listing.ai_usable_price
            or listing.effective_price is None
            or listing.effective_price <= 0
        ):
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
                listing.marketplace, listing.product_id, normalized_name, listing.effective_price,
                timestamp, str(listing_row["first_seen_at"]), listing.updated_at,
                listing.listing_id,
            ),
        )
        self.connection.commit()
        if cursor.rowcount == 1:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO market_price_history
                    (marketplace, product_id, normalized_product_name, observed_price,
                     observed_at, source_updated_at, price_observation_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    listing.marketplace, listing.product_id, normalized_name, listing.effective_price,
                    timestamp, listing.updated_at, cursor.lastrowid,
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
            WHERE normalized_product_name = ? AND valid = 1
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

    def invalidate_contaminated_price_observations(self) -> dict[str, int]:
        """Invalidate, without deleting, stored evidence that is not an active part sale.

        Older rows predate scope storage. Their title and description are classified
        conservatively here so clear complete-PC, bundle, accessory, and model-token
        mismatches cannot continue to influence automatic prices.
        """
        rows = self.connection.execute(
            """
            SELECT o.id, o.normalized_product_name, l.id AS listing_row_id, l.title,
                   l.description, l.listing_status, l.ai_scope,
                   (
                       SELECT classifier_version FROM ai_classifications AS a
                       WHERE a.marketplace = o.marketplace AND a.product_id = o.product_id
                         AND a.success = 1
                       ORDER BY a.id DESC LIMIT 1
                   ) AS classifier_version
            FROM price_observations AS o
            LEFT JOIN listings AS l
              ON l.marketplace = o.marketplace AND l.product_id = o.product_id
            """
        ).fetchall()
        invalidated = 0
        reviewed = 0
        for row in rows:
            if row["listing_row_id"] is None:
                continue
            reviewed += 1
            title = str(row["title"])
            description = str(row["description"])
            status = str(row["listing_status"])
            classifier_version = str(row["classifier_version"] or "")
            stored_scope = str(row["ai_scope"])
            scope = (
                stored_scope
                if classifier_version == CLASSIFIER_VERSION and stored_scope in _LISTING_SCOPES
                else _historical_scope(title, description)
            )
            if classifier_version != CLASSIFIER_VERSION:
                self.connection.execute(
                    "UPDATE listings SET ai_scope = ? WHERE id = ?",
                    (scope, int(row["listing_row_id"])),
                )
            reason: str | None = None
            if status not in _LISTING_STATUSES or status != "active":
                reason = f"listing status is {status}"
            elif scope != "standalone":
                reason = f"listing scope is {scope}"
            elif _observation_name_mismatch(str(row["normalized_product_name"]), title, description):
                reason = "normalized product does not match listing text"
            if reason:
                self.connection.execute(
                    "UPDATE price_observations SET valid = 0, invalid_reason = ? WHERE id = ?",
                    (reason, int(row["id"])),
                )
                invalidated += 1
            else:
                self.connection.execute(
                    "UPDATE price_observations SET valid = 1, invalid_reason = NULL WHERE id = ?",
                    (int(row["id"]),),
                )
        self.connection.commit()
        return {"reviewed": reviewed, "invalidated": invalidated}

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
