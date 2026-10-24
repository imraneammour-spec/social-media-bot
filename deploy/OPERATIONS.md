# Host backup and monitoring setup

These files are prepared, not installed or active on a cloud VM. They target the
reviewed Ubuntu image/operator `ubuntu`; adjust all ownership, SSH and unit-user
settings consistently if selecting a different image. Membership of `docker`
is administrative/root-equivalent access, reserved for the trusted operator.
PostgreSQL remains private in Compose. Do not enable these units until a verified
Always Free destination, real production restore and staging checks exist.

## Encrypted daily backups

Install host `python3` and `age` from Ubuntu packages. The cloud-init template does
this and prepares `/srv/postenza/backups` for the Ubuntu operator, mode 0700.
Keep only the public age recipient on the VM; retain the private identity on two
independent protected recovery copies. Never put that identity in app_data,
GitHub, the app environment, or the VM's backup bundle.

Place a root-owned, mode-0600 `/etc/postenza-operations.env` using
`operations.env.example`. The systemd manager reads it before switching to the
Ubuntu operator. Set the real public recipient. A run needs at least **7 GiB free
scratch space** at the default 2-GiB per-file limit, retaining a separate 1-GiB
application reserve. Aggregate captures must fit below that limit including
archive/encryption overhead. Oversized captures fail, never silently truncate.
Changing the size limit requires rechecking actual disk capacity and free-tier
storage allocation; the script never allocates or purchases storage.

The backup job checks the original verified recovery dump, its SHA-256, manifest,
restore receipt and operator attestation. It takes a maintenance lock shared with
deployment. It stops only web/worker services already running, captures a custom
PostgreSQL dump using the database container's own client, and copies app_data
from the existing stopped web container. It records that container's actual image
reference, includes `.env.production`, and restarts only the previously running
services before packing/encrypting the new captures. Normal failures and handled
termination release scratch space and restore those service states. A forced
kill or power loss can still require operator inspection/restart; never start
the worker by guess after such an interruption.

The public-key-encrypted tar contains `database.dump`, `app-data.tar`,
`environment` and a SHA-256 file manifest. Originals, earlier encrypted backups
and Railway volumes are not deleted. There is no automatic retention deletion.
`last-encrypted-backup.json` records ciphertext completion and explicitly says
`restore_verified:false`. A successful dump catalog/encryption is not a restored
production proof. Schedule a low-traffic window: writer quiescence causes brief
web downtime and pauses publishing while files/database are captured.

After the first manual run succeeds, copy the reviewed service/timer files into
`/etc/systemd/system`, reload systemd, and enable `postenza-backup.timer`. Its
schedule is **03:00 UTC daily**, with up to 15 minutes random delay. This document
does not enable it. Keep encrypted off-host copies on independently verified free
storage; local archives alone do not survive loss/reclamation of the VM. No
off-host storage account, copy, or backup retention policy has been activated.

To test restore, decrypt to a protected new location with
`scripts/encrypted_backup.py`. Verify each manifest hash and restore the SQL dump
into an empty isolated PostgreSQL instance, then compare row counts/content,
check original credential decryption and inspect media/ownership. Extract app
files with safe archive handling and restore UID/GID 10001. Never overwrite live
data or start queues to test restoration. Retain the original source unchanged.

## Read-only host monitoring

`postenza-monitor.timer` runs every five minutes once manually enabled. It checks
local `/ready`, web/PostgreSQL/proxy container health, disk usage (fails at 85%),
and optionally worker health and encrypted-backup age (26-hour maximum). The
monitor does not start Docker/services, restart containers or send notifications.
Staging defaults do not require a worker or a daily backup until those are approved.
After approved production startup and the first successful backup, set
`POSTENZA_MONITOR_WORKER=true` and `POSTENZA_MONITOR_BACKUPS=true` appropriately.

Maintenance locks suppress expected downtime for at most 30 minutes. Failures
appear in the unit's journal and nonzero status. There is no external alerting,
TLS/domain monitoring, or off-host dead-man check yet; verify those separately
before declaring production monitoring complete. Timer syntax and real behavior
on the selected VM remain part of staging acceptance.
