# Single-VM GCP deployment

This directory prepares a single Debian 13 or Ubuntu 24.04 VM in `project-471d0dfb-dcea-4f41-ba2`. It does not provision cloud resources, change IAM/firewalls, configure LINE, send messages, or upload credentials. Domain and SMTP settings are deployment prerequisites; the production guards remain enabled.

## Proposed infrastructure and cost

The reviewable specification is [vm-plan.json](vm-plan.json): one `e2-standard-2` VM (2 vCPU, 8 GiB RAM) in `asia-east1-b`, a 50 GiB `pd-balanced` boot disk with automatic deletion disabled, and a reserved external IPv4 address. The console quote observed during preparation was USD 56.64/month for compute plus USD 5/month for the disk: **USD 61.64/month before IPv4, egress, off-VM backups, domain, SMTP and taxes**. Recheck the selected region and current [Compute Engine pricing](https://cloud.google.com/products/compute/pricing/general-purpose) before creation. This is a small-pilot assumption, not a capacity test or fixed invoice.

ClamAV alone needs substantial memory; its [official Docker guidance](https://docs.clamav.net/manual/Installing/Docker.html) recommends about 3–4 GiB. The 8 GiB VM leaves room for PostgreSQL, the API, Caddy, workers and builds. Watch memory, disk and scan-queue growth before increasing workload. One VM and local named volumes provide persistence across container/VM restarts, not high availability or disaster recovery.

Only Caddy publishes TCP 80/443 and UDP 443. API 8000, PostgreSQL 5432 and ClamAV 3310 remain on the Docker network. Use a dedicated VPC/subnet from the plan. Restrict SSH to approved administrators through OS Login and [IAP](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding), whose IPv4 source range is `35.235.240.0/20`; do not add a public `0.0.0.0/0` SSH rule. The application needs no GCP API permissions. Check that the private Docker subnet `172.30.70.0/24` does not overlap an existing host network before activation.

## Service layout

`db`, `clamav`, `api`, `maintenance` and `caddy` run by default. Maintenance scans files, processes stored follow/unfollow metadata and builds requested exports; it never invokes case notification delivery. `notification-worker` requires both the `notifications` profile and `YOUTH_ENABLE_CASE_NOTIFICATIONS=true`.

The API runs exactly one Uvicorn worker, with access logs disabled. Its live LINE reply runtime, if explicitly enabled in private settings, owns the same process-local buffer as the webhook. Do not scale it to multiple processes/replicas. Restarting can lose short-lived conversations and unconsumed handoffs; durable reply receipts prevent a claimed reply from being replayed. Maintenance uses a 2-second poll; the API reply runtime can use `YOUTH_WORKER_POLL_SECONDS=0.5`.

Caddy access logs are not enabled, so one-time handoff URLs are not copied into a proxy request log. Caddy blocks simulator, developer API documentation and health routes at the public edge. Production also disables demo rules and the simulator. The public webhook is `https://YOUR_DOMAIN/api/v1/webhooks/line`, which is the path `app/notifications.py` registers; signature validation still applies. A loaded page or an accepted webhook is not evidence of working email login, LINE replies or financial processing.

## Source transfer without Git

From the repository, make a source archive in a separate working directory:

```powershell
python deploy/gcp/create_package.py --output C:\path\to\work\youth-release.tar.gz
```

The script prints its SHA-256 and file count. It uses an explicit source allowlist, normalizes archive metadata and excludes `.env`, private key/database/mail files, `var`, Git, dependency folders and browser fixtures. It does not use `git archive`, so approved local changes are included. Rebuild the archive after the final source changes. Do not add runtime settings to it.

After the VM and approved SSH access exist, transfer that archive through your approved SSH/IAP path; for example:

```bash
gcloud compute scp youth-release.tar.gz youth-service-prod:/tmp/youth-release.tar.gz \
  --project project-471d0dfb-dcea-4f41-ba2 --zone asia-east1-b --tunnel-through-iap
```

On the VM, compare `sha256sum /tmp/youth-release.tar.gz` with the locally printed digest. Create a new, immutable release directory (for example `/opt/youth-service/releases/r20260919-<digest-prefix>`) and extract there with `tar --no-same-owner --no-same-permissions`. The archive contains no symlinks. Run the scripts with `bash` if your transfer mechanism did not preserve executable bits; systemd needs `compose.sh` executable, so set `chmod 0755 deploy/gcp/*.sh` in the extracted release.

## Initialize and activate

The bootstrap script installs Docker from its official Debian/Ubuntu apt repository if Docker is absent, enables Docker at boot and installs systemd units. It neither creates a VM nor formats/moves a disk. An existing incompatible Docker installation needs operator review; the script does not uninstall it. See [Docker's installation guidance](https://docs.docker.com/engine/install/ubuntu/) for host prerequisites.

```bash
cd /opt/youth-service/releases/YOUR_RELEASE
sudo bash deploy/gcp/bootstrap_vm.sh
sudo python3 deploy/gcp/init_env.py --domain YOUR_DOMAIN
sudoedit /etc/youth-service/runtime.env
```

`init_env.py` creates root-private settings once, generates unique local database/session/TOTP keys and prints none of them. It refuses to overwrite an existing file. Keep the TOTP encryption key and session secret in your approved secret backup; replacing them can invalidate sessions or make enrolled authenticators unusable. Settings must be owned by root with mode 0600 or 0400. Never run unredacted `docker compose config`, echo the env file, or paste credentials into a terminal transcript.

Complete a real SMTP host, TLS settings, sender address and any required credentials. SMTP delivery is synchronous for email OTP and is independent of the optional notification worker. A fake SMTP hostname is not a deployment workaround. Set an owned/publicly resolvable DNS name whose A record points at the VM's static IPv4; remove an incorrect AAAA record if IPv6 is not configured. Caddy needs reachable 80/443 and persistent certificate storage for [automatic HTTPS](https://caddyserver.com/docs/automatic-https).

The default LINE settings are disabled. Public frontend labels are `VITE_LINE_BOT_BASIC_ID=@057acdtt` and `VITE_LINE_BOT_DISPLAY_NAME=meichu_test`; those are not credentials. `VITE_LIFF_ID` remains blank until a real LIFF application is configured, and any change requires rebuilding the image. Bot Messaging credentials, Login channel/provider, LIFF and webhook configuration are separate prerequisites; their absence must not be described as completed authentication. Configure them privately and explicitly enable live replies only after the matching runtime has been reviewed.

```bash
sudo bash deploy/gcp/activate.sh
```

Activation validates Compose, builds the existing hardened multi-stage Dockerfile, validates production settings without printing their values, parses the Caddyfile, starts the private database/antivirus, stops writers, takes a consistent local backup, runs Alembic and seeds only the grant scheme, then switches `/opt/youth-service/current` and starts the systemd service. It stops on missing SMTP/domain/secrets, failed backup, migration or antivirus readiness. It does not seed demo accounts or create a staff account. Initial ClamAV signature download may take several minutes. Current image tags follow upstream stable releases; set `POSTGRES_IMAGE`, `CLAMAV_IMAGE` and `CADDY_IMAGE` to reviewed digest-pinned references for repeatable production image selection.

No automatic downgrade or rollback runs after a migration failure. Services stay stopped for review. Restoring old source alone is not guaranteed compatible with a newer database. The activation leaves outbound case notifications stopped; it never enables their systemd unit. If a unit was enabled in an earlier deployment, disable it until the current release's delivery configuration is reviewed, otherwise it may resume at the next boot.

## Verify and operate

Use the private wrapper so release image tags and env paths remain consistent:

```bash
sudo /opt/youth-service/current/deploy/gcp/compose.sh ps
sudo /opt/youth-service/current/deploy/gcp/compose.sh exec -T api \
  python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5).close(); print('ready')"
```

Check public HTTPS for `/`, `/admin/` and `/precheck`; simulator routes must return 404 and unsigned webhook requests must fail. Separately verify permitted synthetic email authentication, staff MFA, file upload/ClamAV scanning, durable case operations and the explicitly authorized LINE flow. No script here sends a test email/message or edits LINE console settings automatically. The optional case notification worker can be enabled with `sudo systemctl enable --now youth-notifications.service` only after its env flag and real recipients/delivery settings have been reviewed. Disabling it uses `sudo systemctl disable --now youth-notifications.service`.

Boot recovery is provided by Docker `restart: unless-stopped` plus `youth-service.service`; it reuses the fixed `youth-prod-*` named volumes. It does not rerun migrations or seed on reboot. Never use `docker compose down -v` for an update. Retain the VM's boot disk on deletion; deleting that disk still destroys local volumes and local backups.

## Consistent backup and restore limits

```bash
sudo bash /opt/youth-service/current/deploy/gcp/backup.sh
sudo systemctl restart youth-service.service
```

Backup stops Caddy, the API, maintenance and the notification worker before `pg_dump -Fc` and a read-only file-volume archive. It writes root-private files under `/var/backups/youth-service`; services remain stopped until restarted, including after a backup error. Restart notifications separately only if still authorized. This quiescence keeps attachment versions and database references from changing between the two snapshots.

Copy backups to your separately approved encrypted off-VM destination and test restores; the script does not configure a bucket, service account, retention policy or off-site transfer. Restore PostgreSQL and the matching file archive together on a stopped, isolated replacement instance using the recorded release and original private keys. Reapply ownership UID/GID 10001 to application data as needed. Verify schema, receipt references, file digests and login before enabling traffic or delivery. The local backup does not contain Caddy certificates, SMTP/LINE credentials or TOTP keys; those need their own protected recovery process. No automated destructive restore is supplied, and no restore drill or high-availability claim is made.

## Local validation

```bash
python3 deploy/gcp/test_deployment.py
python3 -m py_compile deploy/gcp/*.py
bash -n deploy/gcp/compose.sh deploy/gcp/bootstrap_vm.sh deploy/gcp/activate.sh deploy/gcp/backup.sh
docker compose --env-file /path/to/SYNTHETIC.env -f deploy/gcp/compose.yaml config --quiet
```

Use synthetic env values for local validation, including `YOUTH_ENV_FILE` pointing to that file. These checks validate source packaging, secret-output discipline, worker separation, script syntax and Compose structure. They do not prove GCP readiness, SMTP delivery, certificate issuance, ClamAV health or a restore. No cloud resources are created by these validation commands.
