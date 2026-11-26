# Postenza V1 polish implementation plan

Approved scope: current branch, UI/presenters only. No commit, push, deployment,
Railway changes, migration, delivery or credential changes. Main implementation
and review run inline; the isolated translation catalog was delegated separately.

## Implementation map and verification ledger

- [x] Automation: render a five-step direct-message flow, product-facing mode names,
  reply-based delay help and progressively disclosed custom-message fields.
- [x] Localization: English message IDs with complete French/Arabic catalogs;
  translate static UI plus allowlisted presenter labels/errors, never customer data.
  Read `postenza_locale` with `sociora_locale` fallback. Test all app routes in three locales.
- [x] Navigation: expose Analytics with its own active marker; Message activity;
  move follow-up list/filter/cancel controls to Engagement using existing signed actions.
- [x] Lifecycle: combine safe waiting-interaction summaries with job status,
  timestamps and allowlisted failure explanations. No recipient/provider IDs serialized.
- [x] Analytics/leads: count persisted successful direct interactions and consumed
  recipient replies; show direct interactions read-only alongside editable legacy leads.
  Do not claim all keyword matches/public replies are separately measured.
- [x] Content: retain public HTTPS URLs; add browser validation, preview states,
  caption limit, timezone help, delete confirmation. No upload/storage interface.
- [x] Branding/accessibility: locale fallback, translated aria/confirmation text,
  backdrop display, mobile focus management, RTL and logical spacing.
- [x] Final gate: focused unittest suites, rendered-template/translation checks,
  Python compile and git diff --check. Fix outdated UI assertions, not backend expectations.

## Regression protocol

Write tests in `tests/test_product_polish.py` using the real tenant dashboard fixture.
Verify failing cases before implementation: reply-based automation help, all-locale
navigation, legacy locale fallback, follow-up expiry/waiting rows, cancellation and
private-data exclusion. Run `python -m unittest tests.test_product_polish -q` after
each coherent phase. Existing suites cover tenant/auth/content/scheduler/OAuth flows.

## Approved deferrals

Tenant-safe image uploads need a separately authorized, size/type-validated,
account-scoped media hosting adapter; the legacy Cloudinary uploader is not reused.
Direct interactions have no editable stage/note/analysis model; these remain read-only.
Public-reply totals and all keyword-match totals are deferred because separate
successful-action facts are not durably recorded.

## Baseline

HEAD adb36f9, clean worktree. Audited 140 app/auth tests: 135 passed; five
scheduler/analytics navigation/copy assertions failed. Logout POST and CSRF are valid.

## Recovery and final review

Recovered 22 approved dirty-worktree files without reverting or restarting.
All were intended; catalog coverage, validation, mobile behavior and test gates
were incomplete but intended. No suspicious or unrelated edits were found.

Catalog coverage: 383 explicit messages in each of EN/FR/AR plus 14 existing shared
messages per locale. App templates, finite presenter labels, confirmations,
preview feedback and safe route errors are localized. The signed-in onboarding
screen shares the same locale fallback. User-written captions, notes, usernames,
automation copy and generated analysis remain untouched (except fixed system
fallback labels). Authentication/registration screens outside the authenticated
workspace are not redesigned by this pass.

The custom-interaction metric counts saved follow-up interaction records, not
every successful private reply. This limitation is explicit in Analytics. Read-only
interaction lists identify their bounded latest-25 scope; no editable metadata or
unmeasured public-reply/keyword counts were invented.

Backend-adjacent changes are account-scoped read models, UI formatting and form
validation, localized safe response bodies, cancellation navigation, and removal
of the now-unused follow-up query from the publishing Calendar. No dispatch,
credential, cooldown, idempotency, lease, publishing or Cron code changed.

Test corrections retain real contracts: Calendar publishing labels; Engagement
cancellation location; analytics active navigation; Message activity wording;
Casablanca display times. Existing cancellation, CSRF, ownership and logout
assertions remain active. The two additional full-discovery errors in the older
phase5b1b fixture were reproduced against a clean HEAD archive: missing registered
logout route. Only the test fixture was corrected to register real auth routes,
and its already-stale plural heading expectation was updated to match HEAD.

Verification includes real Flask routes in all three locales, complete catalog and
placeholder checks, rendered JavaScript syntax checks, DOM-contract tests for the
menu/preview/disclosure, and the full Python regression discovery. No live browser
visual audit, Instagram interaction or remote environment change was performed.

## Final results

- Full Python discovery: 502 tests passed, 0 failures/errors (249.696 seconds).
- New product-polish gate: 17 tests passed, included in full discovery.
- JavaScript: 6 tests passed (3 existing dashboard tests, 3 new DOM-contract tests).
- All 17 HTML templates compiled; app routes rendered in EN/FR/AR.
- Catalog completeness, placeholder parity and rendered script syntax checks passed.
- Python compile checks and `git diff --check` passed.
- No remaining known baseline failures. No commit, push, deployment, migration,
  Railway/configuration change or live Instagram interaction.
