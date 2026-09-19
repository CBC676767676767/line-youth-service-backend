#!/usr/bin/env bash
set -euo pipefail
[[ $(id -u) -eq 0 ]] || { echo 'Run release activation with sudo.' >&2; exit 2; }
script_dir=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
release_dir=$(cd -P -- "$script_dir/../.." && pwd)
case "$release_dir" in /opt/youth-service/releases/*) ;; *) echo 'Extract the release under /opt/youth-service/releases first.' >&2; exit 2;; esac
settings=/etc/youth-service/runtime.env
[[ -f "$settings" ]] || { echo 'Initialize private runtime settings first.' >&2; exit 2; }
case "$(stat -c '%a' "$settings")" in 600|400) ;; *) echo 'runtime.env must have mode 0600 or 0400.' >&2; exit 2;; esac
[[ $(stat -c '%u' "$settings") -eq 0 ]] || { echo 'runtime.env must be root-owned.' >&2; exit 2; }
compose=(bash "$script_dir/compose.sh")
"${compose[@]}" config --quiet
"${compose[@]}" build api
"${compose[@]}" run --rm --no-deps api python /srv/deploy/check_settings.py
"${compose[@]}" run --rm --no-deps caddy caddy adapt --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
"${compose[@]}" up -d --wait --wait-timeout 900 db clamav
# Never migrate under active API/maintenance writers; backup leaves them stopped.
bash "$script_dir/backup.sh"
"${compose[@]}" run --rm --no-deps api alembic upgrade head
"${compose[@]}" run --rm --no-deps api python -m app.cli seed-grant
ln -sfn "$release_dir" /opt/youth-service/current
systemctl restart youth-service.service
echo 'Production containers started. Verify public HTTPS, SMTP and LINE separately before declaring readiness.'
echo 'Outbound case notifications remain stopped; enable their service separately when authorized.'
