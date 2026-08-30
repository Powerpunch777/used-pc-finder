# used_pc_finder

Private, self-hosted Bunjang scanner for used PC components. It reads public
listing data, keeps a local SQLite catalogue, estimates trusted market prices,
and sends one digest for verified bargains. It is designed for a single Debian
operator and is not a hosted service.

## Architecture

`BunjangCrawler` performs incremental latest-first searches and refetches detail
only for new or changed listings. SQLite stores listings, checkpoints, price
observations, AI audit records, durable first-stage AI jobs, final-review facts,
and notification state. Application code evaluates effective price, reference
market price, and the configured discount threshold.

Every possible notification goes through two text-only AI stages:

1. First-stage classification verifies exact model, standalone scope, condition,
   active status, and unambiguous effective price. Its SQLite queue survives
   Codex quota/rate-limit/timeout failures and retries with concurrency four.
2. The independent final stage refetches current Bunjang text and verifies the
   exact product, price, status, and confidence before delivery.

No listing images are used. `EmailNotifier` is the active delivery backend.
`KakaoNotifier` is an unconfigured interface placeholder for a future backend;
adding it does not require changing crawling, AI, pricing, or bargain logic.

## Debian setup

```sh
sudo apt-get update
sudo apt-get install -y python3 python3-venv sqlite3
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config/settings.example.json config/settings.json
chmod 600 config/settings.json
```

Edit only the local `config/settings.json`. It is ignored by Git. The example
file contains placeholders only. Runtime databases, backups, logs, locks,
`.env` files, Codex auth/cache data, and SMTP secrets are also ignored.

Codex CLI must already be authenticated for the two AI stages:

```sh
codex login
```

Configure the Gmail app password privately; it is saved mode `600` outside the
repository at `~/.config/used_pc_finder/secrets.env`:

```sh
.venv/bin/python main.py --setup-email
```

Never place a password, token, cookie, `auth.json`, or `secrets.env` in this
repository. Keep `email_notifications.enabled` false until configuration is
verified in your private deployment.

## Normal operation

```sh
# One normal incremental scanner pass. This may send one digest if enabled.
.venv/bin/python main.py --live

# Read-only production health summary.
.venv/bin/python main.py --status

# Run the integrity-checked daily backup operation manually.
.venv/bin/python main.py --backup-database

# Run tests without sending email or doing a market-price backfill.
.venv/bin/python -m unittest discover -v
```

The scanner emits `AI_QUEUE`, `BACKUP`, `PRICE_WARNING`, and, only when severe
price movement coincides with independent pipeline-failure evidence,
`SAFETY_HALT`. Price volatility alone never halts notifications: component
prices can legitimately move quickly.

## Scheduler

The systemd timer invokes only the normal incremental scanner every ten minutes.
It uses locks so an overlapping activation skips safely; it never runs a full
backfill.

```sh
sudo ./scripts/install-systemd-scheduler.sh
sudo systemctl enable --now used-pc-finder.timer

systemctl status used-pc-finder.timer
systemctl status used-pc-finder.service
systemctl list-timers used-pc-finder.timer
journalctl -u used-pc-finder.service -n 200 --no-pager
tail -f logs/production-scan.log

# Manual immediate normal scan; timer remains enabled.
sudo systemctl start used-pc-finder.service
```

## Backup and restore

Before the first normal scan each UTC day, the production wrapper runs SQLite
`integrity_check`, creates `data/backups/listings-YYYY-MM-DD.sqlite3`, and keeps
the seven newest successful backups. Backups are local-only and ignored by Git.

```sh
ls -lt data/backups/
sqlite3 data/listings.sqlite3 'PRAGMA integrity_check;'

# Stop the scanner before restore, preserve the current file, then replace it.
sudo systemctl stop used-pc-finder.timer used-pc-finder.service
cp data/listings.sqlite3 data/listings.before-restore.sqlite3
cp data/backups/listings-YYYY-MM-DD.sqlite3 data/listings.sqlite3
sqlite3 data/listings.sqlite3 'PRAGMA integrity_check;'
sudo systemctl start used-pc-finder.timer
```

Do not commit any `data/`, `logs/`, or local configuration files.
