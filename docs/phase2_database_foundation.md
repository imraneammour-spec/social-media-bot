# Phase 2: additive database foundation

This phase adds an unused PostgreSQL schema for future tenant ownership. It does
not alter the live JSON persistence path, start a database connection at app
startup, or migrate any production data.

## Ownership model

`User (UUID)` owns one or more `InstagramAccount (UUID)` records. An external
Instagram user ID is unique, but it is not a primary key. Future operational
records must be reached through an account the authenticated current user owns:

`current_user -> owned InstagramAccount -> stories / analytics / leads / settings / jobs`

No current route uses this relationship yet; authentication and route scoping
are deliberately later work.

The initial schema contains only:

- `users` — future identity fields; password hashes remain nullable and are not
  used in Phase 2.
- `instagram_accounts` — account ownership and connection metadata.
- `instagram_credentials` — an intentionally empty future encrypted-credential
  location. The active `INSTAGRAM_ACCESS_TOKEN` remains environment-only.
- `migration_runs` — audit/checkpoint metadata for a future idempotent import.

## Optional configuration and bootstrap

`DATABASE_URL` is optional. If unset, the app continues as before with JSON
files and creates no SQLAlchemy engine or connection. Railway-style
`postgres://` and `postgresql://` URLs are normalized internally to the
`postgresql+psycopg://` driver URL and are never logged.

After a future operator has provisioned a database and applied the Alembic
schema, bootstrap is explicit:

```powershell
$env:BOOTSTRAP_OWNER_EMAIL = "operator@example.com"
$env:BOOTSTRAP_OWNER_NAME = "Optional display name"
$env:BOOTSTRAP_INSTAGRAM_USERNAME = "optional_instagram_username"
.\.venv\Scripts\flask.exe --app webhook bootstrap-owner
```

`BOOTSTRAP_OWNER_EMAIL` (or `--email`) is required. Owner name and username are
optional. The command obtains the existing configured `INSTAGRAM_USER_ID`, is
idempotent, creates no credential record, and refuses to reassign an Instagram
account already owned by a different user. It does not read, move, or store the
access token.

## Future database commands (not run in this phase)

For local development or a Railway one-off task, set a secret `DATABASE_URL`
outside source control, then run:

```powershell
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\flask.exe --app webhook bootstrap-owner
```

For a future approved schema change, generate and review a new migration locally
before applying it:

```powershell
.\.venv\Scripts\alembic.exe revision --autogenerate -m "describe approved change"
.\.venv\Scripts\alembic.exe upgrade head
```

The migration is reversible with `alembic downgrade base` only before later
database-backed features depend on these tables. Never run either command
against production without an approved backup and change window.

For Railway later: provision PostgreSQL, set `DATABASE_URL` as a service secret,
deploy the dependency-only code, run `alembic upgrade head` as a one-off command,
set the bootstrap values as secrets, then invoke `bootstrap-owner` once. Verify
the resulting ownership mapping before any separate feature migration. None of
those actions occurred in Phase 2.

## JSON-to-future-table mapping (documentation only)

| Current JSON file | Future database destination |
| --- | --- |
| `posts.json` | `media_assets`, `prompt_sets`, `prompts` |
| `processed_comments.json` | `processed_comments` |
| `requests.json` | `prompt_requests`, `leads` |
| `pending_access.json` | `pending_accesses` |
| `smart_reply_activity.json` | `smart_reply_events` |
| `followups.json` | `followups` |
| `stories.json` | `stories`, `story_attempts` |
| `auto_story_settings.json` | `auto_story_settings` |
| `dashboard_drafts.json` | `post_kits` or a legacy import/archive table |

Those operational tables, import routines, dual-write logic, and read-path
switches are intentionally absent. Existing file names, schemas, and readers /
writers remain untouched.

## Credential security boundary

Before writing any token to `instagram_credentials`, a later phase must provide
application-level authenticated encryption, key rotation/versioning, access
control, redacted logging, and a tested token-refresh lifecycle. Storing a
plaintext environment token in PostgreSQL is prohibited.
