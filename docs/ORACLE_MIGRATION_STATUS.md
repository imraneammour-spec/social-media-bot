# Oracle migration continuation — 24 October 2026

The user reports completing Oracle registration/card verification and selecting
Morocco West (Casablanca). This supersedes the previous "no Oracle account" status.
Account activation, actual trial balance/expiration, Always Free eligibility,
regional capacity, networking and total cost have **not yet been inspected**.
The browser tool currently rejects both inventory and direct Chrome access with
"Local browser use is disabled in Work settings", including after the user
confirmed enabling access and the browser connection was reset. This is a tool
access blocker, not evidence that the user's Chrome extension is disconnected.
No Oracle resource, account upgrade or billable service has been created.

Read-only Browser-plugin diagnostics subsequently confirmed Chrome running and
its extension enabled. The expected Windows native-host registration is missing;
this persisted after the extension was installed/pinned and its sidebar opened.
The separate Work access gate still rejects this conversation. See
`BROWSER_ACCESS_DIAGNOSTIC.md` for observations and the required plugin-UI repair.
A fresh message/session did not fix access. No cookies, registry changes,
permission bypass or unsupported browser control was attempted.

Oracle's [current official allowance](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
lists A1 1,500 OCPU-hours/9,000 GB-hours monthly (2 OCPU/12 GB equivalent),
200 GB total boot/block storage and five volume backups. These published limits
are not proof of this tenancy's eligibility or Casablanca availability.
Only resources explicitly verified within Always Free limits may be created;
the [30-day trial credit](https://www.oracle.com/cloud/free/faq/) must not be used
to justify a resource that becomes billable. Do not upgrade to Pay As You Go.

The existing recovery branch and draft PR #1 are preserved. No new Railway
support reply was returned by the Gmail search. Production export, restore,
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
ten unit cases remains simulated; no actual cloud VM backup has run. The exact Railway
Gmail thread was read: it contains only the approved sent request, dated
18:26:58 UTC, with no support reply or export.

## Acceptance status

- Infrastructure resources provisioned: none; new infrastructure spending: $0.
- Verified prospective Oracle monthly total: unknown until the console is inspected.
- Railway production backup and restore: blocked; no export received.
- Staging URL: none. Production URL remains https://postenza.app, previously provider 404.
- Application CI: [run 40137771535](https://github.com/imraneammour-spec/social-media-bot/actions/runs/40137771535) passed 62 tests, migrations, dependency audit, shell syntax, Docker build, Gunicorn startup/readiness and the named-volume encryption drill on review revision `d1b8f83064f6f2a70eea1d4b6a53d8d2b72ea47f` (same Git tree as local `9790642`). Initial CI revealed the tiny backup fixtures unnecessarily using the production-sized scratch cap; fixtures now use a 4-MiB cap, while production's 2-GiB file limit/7-GiB scratch gate and the dedicated insufficient-space regression are unchanged.
- Billing: disabled; real merchant sandbox credentials/events still required.
- SEO/Search Console: code prepared; ownership/indexing and live performance unverified.
- Credential rotation: preserve originals for recovery; coordinate replacement after verified restore.
- Rollback: source volumes untouched; no DNS cutover or production migration performed.
