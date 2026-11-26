# Serv00 staging preparation

User specification: prepare the existing recovery branch for Serv00, without
Docker/systemd or paid services, preserving the clean database and all user edits.
Account activation is pending. No DNS, billing or publishing activation.

Design: one Passenger web process and one non-overlapping cron worker tick.
Use an external private JSON configuration and persistent directory, never a
release-local database or data directory. The Serv00 entry points are deliberately
staging-only: worker ticks probe readiness/storage and never execute integrations.
The general worker gains a singleton lock and a one-cycle mode, retaining its
existing continuous mode. Limit each PostgreSQL engine to one connection in the
shared-host profile and reuse the worker's existing auth engine. Native libpq
dependencies and the actual account's aggregate memory remain remote gates.

## Global constraints and review focus

- Preserve Railway volumes, archives, keys and postenza_fresh_staging untouched.
- Do not stage unrelated user changes/deletions or run unchanged browser tests.
- Refuse configuration inside the public website/release and refuse nonempty
  fresh-database targets; never treat a fresh database as recovered Railway data.
- Duplicate cron invocations must not overlap, including after process death.
- Probe health cannot be represented as successful publishing execution.
- No secrets in command arguments/logs, no unbounded restart loops or log files.
- A Linux/Windows simulation is not FreeBSD/Passenger acceptance.

## Task 1: Bound resource usage and make worker cron-compatible

Interfaces: POSTENZA_HOST_PROFILE=serv00; worker main(once, probe), shared DATA_DIR.
Write failing behavioral tests for bounded pools, single-engine worker reuse,
overlapping worker exclusion, task failures and crash/restart lock recovery.
Implement the minimum runtime/worker changes and run these targeted tests.
Expected: tests pass without executing real integrations.

## Task 2: Native deployment and verification

Interfaces: deploy/serv00/config.example.json -> scripts/serv00.py -> Passenger
and cron. Enforce staging settings before imports; preserve external storage;
allow explicit migration only into an empty PostgreSQL database. Generate a
Passenger bootstrap exclusively, print cron lines without installing them, and
verify schema/headers/billing/SEO/storage without creating app accounts/jobs.
Test unsafe configuration, nonempty migration refusal, subprocess restart and
persistence. Measure concurrent web/worker memory in a portable host drill.
Expected: targeted tests pass; actual FreeBSD build and resource use stay pending.

## Task 3: Evidence, review and existing PR

Run the relevant PostgreSQL tests and Linux CI once for the changed runtime.
Run a fresh scoped review under requesting-code-review, fix important findings,
and update draft PR #1 with evidence and exact remaining Serv00 checks. Keep the
existing branch and unrelated work intact. Do not merge or deploy.
Expected: validated local preparation, remote acceptance clearly outstanding.

## Progress

- Pre-flight: Task 1 pool/worker interfaces feed Task 2 staging launcher. The
  launcher always supplies an absolute persistent DATA_DIR before importing Flask.
- Ruling: use the explicitly requested existing checkout. Keep staging probe ticks
  distinct from active queues until the provider is accepted. The cost is that
  real publishing remains intentionally untested and disabled.
- Task 1: complete. Original resource/worker tests were observed failing before
  implementation; singleton exclusion/abrupt kill recovery, failure heartbeat and
  engine reuse now pass. Default VM pool behavior remains unchanged.
- Task 2: complete locally. 16 targeted tests pass (18.60 seconds; one existing
  Alembic warning), including new empty-DB migration, nonempty refusal, native
  verification/probe subprocesses, persisted sentinel and lease recovery. Safe
  config checks and the compressed-image preallocation guard passed RED -> GREEN.
  Memory drill added for Linux/FreeBSD; Passenger acceptance remains remote-only.
- Preservation: read-only check confirms postenza_fresh_staging has 20 tables,
  zero business rows and one Alembic row. Original backup SHA-256 unchanged.
- Task 3: scoped independent review dispatched; Linux CI and PR update pending.
- Final review: one Important finding fixed. FileLock's soft fallback could leave
  a stale marker after abrupt death on filesystems without flock. The worker now
  refuses soft fallback; configuration regression observed RED before the fix.
  An additional Unix ENOSYS fault test covers the actual unsupported-lock path.
- Final: minor (deferred): the loader retains inherited OAuth engagement/keyring
  variables although app secrets are cleared and cron is probe-only. Before any
  live integration deployment, start with a clean environment and explicitly
  disable/remove these process variables. No archived keys are to be deleted.
- Final: Ruling: FreeBSD builds, TLS/Passenger, actual cron/locking and account-wide
  resources remain remote acceptance gates; guessing them could produce a broken
  deployment or exceed limits, so launch stays blocked until measured.
- Final: Ruling: real publishing/token refresh/billing execution was intentionally
  excluded. The cost is unverified integration behavior; no activation is allowed.
- Final: Ruling: historical Railway recovery stays unknown; its assets were not
  accessed by this patch. Treating staging results as recovery would misstate loss.
- Final: Ruling: unrelated UI edits/deletions and unchanged browser tests stay
  outside the patch. The cost is that this backend run does not re-certify those
  uncommitted UI changes; earlier mobile/browser evidence still has its own scope.
- Final: Ruling: CI is established by the actual run, not the reviewer verdict;
  its result is recorded after completion below.
- Final verification: after the important lock fix, the full local recovery suite
  passed 78 tests with one Unix-only test skipped on Windows and five existing
  Alembic warnings (33.20 seconds). Linux CI will run the Unix ENOSYS path and
  concurrent RSS drill. No unchanged browser tests were repeated.
- Task 3: complete. Scoped independent review's Important finding was fixed and
  verified RED -> GREEN; minor environment hygiene is documented for promotion.
  Linux CI run 40204460392 passed all 79 tests (including the Unix fault path),
  native shell syntax, dependency audit, migrations and legacy Docker recovery
  drill. The concurrent staging RSS drill measured 288.0 MiB of sampled peak sum,
  excluding Passenger/uploads/live integrations. Application remote d9ac83b9 and
  local a3506d0 share tree b8f6b2f839dc19723f0cb2ffcb30b8453646465d.
  Existing draft PR #1 receives these validated changes; no merge/deployment.
- Stop: all executable local preparation is complete. Remaining work requires
  Serv00 activation and the remote acceptance checklist; no further speculative
  changes or repeated browser tests are scheduled.
