# Clean database assessment — 25 October 2026

## Scope and decision

The owner reports that Postenza has never publicly launched and has only one
real user. This reduces expected customer impact but does not prove that the
inaccessible Railway databases contain no valuable development/account data.
No Railway database contents have been recovered or inspected.

The owner explicitly accepted a clean database on 25 October 2026 after being
told the source contents are inaccessible and that their old account, Instagram
connection, content and jobs might be lost. They chose to recreate their account
and reconnect Instagram, then reaffirmed fresh PostgreSQL preparation while
preserving all old volumes and backups. This supersedes the earlier requirement
to recover Railway before any fresh staging preparation. It does not authorize
deleting old data, fabricating a recovery receipt, paid hosting or premature DNS.

An isolated **local staging/test database** was initialized to verify all
migrations and workflows. This is preparation, not a Railway restore. A separate
empty staging database is prepared independently of the synthetic test fixtures.
Public deployment still requires a suitable verified free destination and its
acceptance tests. The source-data implications below remain part of the record.

## Local evidence inspected before initialization

- Workspace backup inventory found only the encrypted configuration archive,
  its protected age identity, and `backups/local-postgres-restore-proof/database.dump`.
  The dump is a synthetic local recovery drill, not a Railway export.
- Existing PostgreSQL databases on the local recovery server are `postenza_test`
  and `postenza_restore_test`. Read-only inspection found 23 users in the former,
  all with `@example.test` addresses, no Instagram credentials, one preview
  Instagram account and one content record. The latter has one synthetic test
  user, one billing-usage row and no Instagram accounts or credentials.
- `data/analytics.json` contains one legacy analytics record.
- `data/schedule.json` contains one legacy schedule entry; its referenced media
  file is absent locally. Do not enqueue this old entry on a staging worker.
- A legacy comments text file is also retained in `data/`.
- Configuration exports for both Railway projects, the verified encrypted
  configuration archive, historical encryption keys and original local databases
  remain intact. No existing file or database is overwritten by this preparation.

This inventory covers the project workspace and its local recovery server; it
does not claim an exhaustive search of every disk or cloud account.

## Information a clean launch would not restore

If present only in Railway, the following would be absent from the new database:

- The owner's login/password hash, onboarding state and account preferences.
- Instagram account links and encrypted OAuth credentials; reconnect accounts
  through the normal OAuth flow instead of copying unknown or stale tokens.
- Drafts, generated content, prompts, scheduled jobs, engagement/follow-up
  queues, message history and processing/deduplication state.
- Billing/subscription/usage records. The owner's statement establishes no public
  launch, but billing remains disabled and no historical payment data is assumed.
- Legacy JSON/media files on the Railway application volume. Existing assets at
  Cloudinary may survive independently; their mapping to source records remains
  unknown and must not be called recovered.

The application code, migrations, billing/SEO implementations, local source files
and saved provider configuration can be preserved independently of the database.
New local staging data must not be imported as a production recovery snapshot.

## Staging controls

Use a distinct database name, no publishing credentials, embedded scheduling off,
billing disabled and search indexing disabled. Original production recovery
attestations must not be fabricated to bypass `deploy/update.sh` or backup gates.
This file records the explicit fresh-initialization decision. Any future
clean-initialization automation needs its own verification; the existing
verified-recovery mode remains unchanged and cannot be satisfied with test data.

Oracle deployment is independently blocked by its authenticated capacity report:
A1 2 OCPU/12 GB and 1 OCPU/6 GB return `OUT_OF_HOST_CAPACITY` in Casablanca;
E2.1.Micro returns `HARDWARE_NOT_SUPPORTED`. No VM has been created.

## Completed local initialization and verification

- `postenza_fresh_staging`: all migrations applied through `20261024_01`, 20
  public tables, zero business rows. The sole metadata row is Alembic's version.
- Its logical dump and fingerprints are under ignored
  `backups/fresh-staging-20261025`; `ORIGIN.md` explicitly identifies fresh local
  initialization, not Railway data. Restore into `postenza_fresh_restore_test`
  matched all 20 tables. Dump SHA-256:
  `0a1852e3e3411c32940bf84cce5387f4636c6cfdf6610d1817880a35b089350d`.
- `database.dump.age` was encrypted using the existing protected age identity's
  public recipient, decrypted privately and compared byte-for-byte by SHA-256.
  Old archives and keys were not replaced; no production recovery marker exists.
- Synthetic browser/test fixtures use the separate `postenza_oci_clean_test`
  database. The fresh staging database above has no preview user or queued jobs.
  Sixty-two recovery tests passed with four existing Alembic warnings. Browser
  checks exercised signup/onboarding, login/logout, draft save, future scheduling,
  calendar display and return-to-draft. Billing stayed disabled. No real OAuth,
  Cloudinary upload, Gemini generation or publishing was performed.
