# Serv00 staging deployment and acceptance

Status: account activation pending, 25 October 2026 UTC. Nothing is deployed.
This package prepares the existing application for shared hosting without Docker,
root, systemd, a self-managed PostgreSQL server or a reserved application port.
It is deliberately staging-only. Billing, indexing, OAuth app secrets, AI/media
API credentials and integration worker execution are disabled by the native loader.
Keep the existing Linux/Oracle deployment package unchanged for a future VM.

## Provider assumptions to confirm on the activated account

The published free offer has 512 MB RAM, 15 processes, 3 GB persistent SSD and
PostgreSQL. Flask uses Passenger behind the provider's nginx. Set Passenger to
**one process**; the separate serial worker uses cron, not Passenger's lifetime.
The website can stop after 24 hours without requests and resume on the next hit.
Cron is supported but actual execution, process accounting and storage locking
must be tested on this account. Do not assume a Linux executable/wheel works.
The worker refuses soft-lock fallback when native locking is unavailable; this
must fail visibly rather than leave a stale marker after a process crash.

Official sources checked 25 October:
[offer](https://www.serv00.com/), [Python](https://docs.serv00.com/Python/),
[Flask](https://docs.serv00.com/Flask/), [cron](https://docs.serv00.com/Cron/),
[PostgreSQL](https://docs.serv00.com/PostgreSQL/),
[WWW options](https://docs.serv00.com/WWW/),
[terms](https://www.serv00.com/terms-of-service/).

## 1. Native environment, without modifying an existing site

After activation, verify SSH host fingerprints, `uname -a`, `python3.12 --version`,
`devil info limits`, quota/free disk and the PostgreSQL server version. Enable
Binexec in DevilWEB. Keep this repository in a **private directory outside
domains/**. Clone the existing recovery review branch; don't upload `.env`,
`backups`, local databases, age private keys, or the user's uncommitted UI work.
Use a read-only deploy key for the private repository and verified release SHA.

Create a private virtualenv and install native dependencies serially:

```sh
umask 077
python3.12 -m venv /usr/home/LOGIN/.virtualenvs/postenza
/usr/home/LOGIN/.virtualenvs/postenza/bin/python -m pip install --no-cache-dir -r deploy/serv00/requirements.txt
/usr/home/LOGIN/.virtualenvs/postenza/bin/python -m pip check
```

If the provider requires `virtualenv` instead, use its documented Python 3.12
interpreter selection. `psycopg` uses the **system libpq**, avoiding a presumed
`psycopg-binary` wheel. Check `import psycopg; print(psycopg.pq.version())`.
Pillow, cryptography and google-genai's dependencies may need native builds.
Use `MAKEFLAGS=-j1`, `MAX_CONCURRENCY=1`, `CPUCOUNT=1` if compilation is needed.
Do not start package compilation while both application processes are running.
If a required dependency cannot build within the account's limits, stop: the
host has not passed acceptance. Do not silently downgrade pinned dependencies.

## 2. Dedicated empty staging database and private configuration

Create a **new** database in DevilWEB PostgreSQL, e.g. `LOGIN_staging`, with a fresh
password. The host is `pgsqlX.serv00.com`, matched to the assigned server. Record
the actual major version; local PostgreSQL 18 backups cannot be presumed portable
backward to an older server. Apply migrations to a new empty database instead.
Keep `postenza_fresh_staging` and its verified local snapshot unchanged.

Copy `config.example.json` to `/usr/home/LOGIN/postenza-private/config.json`;
replace all placeholders, generate a fresh random Flask secret and chmod 600.
Keep its parent/data directories private (700). URL-encode the database password.
Start with `sslmode=require`; verify actual DB encryption and certificate support
on the host. If this fails, inspect the provider's TLS/private transport before
proceeding; do not silently disable TLS. The JSON permits exactly four keys.
It is read explicitly by both Passenger and cron, so shell startup files cannot
silently select different configurations. Never commit this file.

From the private checkout, with the venv Python:

```sh
python scripts/serv00.py migrate-fresh --config /usr/home/LOGIN/postenza-private/config.json --confirm-fresh-database LOGIN_staging
python scripts/serv00.py verify --config /usr/home/LOGIN/postenza-private/config.json
```

`migrate-fresh` requires the exact database name and refuses any existing user
tables/views/sequences. It is not an update command and never drops anything.
It creates the migration schema, **no owner account and no Instagram links**.
This is owner-approved clean initialization, not recovered Railway content.

## 3. Passenger and HTTPS on the provider's staging hostname

Create/select an unused provider staging hostname as a Python site in DevilWEB,
with the absolute venv interpreter. Retain the provider's logs directory. Set:

```sh
devil www options STAGING_HOST processes 1
```

The generated `passenger_wsgi.py` belongs in the site's `public_python` directory;
only that bootstrap is exposed there. The repository, secrets and persistent
data remain elsewhere. Do not copy the whole repository into `public/`.

```sh
export POSTENZA_PYTHON=/usr/home/LOGIN/.virtualenvs/postenza/bin/python
export POSTENZA_CONFIG=/usr/home/LOGIN/postenza-private/config.json
export POSTENZA_PASSENGER_ROOT=/usr/home/LOGIN/domains/STAGING_HOST/public_python
sh deploy/serv00/deploy.sh
```

The wrapper checks dependencies and staging readiness, exclusively creates a new
bootstrap (refuses overwrite), and prints the cron command. It does **not**
install cron, alter websites, change DNS or run active queues. Enable the
provider's free certificate and HTTPS-only setting on the staging hostname.
Proxy headers remain untrusted until the actual forwarding behavior is verified.
Check public `/health`, `/ready`, `/login`, secure cookies, CSRF, redirects,
headers, noindex/robots and private-file inaccessibility over real HTTPS.
Dynamic assets are served by Flask initially; no static copy can accidentally
override staging robots/SEO responses.

## 4. Cron, recovery and resource acceptance

Run `python scripts/serv00.py cron --config /absolute/private/config.json` with
the venv interpreter. Inspect the printed absolute command and add only that
line with `crontab -e`, preserving all existing entries. No shell environment
or working directory is assumed. Normal ticks produce no output, avoiding
growing log files. Errors use class-only messages via the provider's cron output.
Cron runs **once per minute**, so eligibility can be delayed up to a minute plus
queue processing time; long tasks defer subsequent cycles until the lock clears.

Ticks use the real app/database but run **readiness probes only** and update
`worker-probe-heartbeat`. They never claim jobs, refresh tokens, reconcile billing
or publish. `worker-heartbeat` is reserved for actual queue execution. Do not
replace the printed command with the general worker during this staging phase.
The general worker supports `--once` for a later accepted deployment; this
package does not enable it automatically.

Verify two cron invocations cannot overlap; kill only the identified test worker
PID and confirm a subsequent scheduled tick succeeds. Leave a sentinel file in
the external data directory and verify it survives Passenger restart and cron
process restart. Verify recovery after a provider restart when possible, without
rebooting shared infrastructure. Retained SQL leases can be reclaimed after
expiry; ambiguous outbound publishing is not automatically retried.

Run `python scripts/serv00_memory.py --config /absolute/private/config.json`.
It samples separate web/worker processes concurrently, including password hashing.
The sum of sampled peak RSS must stay below **384 MiB**, reserving 128 MiB of the
published account limit for Passenger/provider overhead. Then measure **the actual
account total** with Passenger, SSH/cron and representative safe uploads/mobile
requests. One process per role and one PG connection per process are the baseline;
decoded staging images over 4 million pixels are rejected before allocation.
The drill excludes real AI, story rendering and uploads; those need a separate
resource test before those features can be enabled. Watch the 15-process cap and
disk/inode use as well. Local measurements cannot certify Serv00's accounting.

## 5. Backup, promotion and stop conditions

Use the existing native `scripts/database_backup.py` for a consistent PostgreSQL
dump/fingerprint manifest (database URL through environment, never command-line
passwords). Use a compatible `pg_dump`/`pg_restore` major. Stop staging writers
through the provider panel during combined DB/files/config capture; disable only
this app's cron entry. Archive external persistent files and private config into
a new private path without overwriting old backups. Encrypt using the existing
`scripts/encrypted_backup.py` and an age **public recipient only** on the host.
Download the encrypted artifact off-host; decrypt privately and restore into a
separate empty disposable DB, comparing every manifest fingerprint and file hash.
If age/PG tools or scratch disk are unavailable, do the capture over verified SSH
to a protected local directory; stop rather than treating an incomplete backup
as verified. Never upload the recovery private identity to shared hosting.
Do not copy Docker/systemd backup/monitor scripts to this deployment.

Before public promotion: free eligibility/account limits, native package build,
actual Passenger/cron restart, TLS, measured account memory/process/disk quotas,
outbound connectivity to Meta/Gemini/Cloudinary (read-only requests), independent
backup/restore and provider terms must all pass. The free provider has no SLA;
keep an off-host copy and perform an actual SSH/DevilWEB login within 90 days as
required by its terms. No automatic background browser monitoring is configured.

Existing local auth, billing/security, SEO and 390px mobile checks are recorded
in recovery documentation and are not repeated for unchanged templates. Real
merchant sandbox events, OAuth linking and feature resource tests remain pending.
Deferred environment hardening: the loader currently retains any inherited
`INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED` and `INSTAGRAM_CREDENTIAL_*` variables.
App secrets are cleared and ticks are probe-only, but before enabling integrations
use a clean dedicated process environment, explicitly turn engagement off and
remove inherited keyring variables from that process. Preserve all original
archived encryption keys; this is environment hygiene, not file deletion.
Keep billing/social publishing/indexing disabled and postenza.app DNS unchanged.
Do not submit a sitemap to Google before the public site is live. Stop local
implementation after the checks in this package pass; account approval is the
next dependency.
