# Postenza recovery and migration runbook

Status on 24 October 2026: preparation only. Railway production PostgreSQL has
NOT been exported. Do not use the local test dump to satisfy the production gate.
Do not create a paid resource, reactivate Railway Hobby, change DNS, or remove a
source volume under the current authorization.

## 1. Recover the source before provisioning a replacement

Two Railway projects have retained PostgreSQL volumes. Preserve both until their
contents identify the authoritative production database. The application volume
also contains legacy JSON state and media and must be recovered. See
`docs/RECOVERY_STATUS.md` for the inventory and observed export failure.

The authenticated Railway CLI cannot browse/download these volumes while their
services have no active deployment. The saved database addresses are private-only.
The next external prerequisite is a Railway-provided export, or a provider-approved
temporary recovery route that incurs no charge. Request database and `/app/data`
exports for both projects where present, plus their PostgreSQL major versions.
No support message has been sent on your behalf. Preserve the original files and
obtain their checksums. A raw PostgreSQL data directory must first be started in
an isolated environment using its matching PostgreSQL major version; never attach
it to an arbitrary newer server. Prefer a logical custom-format dump.

When SQL access becomes available, install PostgreSQL client tools matching the
source major (or a supported newer pg_dump). Use a protected environment/session
for DATABASE_URL; never paste credentials into command history, documentation,
GitHub Actions logs, or screenshots. From a private recovery machine:

```sh
python scripts/database_backup.py backup backups/production-recovery
```

This takes a repeatable-read exported snapshot shared by pg_dump and the public
table fingerprints. It creates a custom dump, SHA-256 checksum, per-table row
counts and content hashes. Back up application files separately, with a file
manifest and archive checksum. Keep at least two encrypted copies on independent
storage. Preserve ALL credential encryption keys and the Flask signing secret.
The ignored variable JSON files under `backups/railway-20261024` are sensitive
configuration backups; they are not database backups and are not encrypted.

## 2. Verify recovery in isolation

Create a new empty PostgreSQL database on an isolated recovery server, with no
worker running. Set RESTORE_DATABASE_URL privately to that database. Then:

```sh
python scripts/database_backup.py verify backups/production-recovery
```

Restore refuses a destination containing public tables, uses one transaction,
and stops on errors. Check the generated restore-verification.json against the
manifest. Inspect users, account ownership, content, jobs, credential decryption
and representative media manually. Reconcile both Railway databases before
selecting one; do not merge identities or assume the largest volume is current.
Retain the original snapshot and key inventory unchanged.

Do not run Alembic on the source as part of recovery. Restore a second copy for
staging and upgrade that copy. The new billing migration grants pre-existing
users explicit unlimited legacy access. New accounts receive configurable free
allowances. Never downgrade billing after real billing data exists.

## 3. Destination readiness

No cloud account/resource was created. The container package supports x86-64 and
ARM64 Linux VMs. OCI Always Free is a conditional candidate, not an available
account or guaranteed capacity. Verify the Always Free label, home region,
resource quotas, public IP/storage costs and account restrictions before creating
anything. Do not upgrade to Pay As You Go. `cloud-init.yml` is an Ubuntu tools-only
template; review it against the selected image. Ubuntu's packaged PostgreSQL
client may be older than the source: use an appropriate matching client for dumps.

Use SSH keys, limit port 22 to the operator, expose only ports 80/443 publicly,
and leave PostgreSQL private. Run a single web process (four threads) plus one
worker sharing the same local app_data volume. File locks support multiple
processes on this VM; the legacy file state is not ready for multiple VM replicas.
Do not horizontally scale before moving that state to transactional/object storage.

Place the reviewed checkout at `/srv/postenza`. Copy `.env.example` into ignored
`.env.production`, restrict it to the operator, and fill values from the verified
source configuration. Database example shape:
`postgresql+psycopg://postenza:<URL-encoded-password>@postgres:5432/postenza`.
POSTGRES_PASSWORD must be the same underlying password. Preserve encryption key
IDs exactly and include every historical key, not just PRIMARY. Configure a
separate staging hostname, SEARCH_INDEXING_ENABLED=false, BILLING_MODE=disabled,
POSTENZA_EMBEDDED_SCHEDULER=false, and no publishing credentials on staging.
TRUST_PROXY_HEADERS=true is valid only behind the supplied private reverse proxy.

Compose uses PostgreSQL 18 and Caddy-managed HTTPS. An older source can be
logically restored after checking extension/version compatibility; never copy
its raw data directory into the PostgreSQL 18 volume. Import verified files into
app_data with UID/GID 10001 and check permissions before starting web.

The database is a named persistent volume, web binds only to 127.0.0.1:8000,
and PostgreSQL has no published port. Docker, TLS, ARM builds and cloud-init have
not been executed on this Windows machine; validate them on the chosen staging VM.

## 4. Build, stage, and verify

CI runs the isolated recovery tests, a real PostgreSQL quota check, compile,
dependency audit and app image build. The image workflow produces immutable
Git-SHA tags for linux/amd64 and linux/arm64. Do not deploy a moving latest tag.
For a private GHCR image, provide the destination with read-only package access;
no registry credentials have been provisioned here.

Install the verified production snapshot under
`/srv/postenza/backups/production-recovery`, including the manifest and restore
receipt. After checking the production origin and representative restored data,
write its dump SHA-256 alone into
`/srv/postenza/backups/PRODUCTION_RECOVERY_APPROVED`. This file is a deliberate
operator attestation, not generated by the local test drill. Do not create it
until the real production recovery is verified. Retain a fresh pre-migration
backup before every later deployment; the script's gate does not replace that.

Configure GitHub's staging environment secrets POSTENZA_SSH_KEY,
POSTENZA_KNOWN_HOSTS (verified host key), POSTENZA_SSH_HOST and POSTENZA_SSH_USER.
The deploy workflow is manual and validates an immutable GHCR image reference.
It stops the worker, applies migrations, starts web, requires /ready success,
then starts the HTTPS proxy. It deliberately leaves the worker stopped on
staging. A failure requires inspecting logs and reverting the image; never
drop/reinitialize the database to obtain a green health check.

Verify /health and /ready separately. /ready requires the exact Alembic head and
an accessible users table. Test signup/login/logout/session expiry, per-user
ownership, content creation/editing, timezone conversion, scheduling/canceling,
uploads, settings, error states, and mobile layouts. Use provider sandbox keys
for billing and controlled test accounts for Meta. Confirm expired token refresh,
signed webhook rejection/retry/idempotency, queue claims, and no duplicate sends.
No production publishing/DM or billable AI generation was performed during this
recovery. Live integration tests remain required.

## 5. Billing activation remains separate

Provision an approved Lemon Squeezy merchant store and sandbox API key, store ID,
signing secret, and the four monthly/annual variant IDs. Match actual approved
prices and validate the configured operational allowances before launch. Point
the sandbox subscription webhooks at `/billing/webhook`. Follow the provider's
test-mode checkout/payment instructions and verify renewals, failures,
cancellation, portal changes and invoice downloads against real sandbox events.
The automated suite uses mocked provider responses; it is not a provider sandbox
payment receipt. Live billing remains disabled and requires explicit commercial
approval; do not set BILLING_LIVE_APPROVED=true under the present instructions.

## 6. Cutover and rollback

Proceed only after production backup/restore and staging tests pass. Quiesce
source writes and workers, take/verify the final consistent snapshot and app file
archive, restore the destination, and retain the source untouched. Configure
PUBLIC_BASE_URL, the exact Meta OAuth callback and webhook endpoint, signing
secrets, Cloudinary access and the preserved encryption keyring. Obtain a valid
certificate and test the destination before DNS cutover.

Check the DNS owner/registrar and configure both apex and the chosen www redirect;
www currently has no DNS record. Switch DNS only after these checks. Verify live
registration/login/media/webhooks and due-job execution, then start exactly one
worker using `docker compose --env-file .env.production -f deploy/compose.yml up -d worker`.
Watch heartbeat age, /ready, queue failures, disk usage and backup freshness.
Configure encrypted daily logical backups plus app file backups and periodically
restore them on separate storage. No scheduled backup job or monitoring account
has been activated because there is no destination yet.

Keep the old image, source config and Railway volumes for rollback. If validation
fails before accepting writes, stop destination jobs and revert routing/image.
After new writes, reconcile/export destination changes before reverting; blindly
pointing back to the source loses data. Prefer rolling back code while retaining
the migrated schema; destructive downgrade is not a default rollback step.

Only after public production pages return 200 should indexing be enabled, Search
Console verification retried, the sitemap submitted and URL inspection performed.
The matching existing HTML verification tag is preserved. Do not claim successful
Google indexing without Search Console evidence.
