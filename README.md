# used_pc_finder

A respectful scanner for publicly accessible used computer-part listings. Bunjang
is the active marketplace source. The scanner stores listings in SQLite,
normalizes products, applies editable condition rules, compares prices with
editable reference prices, and can email qualifying bargains through Gmail.

## Quick start

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py --sample
.venv/bin/python -m unittest discover -v
```

Use a no-email live test for one configured source:

```sh
.venv/bin/python main.py --live --source rtx_3060_ti --limit 3 --no-email
```

Remove `--no-email` only when real Gmail notifications are intended. `--limit`
limits candidates processed per configured search query; it does not change the
public search response page size.

## Bunjang source and incremental scanning

`config/settings.json` contains the editable `bunjang_sources` list. The initial
queries target practical sub-500,000 KRW candidates: selected RTX/RX GPUs, Ryzen
and Intel CPUs, DDR5 kits, 2 TB NVMe drives, and B650/B550/Z790/B760 boards.
`maximum_listing_price` defaults to 500,000 KRW. The crawler drops a more expensive
public search card before SQLite checks, detail fetching, condition analysis, AI,
pricing, or email. Searches use Bunjang's publicly available web search with
`sort=latest`; no login, private API, CAPTCHA bypass, or browser automation is used.

For each search record the crawler captures the Bunjang product ID, title, price,
canonical URL, `updatedAt`, and inexpensive search metadata. It excludes records
identified as advertisements, external sponsored cards, and non-selling products.
Only a new or changed record causes a detail request.

SQLite considers a Bunjang item new when its product ID has not been stored. A
stored item is processed again when its `updatedAt`, price, or search-content
fingerprint changes, allowing price reductions to be reconsidered. Otherwise it
is skipped before description fetching, condition filtering, optional AI work,
pricing, or email. Product IDs are de-duplicated across search queries during a
scan.

The scanner keeps a source-specific `updatedAt` watermark. It verifies that
timestamps decrease both within each fetched page and across page boundaries
before using a cutoff. It stops only after records are strictly older than the
previous watermark, unchanged, and at least the configured
`watermark_overlap_pages` extra page has been checked. Equal timestamps and an
ordering inconsistency disable the early cutoff for that run.

The former Karrot crawler is retained under `used_pc_finder/legacy/` for future
reuse, but it is not an active scan source.

## Condition, pricing, and AI gates

The crawler obtains a detail description only for new or changed records. The
editable rules in `config/condition_rules.json` classify a listing as `normal`,
`risky`, `broken`, or `unknown`. Only `normal` listings can reach price comparison
or notification. All other conditions are still stored in SQLite.

Bunjang requests retry transient connection/read timeouts and 5xx responses up to
two times with exponential backoff. Permanent 4xx responses are not retried. An
exhausted detail request is durably queued by product ID and retried on a later
full-backfill invocation; it does not pause the page or query. Failed searches are
logged and leave their cursor checkpoint unchanged for a later run.

The Codex CLI classifier is enabled by default and uses the already authenticated
ChatGPT Codex session—not `OPENAI_API_KEY`. Deterministic rules first accept clear,
active standalone parts and reject clear broken, unavailable, accessory, bundle,
complete-PC, and model-mismatched listings. Only the remaining ambiguous listings
enter the AI queue. It runs `gpt-5.6-luna` at `low` reasoning with a strict JSON
schema, a 90-second timeout, 0.85 confidence threshold, and a streaming worker
pool of four concurrent calls by default; all values are editable in
`ai_classification`.
Codex web search is not enabled
and the prompt prohibits tools, browsing, and marketplace access.

Only high-confidence, active, normal, standalone, non-rejected parts explicitly
marked usable for market price, whose product name can be converted through the
local canonical alias system, can add market-price observations. A
timeout, subprocess error, unsupported model, malformed output, or schema error
fails closed and cannot notify; failures remain eligible for retry on a later scan.
SQLite's `ai_classifications` table retains every actual result and
failure with model, reasoning effort, content fingerprint, timestamp, duration,
and classifier version; an unchanged fingerprint reuses a successful result.

## Backfill progress

Full market-price backfills print periodic `BACKFILL_LIVE` lines with query/page
counts, listing and observation totals, exclusions, AI queue/worker statistics,
rolling AI duration, elapsed time, crawl state, and a smoothed ETA. The ETA is
`calculating` until there are enough recent samples; after crawling ends it uses
the remaining queue, active workers, and recent AI duration for a more precise
drain estimate. `ai_classification.progress_interval_seconds` controls the report
interval. Crawling continues while up to `ai_concurrency` ambiguous reviews run.

Reference prices are in `data/market_prices.json`; adjust
`minimum_discount_percent` in `config/settings.json` to change the deal threshold.

## Automatic market prices

The editable manual prices in `data/market_prices.json` remain the fallback. New
and price-changed eligible Bunjang listings additionally append an immutable row
to SQLite's `price_observations` table with marketplace, product ID, normalized
product, price, observation time, original first-seen time, and source update time.
An old listing whose price falls therefore creates a fresh, high-weight observation.

Automatic estimates use observations from the configurable 90-day window, reject
unsuitable conditions/accessories/bundles and robust MAD price outliers, then apply
exponential decay (`0.5 ** (age_days / half_life_days)`). The default 21-day
half-life and weighted median reduce the influence of stale or unrealistic prices.
At least five valid observations are required; otherwise the manual price is used.

Inspect one normalized product without crawling:

```sh
.venv/bin/python main.py --market-price "RTX 3060 Ti"
```

The output reports the effective price, valid observation count, estimator,
oldest/newest valid observations, and whether it is automatic or manual fallback.

## Email notifications

Set `email_notifications.enabled` to `true` and configure the sender, recipient,
and SMTP details in `config/settings.json`. For unattended local use, run
`.venv/bin/python main.py --setup-email` once. It uses hidden input and saves the
Gmail app password only in `~/.config/used_pc_finder/secrets.env` with mode `600`;
normal program startup loads it automatically. To send exactly one real
bargain-format SMTP test email, run `.venv/bin/python main.py --test-email`.
Successful notifications are recorded in SQLite. When a stored Bunjang product
changes, its notification state is reset so a newly reduced price can be
considered again.
