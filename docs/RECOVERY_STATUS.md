# Postenza recovery status — 24 October 2026

**Production remains offline. Production data is not yet recovered or verified.**
No service was upgraded/reactivated, no cloud resource created, no paid service
enabled, no live payment collected, no DNS changed, and no Railway volume deleted.
The code repairs and local verification below are real; they do not constitute a
successful production deployment. The original URL remains https://postenza.app.

**New security dependency:** Railway's 23 October account-specific notice names
this workspace as potentially at risk because its variable names match the
attacker's search pattern. Railway cannot confirm whether its secrets were
decrypted. Its [incident report](https://blog.railway.com/p/preliminary-report-october-security-incident)
recommends rotating sensitive credentials stored before the incident. Preserve
the original recovery evidence/keyring privately, then coordinate API/token,
database-password and session-secret rotation before relaunch. No credential
rotation has been executed. A successful key validation does not establish that
the key was never exposed. Do not overwrite encryption keys or database access
before verified recovery; see the runbook's incident section.

## Confirmed outage and recovery inventory

Authenticated Railway inspection showed an expired trial, inactive Hobby
customer state, both application and PostgreSQL services offline, and no active
deployments. Historical deployments are Removed; latest deployment log requests
returned zero lines. The API reported approximately $3.98 credit balance and
$3.9348 remaining usage credit. Those balances do not demonstrate an active free
subscription or authorization to restart a paid service.

DNS points postenza.app to bkegis6m.up.railway.app. HTTPS succeeds but `/`,
`/health`, `/login`, `/robots.txt`, and `/sitemap.xml` return Railway's
`Application not found` 404, before Flask. Neither inspected project has the
custom domain attached; both list generated Railway domains only. www has no DNS
record. These observations confirm infrastructure/routing failure; application
bugs alone cannot repair this provider-side 404.

| Railway project | PostgreSQL volume | Reported size | Other volume |
| --- | --- | --- | --- |
| precious-peace | ac255ee2-5585-47ab-8b1d-a8acf84b2837 | 104.923136 MB | /app/data, 39.247872 MB |
| devoted-imagination | b406f058-342b-4c39-81d6-f65a7bca5e2c | 119.160832 MB | None listed |

Both PostgreSQL volumes were reported Ready, not pending deletion, deletedAt
null, mounted at /var/lib/postgresql/data. This proves retained volume records,
not intact/readable database contents. The precious-peace backup UI reported no
backups. Database addresses in saved configuration are private-only, with no
public TCP proxy. Documented `railway volume files ... list / --json` refused
both volumes because the owning service has no active deployment. CLI volume
export therefore cannot currently recover the data without an external recovery
path. The [CLI volume documentation](https://docs.railway.com/cli/volume) describes
file operations, but their existence does not override this observed restriction.
Railway documents limited post-trial volume retention; no exact deletion deadline
was verified for this account. [Railway trial terms](https://docs.railway.com/pricing/free-trial).

Four app/database variable exports and empty log captures were saved locally
under ignored `backups/railway-20261024`. They contain sensitive secrets and must
stay private. They are configuration exports, **not production database dumps**.
The two projects have different integration settings: precious-peace has Gemini
and Cloudinary but lacks OAuth/encryption settings; devoted-imagination has
OAuth/encryption settings but lacks Gemini/Cloudinary. Preserve both key sets
until the authoritative database and encrypted credentials are identified.

The immediate external prerequisite is a no-cost Railway export/recovery route
for both PostgreSQL volumes and the app data volume. Railway's documented $0 Free
plan includes $1/month, but this account's actual recovery screen offers only
Hobby activation with $5 upfront; no plan was changed.
[Plan documentation](https://docs.railway.com/pricing/plans).

You approved a public-safe support request and then approved email as the fallback
when Central Station proved unavailable. The identical approved message was sent
to support@railway.com from the signed-in Railway account email on 24 October
2026. Gmail confirmed Message sent and the message was verified in
Sent, including the exact recipient. No project IDs, secrets or files were sent.
No export or support resolution has been received/verified.
[Central Station outage notice](https://status.railway.com/incident/ZEO9X68O).
The request text is in `docs/RAILWAY_RECOVERY_REQUEST.md`; its private inventory
was excluded from the email. Do not choose a blank replacement database or delete
either project.

## Architecture and repairs

Postenza is a Flask/Jinja application with EN/FR/AR workspace UI, SQLAlchemy and
Alembic PostgreSQL storage, Instagram Login and signed Meta webhooks, Gemini,
Cloudinary, durable tenant job queues, and legacy JSON/media on persistent disk.
It needs an always-running worker; a sleeping web service plus an expiring/free
database is not an equivalent migration. Legacy JSON state also prevents a safe
move directly to an ephemeral filesystem or multiple VM replicas.

Implemented changes:

- Added a separate Gunicorn web entry point, independent worker, bounded database
  pools, cross-process file locks, atomic JSON writes, readiness/schema checks,
  private cache/indexing controls, and safe log handling.
- Wired the existing durable tenant scheduled-image publisher into worker and
  embedded scheduler paths. Preserved the explicit prohibition on legacy
  automatic post publishing. Independent queue failures no longer block other jobs.
- Added encrypted token refresh with expiry checks and guarded updates that
  preserve concurrent disconnect/reconnect decisions. Live Meta refresh remains
  untested without a controlled connected account.
- Fixed login exceptions for existing users with no password hash, disabled-user
  sessions, Unicode CSRF/signature comparison failures, percent-containing
  Alembic URLs, and missing landing translation keys.
- Stripped whitespace from environment AI credentials. One saved Railway Gemini
  key failed with InvalidHeader before trimming; a read-only model-list request
  succeeded after trimming and included the configured model. No billable AI
  generation was executed. Integration settings still need reconciliation.
- Pinned current dependencies and created an isolated Python 3.12 environment;
  the original .venv references a missing Python executable and was left intact.
- Prepared non-root Docker, persistent PostgreSQL/app volumes, HTTPS proxy,
  health/restart/log settings, CI, ARM64/AMD64 image builds and a manual SSH
  staging deploy workflow with a production recovery gate.

The staging deploy leaves workers stopped to avoid publishing restored production
jobs. The [deployment runbook](../deploy/RUNBOOK.md) covers source export,
fingerprint restore, environment reconciliation, provider checks, cutover,
backups, and rollback. Nothing has been deployed to a destination yet.

## Hosting decision and actual benefits

GitHub showed this account as student-verified. No new Education benefit has
been redeemed or activated during recovery. Azure was marked claimed in the
catalog, but the signed-in Azure account had no subscription and you confirmed
the benefit was not activated. Azure is excluded from this migration.

| Option | Verified offer/limit | Fit and current decision |
| --- | --- | --- |
| Heroku Education | $13/month for 24 months, non-rolling; application approval required | Requires card; overages and post-credit resources are billed. Not activated under no-paid-services rule. |
| Oracle Always Free | A1 allowance currently 2 OCPU/12 GB equivalent; 200 GB combined boot/block storage | Closest free architectural fit for a VM with durable files and jobs, but you have no account; regional capacity and eligibility unverified. |
| Appwrite Education | Two projects at Pro-equivalent allowance for six months | Useful BaaS benefit, not a drop-in Flask/PostgreSQL/legacy-disk host. No migration rewrite justified before recovery. |
| MongoDB Education | $50 Atlas credit | Does not host the existing PostgreSQL schema. |
| Sentry Education | Student observability offer | Useful optional monitoring after deployment; not hosting. No account activated. |
| GitHub Pages / Codespaces | Static publishing / development environments | Neither supplies the required durable production Flask + worker + PostgreSQL system. |
| DigitalOcean Education | Historical offer ended | Do not rely on obsolete $200 benefit claims. |
| Render free | Free PostgreSQL expires after 30 days; free runtime limits | Unsuitable durable production recovery destination. |
| Koyeb free | 512 MB, 0.1 CPU, no persistent volume/free worker, sleeps | Does not preserve Postenza's current storage/jobs requirements. |
| Neon free | 1 GB/project, 100 CU-hours/month | Useful isolated database/staging option; not a full host. A continuously active 0.25-CU database exceeds 100 CU-hours over a month. |

Sources: [current Education catalog](https://education.github.com/pack),
[Heroku offer and billing FAQ](https://www.heroku.com/github-students/),
[DigitalOcean benefit retirement](https://github.com/orgs/community/discussions/201240),
[OCI limits and reclamation](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm),
[Render service limits](https://render.com/docs/service-types),
[Koyeb instances](https://www.koyeb.com/docs/reference/instances),
[Neon free allowance update](https://neon.com/blog/neon-free-plan-1-gb-per-project).

No verified, provisionable, entirely free production destination is available in
the connected accounts. OCI is a conditional candidate, not the chosen live host.
It may reclaim idle VMs and may lack capacity. A small free VM would be
self-managed with no high availability. Do not upgrade it to paid to obtain
capacity. The prepared container package remains portable.

For comparison only, Heroku Basic web ($7) plus Mini PostgreSQL ($5) could fit
the $13 student allowance with embedded jobs after eliminating durable-file
dependence; separate Basic web/worker plus database totals $19 before other
costs. Credits have not been approved for this account, and paid resource
activation is prohibited. [Heroku prices](https://www.heroku.com/pricing).
Actual new infrastructure expense incurred by this work: **$0**. Existing domain,
Gemini, Cloudinary, Railway and other account billing was not changed or waived.
No hosting credit expiration date can be assigned to an unactivated benefit.

## Billing implementation and outstanding merchant setup

Lemon Squeezy lists Morocco for bank payouts and offers hosted recurring
checkout, signed webhooks, invoices and a customer portal. It is the implemented
provider, subject to merchant approval and store setup.
[Supported countries](https://docs.lemonsqueezy.com/help/getting-started/supported-countries).
Base fees are 5% + $0.50 per transaction; additional international, PayPal and
subscription fees can apply. Non-US bank payouts carry 1%; non-US PayPal payouts
3% capped at $30. These fees matter for pricing; merchant approval/payout access
has not been verified. [Fee schedule](https://docs.lemonsqueezy.com/help/getting-started/fees).

Paddle was evaluated as an alternative merchant of record, with merchant review
and payout constraints; no Paddle account was accessed. Its country and payout
terms require account-specific confirmation before selection.
[Paddle countries](https://www.paddle.com/help/start/intro-to-paddle/which-countries-are-supported-by-paddle),
[payout fees](https://www.paddle.com/help/manage/get-paid/is-there-a-fee-taken-for-payouts).

The new billing page shows current subscription/status, renewal/end date,
monthly usage, configurable Free/Pro/Agency limits, plan choices, secure portal,
payment history and invoices. Monthly/annual variant mapping is configuration.
No commercial prices were invented. Default operational allowances are
Free 1 account/30 content/20 AI, Pro 3/300/500, Agency 10/1000/2000; review them
before launch. Existing users are explicitly grandfathered without claiming they
have paid. New billing text is currently English; existing workspace locales are
preserved and Billing navigation is translated.

Backend access depends on verified provider state and transactional quotas, not
onboarding plan choice or checkout redirects. Webhooks validate raw HMAC,
store/test-mode/variant/checkout ownership, fetch a fresh subscription snapshot,
reject stale updates and persist idempotency. Portal/invoice lookup binds to the
authenticated owner's subscription. No raw card data is stored. Hourly
reconciliation and bounded entitlement renewal protect against missed webhooks.

BILLING_MODE defaults disabled; live mode also requires an explicit approval
flag, which remains false. Missing inputs: approved merchant store, test API
key/store ID, signing secret, four approved product variants, provider webhook
setup and real sandbox checkout/payment events. Automated tests mock the provider;
no end-to-end Lemon Squeezy sandbox payment has occurred.

## SEO, Search Console and organic search work

Added server-rendered feature pages for Instagram image scheduling and comment/
message automation plus a pricing/allowances page. Seven public routes are in
the XML sitemap. Canonicals, social metadata, SoftwareApplication JSON-LD,
internal links and private/staging noindex controls are implemented. Legal
documents use canonical HTTP Link headers; product pages use HTML tags. Unsupported
cross-platform or video publishing claims and invented ratings/prices are absent
from the new pages. Existing landing design and verification tag are preserved.

The authenticated Search Console property is unverified. Its HTML verification
token matches the existing homepage tag. Verification cannot succeed against the
current Railway 404. No successful sitemap submission, indexing request,
indexation or live Core Web Vitals measurement is claimed. No new analytics
tracker was installed. Retry these only after live HTTPS public pages return 200.

Intent research used primary product/help sources from
[Buffer's Instagram guidance](https://support.buffer.com/articles/using-instagram-with-buffer-YSjg2dXFV8)
and [Manychat keyword triggers](https://help.manychat.com/hc/en-us/articles/14281211785884-How-to-use-Keywords-Trigger-in-Manychat).
Target useful queries around Instagram image post scheduling, comment keyword
automation, private reply follow-ups, and AI-assisted engagement. No search-volume
estimate or ranking guarantee was obtained. Publish original walkthroughs with
real screenshots and permission/window limits, add contextual links to the
feature pages, and use actual Search Console query/crawl data after launch to
prioritize content. Measure mobile LCP/INP/CLS on production and optimize measured
image/font/script costs; do not label these metrics passed from local HTML alone.

## Executed verification and limits

| Check | Actual result |
| --- | --- |
| Recovery suite | 46 passed, 4 warnings, including real PostgreSQL concurrent quota enforcement and subscription ordering with/without an already cached ORM row |
| Historical suite from ignored git snapshots | 657 passed, 4 failed, 124 subtests passed; unlimited free limits used to emulate grandfathered access |
| Historical failure assessment | Old exact robots/sitemap expectations, old hard-coded migration head, and AST scheduler harness missing new jobs; current contracts have direct recovery tests |
| PostgreSQL 18 migration chain | All revisions applied successfully to a disposable local database |
| Backup/restore drill | Custom dump restored into a separate empty local PostgreSQL database; 20 tables/3 rows and content hashes matched at snapshot time |
| Production PostgreSQL restore | NOT EXECUTED; no source export available |
| Dependency audit | No known vulnerabilities in resolved requirements at audit time; not a complete security certification |
| Browser, desktop | Login session persisted across workspace/billing/content; created draft, scheduled it, saw calendar entry, then unscheduled it locally |
| Browser uploads | Correctly unavailable without Cloudinary; live upload not executed |
| Mobile emulation | Chrome viewport capability did not change actual viewport; subsequent browser use is disabled in Work settings. Responsive CSS present, genuine mobile verification pending |
| Live social publish/DM/AI | NOT EXECUTED; controlled credentials/provider tests required |
| Real payment sandbox | NOT EXECUTED; provider credentials missing |
| Linux Docker build / CI | GitHub CI passed migrations, 46 tests, dependency audit, image build, Gunicorn container startup and /ready plus /health smoke checks on the subscription-race fix; cloud deployment and ARM build not executed |
| Production URL / HTTPS | TLS valid; application endpoints still provider 404 |
| Google indexing | NOT VERIFIED |

The local proof is ignored at `backups/local-postgres-restore-proof`; its dump
SHA-256 is `43bb7f4cb48e3c6102a4c5733cacc564cab7f340618599ef888d7f5d2f35ada9`.
It contains disposable test data only and must never be relabeled production.
Local evidence/logs are under `.recovery/evidence`,
`.recovery/recovery-test-results.txt`, `.recovery/regression-legacy-results.txt`
and `.recovery/requirements-audit.json`. A localhost preview is available at
http://127.0.0.1:8765 while the recovery preview process is running; it is not a
new production URL.

Verified source references: draft [PR #1](https://github.com/imraneammour-spec/social-media-bot/pull/1),
local application commit `9318374`, equivalent review application commit
`ef46465f2adbbddd327b82d0eaba0cf6322e2e3a` (identical Git tree
`d4fe993542f8563e6330eb30817b9b12c89c5fdf`), and
[successful Linux CI run](https://github.com/imraneammour-spec/social-media-bot/actions/runs/40132454610).
The draft PR has not been merged or deployed. Documentation-only updates after
this revision do not change the validated application code.

The completed Codex Security review covered the immutable recovery commit range
through `4259aeea843c626fb0744c76afc27d02edd462f7`. It found one medium-severity
subscription race: an older valid provider snapshot could overwrite a newer
expiry because SQLAlchemy retained subscription state loaded before the owner
lock. A deterministic two-transaction PostgreSQL test reproduced the failure,
including a caller with a preloaded subscription. The fix resolves owner IDs
without caching mutable subscription state, then locks and refreshes checkout
and subscription rows before validating ownership/timestamps. Both regression
cases failed before the fix and passed after it. This was a local reproduction;
no production exploitation or live payment execution is claimed. Review ran
sequentially without an independent reviewer and excluded pre-existing user
working-tree edits. The sealed scan describes the earlier vulnerable revision;
it has not been rewritten to imply that revision passed.

## Remaining actions and preservation

1. Obtain no-cost Railway source exports and verify production restore plus media
   and credential decryption. This is the first and non-negotiable dependency.
2. Establish an eligible free destination account/capacity under the no-paid rule.
   No account creation or service activation was performed.
3. Validate the container/staging environment, actual mobile layout, integrations,
   backups and rollback, then perform final snapshot and authorized cutover.
4. Supply merchant sandbox credentials/variants and execute real provider tests;
   retain disabled live billing pending pricing/payment approval.
5. Verify Search Console and submit the sitemap once production is working.
6. Complete coordinated credential rotation and review provider/GitHub activity
   in response to the workspace-specific Railway security notice before relaunch.

All pre-existing project edits and deletions remain in place. Changes originally
present in app_translations.py, tenant_dashboard.css and app_content.html are not
included in the recovery commit. User-deleted tests/docs were not restored;
historical tests were read into an ignored harness. Secrets, dumps, logs, local
environments and recovery tools are excluded from Git and Docker context.

Rollback instructions are in `deploy/RUNBOOK.md`. Keep the source volumes and
original keys. Do not downgrade/drop data to make readiness pass. Once a new host
accepts writes, reconcile those writes before routing back to an older snapshot.
