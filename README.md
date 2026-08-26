# Posit Connect Security Monitoring System — Phase 1: Inventory Discovery

Discovers every application deployed to a Posit Connect server, records its
owner, URL, bundle, and deployment timestamps, retrieves the package inventory
Connect holds for it, and persists everything to PostgreSQL.

Phase 1 performs **discovery and storage only**. Vulnerability scanning, email
notification, and Microsoft Graph integration are deliberately out of scope.

---

## Requirements

| | |
|---|---|
| Python | 3.12+ |
| PostgreSQL | 12+ |
| Posit Connect | API key with **administrator** rights recommended |

---

## Layout

```
connect-inventory-service/
├── main.py                        # CLI entry point, console report
├── exceptions.py                  # typed exception hierarchy
├── requirements.txt
├── alembic.ini                    # migration config (no credentials)
├── .env                           # your secrets (git-ignored)
├── .env.example                   # template
│
├── config/
│   ├── settings.py                # Pydantic Settings from .env
│   └── logging_config.py          # structured JSON logging
│
├── database/
│   ├── connection.py              # engine, sessions, health checks
│   └── models.py                  # SQLAlchemy ORM models
│
├── clients/
│   └── connect_client.py          # Posit Connect REST client + retry
│
├── repositories/
│   ├── application_repository.py  # upserts
│   └── package_repository.py      # upserts + stale cleanup
│
├── services/
│   └── inventory_service.py       # scan orchestration
│
├── schemas/
│   ├── application.py             # Pydantic schemas
│   └── package.py
│
├── alembic/versions/              # database migrations
├── tests/                         # pytest suite
└── logs/                          # rotating JSON logs
```

Two files sit outside your original tree because the requirements needed a home:
`exceptions.py` (exception handling) and `config/logging_config.py` (structured
logging). `tests/` was added too — see [Tests](#tests).

---

## Before you publish this anywhere

Enable the secret-scanning pre-commit hook — one command per clone:

```bash
git config core.hooksPath .githooks
```

It blocks any commit containing a credential. See [SECURITY.md](SECURITY.md) for
the full pre-publish checklist, and read it before making any repository public:
`.gitignore` does **not** remove secrets already in git history.

---

## Setup

### 1. Install

```bash
cd connect-inventory-service

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
```

### 2. Create the database

```sql
CREATE DATABASE connect_inventory;
CREATE USER connect_inventory WITH PASSWORD 'choose-a-strong-password';
GRANT ALL PRIVILEGES ON DATABASE connect_inventory TO connect_inventory;

-- PostgreSQL 15+ also needs schema rights:
\c connect_inventory
GRANT ALL ON SCHEMA public TO connect_inventory;
```

### 3. Configure

```bash
copy .env.example .env          # Windows
# cp .env.example .env          # macOS / Linux
```

```ini
CONNECT_SERVER_URL=https://connect.yourcompany.com
CONNECT_API_KEY=your-api-key-here

POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=connect_inventory
POSTGRES_USER=connect_inventory
POSTGRES_PASSWORD=your-database-password
```

Generate the API key in Connect under **your user menu → API Keys**. An
**administrator** key is strongly recommended: a publisher key only sees content
it owns or collaborates on, which produces a silently incomplete inventory. The
scan warns when the key is not an administrator.

### 4. Run migrations

```bash
alembic upgrade head
```

Verify, then roll back if needed:

```bash
alembic current                 # show applied revision
alembic upgrade head --sql      # print SQL without executing
alembic downgrade base          # drop the schema
```

### 5. Verify and scan

```bash
python main.py --check          # tests config, Connect, and PostgreSQL
python main.py --limit 5 -v     # smoke test 5 applications with debug logs
python main.py                  # full scan
```

---

## Usage

```bash
python main.py                  # full scan
python main.py --check          # verify everything, then exit
python main.py --limit 5 -v     # smoke test, debug logging
python main.py --guid <guid>    # scan one content item (repeatable)
python main.py --no-packages    # record applications only
python main.py --prune          # delete apps Connect no longer reports
python main.py --db-stats       # show what is stored (no network calls)
python main.py --workers 16     # raise concurrency on a large estate
python main.py --ascii          # ASCII markers if your console can't do ✓
python main.py --create-tables  # create schema without Alembic (dev only)
```

Exit codes: `0` success, `1` completed with per-application failures,
`2` fatal (configuration, connectivity, or database).

### Console output

```
Starting inventory scan...

Found 145 deployed applications.

Processing:
✓ Treasury Dashboard
✓ Risk Analytics
✓ Customer Insights

Inventory Scan Complete

Applications Stored: 145
Packages Stored: 3248
```

---

## Database schema

**applications**

| Column | Type | Notes |
|---|---|---|
| `id` | serial | Primary key |
| `content_guid` | varchar(64) | **Unique** — the natural key |
| `app_name` | varchar(512) | Connect `title`, falling back to `name` |
| `owner` | varchar(256) | Resolved display name |
| `content_url` | varchar(1024) | Derived when the API omits it |
| `bundle_id` | varchar(64) | Coerced to text; Connect returns an int |
| `created_at` | timestamptz | Connect `created_time` |
| `updated_at` | timestamptz | Connect `last_deployed_time` |
| `last_inventory_scan` | timestamptz | When this row was last refreshed |

**packages**

| Column | Type | Notes |
|---|---|---|
| `id` | serial | Primary key |
| `content_guid` | varchar(64) | FK → `applications.content_guid`, `ON DELETE CASCADE` |
| `package_name` | varchar(256) | |
| `package_version` | varchar(128) | Empty string when unpinned |
| `package_type` | varchar(32) | `Python`, `R`, `Quarto`, or `Unknown` |
| `scanned_at` | timestamptz | Scan that last saw this package |

Unique constraint `uq_packages_identity` on
`(content_guid, package_name, package_version, package_type)`.

---

## How idempotency works

A single `scanned_at` timestamp is taken once per scan and threaded through
every write. Rerunning a scan produces identical table contents.

**Applications** —
`INSERT ... ON CONFLICT (content_guid) DO UPDATE`. Existing rows are refreshed
in place; nothing is duplicated.

**Packages** — a two-step reconcile per application:

1. `INSERT ... ON CONFLICT ON CONSTRAINT uq_packages_identity DO UPDATE SET scanned_at`
   — new packages inserted, existing ones just have `scanned_at` refreshed. The
   unique constraint makes duplicates impossible.
2. `DELETE FROM packages WHERE content_guid = ... AND scanned_at < ...`
   — anything not touched by step 1 is no longer in the bundle, so it is removed.

Rows survive across scans rather than being deleted and reinserted, so
`scanned_at` stays meaningful.

Duplicates *within* one manifest are collapsed before the INSERT is built —
PostgreSQL rejects a statement containing two rows with the same conflict key
("ON CONFLICT DO UPDATE command cannot affect row a second time").

---

## Reliability

- **Retry** — `tenacity` retries transient failures (429, 5xx, timeouts, connection
  errors) with exponential backoff and jitter. Permanent failures (401, 403, 404)
  are **not** retried; retrying a bad key only multiplies failed-authentication
  entries in Connect's audit log.
- **A failed package fetch never loses an application.** The application row is
  still written and its previously stored packages are left untouched rather than
  replaced with an empty set. `--prune` treats such an application as still
  present, so a transient error cannot delete it.
- **Batch writes with per-application fallback.** If a batch transaction fails,
  each application is retried individually so one bad row cannot discard the rest.
- **Fail-fast connections.** `DB_CONNECT_TIMEOUT` (default 10s) prevents an
  unreachable database host from hanging the process.
- **Secrets never reach the logs.** Both the API key and the database password are
  `SecretStr`; the loggable view is `Settings.safe_summary()` and database URLs
  render with `hide_password=True`.

---

## Scale

Package inventory costs **one HTTP request per application**, so the scan is
network-bound. It runs in chunks: each chunk's manifests are fetched
concurrently, then written in a single transaction. Progress output stays in
discovery order regardless of concurrency.

Owner resolution uses one paginated `/v1/users` sweep rather than one lookup per
application — on a 700-application estate with 120 distinct owners that is 1
request instead of 120.

Tune with `MAX_WORKERS` (default 8) or `--workers`. Start at the default and
raise it only if your Connect server is comfortable.

---

## Structured logging

`logs/inventory.log` holds one JSON object per line, always at DEBUG, rotated at
10 MB:

```json
{"timestamp":"2026-08-24T15:12:03.412Z","level":"INFO","logger":"services.inventory_service",
 "message":"Inventory scan complete","discovered":145,"applications_stored":145,
 "packages_stored":3248,"failures":0,"duration_seconds":18.4}
```

The console sink is human-readable and goes to `stderr`, so it never mixes with
the scan report on `stdout`.

---

## Tests

```bash
pip install pytest
pytest
```

56 tests covering schema normalisation, the Connect client over real HTTP
(against an in-process fake server), retry boundaries, scan orchestration,
failure isolation, progress ordering, and console formatting.

The tests do **not** need PostgreSQL — the database layer is faked. Repository
SQL is verified by compiling the statements against the PostgreSQL dialect.

---

## Known limitations

- **Package inventory depends on Connect.** Connect records packages from the
  deployment manifest. Static content and content deployed before package
  tracking existed report zero packages — expected, not a bug. The scan logs a
  loud warning if *every* application returns zero, which usually means the
  endpoint is unavailable on your Connect version.
- **API key scope determines visibility.** A non-administrator key produces a
  partial inventory; the scan warns about this at startup.
- **Endpoint availability varies by Connect version.** The client degrades
  gracefully (`?include=owner,tags` falls back to a plain listing), but
  `--check` followed by `--limit 5 -v` is the fastest way to confirm what your
  server exposes.

---

## Next phases (not implemented)

- **Phase 2** — match `(package_name, package_version, package_type)` against a
  vulnerability source (OSV.dev, PyPA Advisory DB, GitHub Advisories).
  `PackageRepository.distinct_package_versions()` gives the work-list size.
- **Phase 3** — notification to owners and administrators.
- **Phase 4** — Microsoft Graph integration.
