# Oracle migration continuation — updated 25 October 2026

Authenticated Chrome access now works. The console verifies tenancy
`imraneammour`, a Free Tier account with an active Free Trial, and Morocco West
(Casablanca). Cloud Shell confirms the same home region and one availability
domain. Registration was not repeated. Earlier browser-blocker observations in
`BROWSER_ACCESS_DIAGNOSTIC.md` are historical and superseded by this successful
connection; no manual registry or browser-authentication changes were performed.

The console lists Ubuntu 24.04 Minimal aarch64 as Free and A1 as Always Free-
eligible. A draft 2-OCPU/12-GB instance with the default 46.6-GB boot volume was
prepared, **not created**. The authenticated capacity report returns
`OUT_OF_HOST_CAPACITY` for both 2 OCPU/12 GB and 1 OCPU/6 GB, and
`HARDWARE_NOT_SUPPORTED` for E2.1.Micro. Root instance/storage/child-compartment
list queries returned no entries. Trial quotas are not additional free allowance.
No VM/VCN, account upgrade or billable service was created. Exact trial balance
and expiration were not needed to justify spending and were not used.

Oracle's [current official allowance](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
lists A1 1,500 OCPU-hours/9,000 GB-hours monthly (2 OCPU/12 GB equivalent),
200 GB total boot/block storage and five volume backups. These published limits
are not proof of this tenancy's eligibility or Casablanca availability.
Only resources explicitly verified within Always Free limits may be created;
the [30-day trial credit](https://www.oracle.com/cloud/free/faq/) must not be used
to justify a resource that becomes billable. Do not upgrade to Pay As You Go.

The owner explicitly approved fresh PostgreSQL preparation after accepting the
unknown Railway-data implications, then requested investigation of other free
hosts because Casablanca capacity is unavailable. See
`CLEAN_DATABASE_ASSESSMENT.md` and `FREE_HOSTING_ASSESSMENT.md` for that decision,
capacity evidence, verified limits, alternatives and the remaining account gate.

The existing recovery branch and draft PR #1 are preserved. Railway Support
replied on 25 October 2026 at 07:40:56 UTC: offline volume export is unavailable;
it proposed paid-plan reactivation, which has not been performed. Its stated
30-day post-trial retention interval and September expiry emails create an urgent
retention uncertainty; see `RECOVERY_STATUS.md` for evidence and limits. No free
export or exact deletion deadline was supplied. Production export, restore,
media checks and original credential decryption remain unverified. All original
Railway resources and keys must remain intact.

## Independent implementation

`scripts/encrypted_backup.py` uses the standard age CLI for backup files. It
never replaces existing output and publishes decrypted files only after age
successfully authenticates the complete ciphertext. Failed partial outputs are
removed. Six integration cases passed locally with the official age 1.3.2 Windows
release: round trip, restricted POSIX permissions (Linux check), encrypt/decrypt
overwrite refusal, altered/truncated ciphertext, and wrong decryption identity.
The POSIX permission assertion requires Linux CI; Windows permissions inherit
the destination directory's NTFS ACL. The complete local recovery suite passed
62 tests with four existing Alembic deprecation warnings.

An encrypted archive of the six existing Railway configuration/log export files
was actually created at ignored `backups/railway-configuration-20261024.zip.age`.
It was decrypted privately and all six files matched the original bytes. Originals
were retained. The age identity is at ignored
`backups/recovery-keyring/railway-config-backup.agekey` under the restricted NTFS
backup directory. It has not been sent to a cloud server or committed. Copy it
to independent protected offline storage before relying on this archive. This
is configuration recovery only, **not a production PostgreSQL backup**. The
verification receipt is `backups/railway-configuration-encryption-verification.json`.

After generating an age identity on an offline recovery machine, retain two
protected copies of that identity independently. Put only its public recipient
on the destination VM. Example commands (replace paths and public recipient):

```sh
python scripts/encrypted_backup.py encrypt private-backup.tar backup.tar.age --recipient age1...
python scripts/encrypted_backup.py decrypt backup.tar.age restored-backup.tar --identity /private/offline-identity.txt
```

Decryption success does not establish database integrity: subsequently use the
fingerprint restore workflow in `deploy/RUNBOOK.md`, verify application files and
test credential decryption. No backup scheduler, off-host copy, monitoring timer,
staging host, firewall change, GitHub deployment secret, or publishing worker has
been activated. Inactive daily backup and five-minute monitor units are now
implemented; see `deploy/OPERATIONS.md`. The 62-test local suite passed, including
ten host-operation cases (Docker boundary simulated; age is real). Capture
failures release scratch space before restoring only previously running app
services. Scratch-capacity and stale-image capture defects were fixed after an
independent scoped review. Linux CI subsequently passed all 62 tests and a real
Docker named-volume capture from a stopped container, age encryption/decryption,
byte/content checks and restarted readiness. Docker service orchestration in the
ten unit cases remains simulated; no actual cloud VM backup has run. The Railway
Gmail thread now contains the approved request sent on 24 October at 18:26:58 UTC
and the support reply received on 25 October. No production export was attached.

## Acceptance status

- Infrastructure resources provisioned: none; new infrastructure spending: $0.
- Verified prospective Oracle monthly total: no provisioned server; no new spend.
  A1 is eligible within the published allowance but current capacity is unavailable.
  The generic estimate was not a verified net bill and no trial-funded deployment ran.
- Railway production backup and restore: no export received; no source modification
  or deletion performed. Current source contents remain unverified.
  Fresh initialization is now explicitly approved, not represented as recovery.
- Fresh local PostgreSQL: `postenza_fresh_staging`, 20 tables at `20261024_01`,
  zero business rows. Snapshot under ignored `backups/fresh-staging-20261025`
  restored into `postenza_fresh_restore_test`; every row count/hash matched.
  Dump encrypted/decrypted with age and bytes verified. No recovery approval marker
  was created. Historical backups and original keys remain unchanged.
- Current local checks: 62 recovery tests passed with four Alembic warnings after
  the registration-link template fix. Separate synthetic browser database:
  `postenza_oci_clean_test`. Login/logout, new registration/onboarding, draft save,
  scheduling/calendar and cancellation were exercised; mobile width 390 px had no
  horizontal overflow on overview/content/calendar/billing. Chrome's viewport
  override did not apply, so actual mobile geometry was verified in the in-app
  browser instead. No publishing worker or real integration calls were activated.
- Latest Linux CI: [run 40202075579](https://github.com/imraneammour-spec/social-media-bot/actions/runs/40202075579)
  succeeded on remote `aeb1b1f7c86a45fbfa725e99aaafe4751ae15977` (same tree as
  local `df9a8be`): tests, migrations, audit, shell checks, Docker build,
  readiness/health and stopped-container named-volume/age/restart drill.
- Free-host fallback: owner registered at Serv00 and is awaiting activation.
  Gmail returned no Serv00 message at this check. Runtime/account free limits,
  worker supervision, dependency compatibility and storage remain unverified.
- Staging URL: none. Production URL remains https://postenza.app, previously provider 404.
- Application CI: [run 40137771535](https://github.com/imraneammour-spec/social-media-bot/actions/runs/40137771535) passed 62 tests, migrations, dependency audit, shell syntax, Docker build, Gunicorn startup/readiness and the named-volume encryption drill on review revision `d1b8f83064f6f2a70eea1d4b6a53d8d2b72ea47f` (same Git tree as local `9790642`). Initial CI revealed the tiny backup fixtures unnecessarily using the production-sized scratch cap; fixtures now use a 4-MiB cap, while production's 2-GiB file limit/7-GiB scratch gate and the dedicated insufficient-space regression are unchanged.
- Billing: disabled; real merchant sandbox credentials/events still required.
- SEO/Search Console: code prepared; ownership/indexing and live performance unverified.
- Credential rotation: preserve originals for recovery; coordinate replacement after verified restore.
- Rollback: source volumes untouched; no DNS cutover or production migration performed.

## Serv00 continuation — local preparation

See `deploy/serv00/README.md` for the native staging path and remote acceptance
checklist. Docker/systemd are not required there. Targeted tests passed 16/16
with one existing Alembic warning. These tests use a new disposable PostgreSQL
database and separate test files, not the approved clean staging DB. A read-only
preservation check confirmed its 20 tables, one metadata row and zero business
rows, and the previously recorded dump SHA-256 still matches. Historical Railway
volumes, backups and keys were not modified.

Staging cron executes readiness/storage probes only, with a distinct heartbeat;
publishing and billing remain disabled. Resource checks include concurrent native
web/worker RSS and a 384-MiB application budget, but actual Serv00 memory and
Passenger/dependency compatibility cannot be accepted before account activation.
Existing mobile/browser flows were unchanged and not repeated for this work.
After the scoped review's native-lock fix, the complete local recovery suite
passed 78 tests, with the Unix ENOSYS test skipped on Windows and five existing
Alembic warnings. Independent review found no Critical issue; inherited OAuth
engagement/keyring cleanup remains a documented minor before live promotion.
