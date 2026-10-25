# Free hosting assessment — 25 October 2026

Constraints: no paid subscriptions/resources, no temporary credit dependence,
Flask + PostgreSQL + durable app files + an independent continuous worker.
Preserve the current branch, user changes, billing/SEO work and all Railway data.
The owner has explicitly accepted a fresh database; see
`CLEAN_DATABASE_ASSESSMENT.md`. No DNS cutover has occurred.

## Oracle: verified account, unavailable host capacity

Authenticated Chrome now works. The console confirms tenancy `imraneammour`,
Free Tier / active Free Trial, in Morocco West (Casablanca). OCI Cloud Shell also
identifies Casablanca as the tenancy home region and returns one AD.

The Ubuntu 24.04 Minimal aarch64 image is listed Free. The A1 shape is labeled
Always Free-eligible. The draft configuration was 2 OCPU / 12 GB, default 46.6-GB
boot volume, no extra block volumes. **This is a draft, not a created server.**
The generic price estimate showed EUR 14.30/month; it was not a confirmed bill or
a verified post-allowance total. No trial-funded resource was created.

The authenticated `CreateComputeCapacityReport` result was:

| Shape/configuration | Reported capacity |
| --- | --- |
| A1, 2 OCPU / 12 GB | OUT_OF_HOST_CAPACITY |
| A1, 1 OCPU / 6 GB | OUT_OF_HOST_CAPACITY |
| E2.1.Micro | HARDWARE_NOT_SUPPORTED |

Root-compartment instance, boot-volume, block-volume and child-compartment list
queries returned no entries. Trial service quotas exceed the free allowance;
they are not evidence of additional free CPU or RAM. No VM/VCN was provisioned.
Evidence screenshot: ignored `.recovery/evidence/oci-casablanca-capacity-20261025.png`.

Published Always Free allowance: A1 1,500 OCPU-hours / 9,000 GB-hours monthly
(2 OCPU / 12 GB equivalent), 200 GB combined boot/block storage, five volume
backups in the home region. Public networking is possible, but no actual public
IP/firewall configuration was created. Idle instances may be reclaimed.
[Oracle limits](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm),
[capacity report](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/compute/compute-capacity-report/create.html),
[networking prices](https://www.oracle.com/cloud/networking/virtual-cloud-network/pricing/).

## Alternative assessment

| Provider | Verified facts | Decision |
| --- | --- | --- |
| Serv00 | Free 3-GB persistent storage, 512-MB RAM, 15 processes, Python/Flask, PostgreSQL, SSH, cron and free TLS | Strongest new candidate for a small staging feasibility test. Actual account/resources, dependency installation, worker supervision/restart, filesystem locking and memory use remain unverified. |
| Alwaysdata Free | 1-GB disk, 256-MB RAM, Python/PostgreSQL and background services; personal use; free sites restricted to alwaysdata.net | Cannot deliver postenza.app on this plan. Registration currently shows a card-validation requirement despite the homepage's no-card claim. Not activated. |
| Northflank Sandbox | Two always-on services, one database, two jobs | Provider says free tier should not be used for production and requires a payment method. Shared persistent app-file storage and an actual zero-cost configuration remain unverified. No card/account/resource added. |
| Render Free | Ephemeral web files, sleeping runtime, PostgreSQL expires after 30 days; no free background worker | Does not satisfy durable full-stack production requirements. |
| Koyeb Free | One web service, no worker or attached volume, sleeps after one hour | Does not satisfy the current requirements. |
| Google Compute Free Tier | US e2-micro and standard disk allowance; external IPv4 billed separately | An ordinary IPv4 VM is not a verified zero-cost alternative. No billing account/resource activated. |

Sources: [Serv00 offer](https://www.serv00.com/),
[Serv00 Python](https://docs.serv00.com/Python/),
[Serv00 PostgreSQL](https://docs.serv00.com/PostgreSQL/),
[Serv00 cron](https://docs.serv00.com/Cron/),
[Alwaysdata plans](https://www.alwaysdata.com/en/offers/),
[Alwaysdata domain change](https://changelog.alwaysdata.com/?tags__name=announcement&year=2025),
[Alwaysdata signup](https://www.alwaysdata.com/en/register/?p=2012),
[Northflank free-tier/billing terms](https://northflank.com/docs/v1/application/billing/pricing-on-northflank),
[Render free limits](https://render.com/docs/free),
[Koyeb instance limits](https://www.koyeb.com/docs/reference/instances),
[Google free allowance](https://docs.cloud.google.com/free/docs/free-cloud-features),
[Google external-IP prices](https://cloud.google.com/vpc/network-pricing).

## Serv00 acceptance gate and manual prerequisite

The registration form is available in Chrome; it was not submitted. The
provider prohibits automated account creation, and its terms require actual
DevilWEB/SSH login within 90 days to avoid deletion. Owner registration/sign-in
was requested. This is an account prerequisite, not a payment request.
[Registration](https://www.serv00.com/register-account/),
[terms](https://www.serv00.com/terms-of-service/).

Before selecting it: verify the account's actual free limits, permitted app use,
supported OS/Python/libpq/cryptography dependencies, PostgreSQL TLS/private access,
shared persistent-file locks, memory below 512 MB with web plus worker, outbound
Meta/Gemini/Cloudinary connectivity, restart supervision, HTTPS and backup restore.
The Linux Docker/systemd package must not be assumed to run on shared hosting.
Use an isolated staging subdomain, no publishing tokens and billing disabled.
Do not change postenza.app until these checks pass. No provider is yet a verified
deployable production destination under the complete set of constraints.
