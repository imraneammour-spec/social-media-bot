# Oracle recovery continuation implementation plan

> **For agentic workers:** Use superpowers:executing-plans for inline implementation. Preserve the current checkout and all pre-existing user changes.

**Goal:** Continue the user's approved Oracle migration while production recovery and browser access remain external dependencies.

**Architecture:** Retain the existing single-VM Docker deployment. Use the standard age CLI with an offline decryption identity for encrypted backup files; do not implement cryptography. Add host timers for backups and local health checks, without activating either until destination and source recovery are verified.

**Tech stack:** Existing Python 3.12, PostgreSQL 18, Docker Compose, age, systemd.

**Spec:** User attachment `8bddd20e-1ef4-4fb6-8890-382b10de0416/Texte collé.txt`.

## Global constraints

- Always Free only; trial credit is not authorization for billable resources.
- No production DNS changes, Railway deletions, unreviewed merge, or publishing worker activation.
- Preserve original keys and data; an isolated local drill is never production recovery.
- Browser verification is required before claiming Oracle activation/capacity/cost.

## Review focus

- Encryption failure/tampering must never publish an apparently successful plaintext recovery file.
- Existing outputs and production data must never be overwritten.
- A backup failure must restart only services that were running before backup.
- Staging monitoring must not require or start a publishing worker.
- Host-local encrypted backups do not satisfy independent off-host recovery.

## Tasks

- [ ] Verify Oracle account/region/limits/cost in Chrome, then provision only confirmed Always Free resources. Blocked while local browser use is disabled.
- [x] Check Railway support response and preserve source resources. No response as of this check.
- [x] Add real age round-trip, tamper, wrong-key and overwrite-refusal tests in `recovery_tests/test_encrypted_backup.py`; run them failing before implementation.
- [x] Implement `scripts/encrypted_backup.py`: `encrypt_file(source, output, recipient)` and `decrypt_file(source, output, identity)`, using age subprocesses, protected temporary files and exclusive final publication.
- [x] Add `deploy/backup.sh`, `deploy/monitor.sh` and inactive systemd templates. Capture database, app files and config during a short graceful app stop, retain encrypted archives without automatic deletion, and document restore verification/off-host requirements.
- [x] Run local recovery tests and Linux CI including actual age encryption/decryption; update draft PR #1 without merging.
- [x] Record exact Oracle/production/billing/SEO acceptance status and remaining external actions in `docs/ORACLE_MIGRATION_STATUS.md`.

## Continuation ledger

- 25 October: authenticated Chrome/OCI Cloud Shell now work. Actual capacity
  reports show A1 2/12 and 1/6 OUT_OF_HOST_CAPACITY, E2 Micro unsupported. No VM
  or VCN was created; registration/browser repair is no longer a prerequisite.
- 25 October: Railway Support replied; no offline/free export offered. Owner
  explicitly accepted a fresh database after the unknown data-loss implications
  were explained, preserving every old volume, archive and encryption key.
- 25 October: fresh local staging reached head 20261024_01, 20 tables and zero
  business rows. Snapshot restore matched all tables; age round trip verified.
  Synthetic browser/test fixtures remain in a different database. Local 62 tests
  passed; actual 390px mobile auth/content/calendar/billing behavior was checked.
- 25 October: registration legal links were rendering escaped HTML. A scoped
  template fix escapes URL substitutions and renders the fixed trusted localized
  markup. EN/FR/AR rendering and browser signup/onboarding verified. Linux CI
  run 40202075579 passed on aeb1b1f (same tree as local df9a8be).
- 25 October: owner registered at Serv00 and is awaiting activation. No free
  destination has passed actual runtime/worker/persistence checks. No billing,
  publishing, source deletion, DNS cutover or public launch performed.

- Ruling: keep the existing checkout/branch; pre-existing working-tree UI edits/deletions remain excluded from commits.
- Ruling: implement host orchestration in standard-library Python behind the shell entry points so failure paths can be tested on Windows and Linux.
- Added ten host-operation tests, observed failures before corresponding implementations/fixes, then 62 local recovery tests passed.
- Independent scoped review found scratch-capacity underestimation and a stale-image capture path; both were verified, fixed and regression-tested. Monitoring no longer starts or socket-activates Docker.
- Host units remain inactive; no account/resource/DNS changes. Linux CI run 40137771535 passed all 62 tests and the real named-volume/stopped-container/age/restart drill. Initial CI's fixture-capacity failure was corrected using a small test-only file cap, retaining the production defaults and capacity regression.
- Browser checks confirmed enabled Chrome extension but missing native-host registration and a separate Work denial. A new turn did not fix access; Oracle capacity/cost remain unverified.
