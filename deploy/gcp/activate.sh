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
profile=production
case "${1-}" in
  --demo) profile=demo; shift ;;
  --production|"") ;;
  *) echo "Usage: activate.sh [--demo|--production]" >&2; exit 2 ;;
esac
compose=(bash "$script_dir/compose.sh")
"${compose[@]}" config --quiet
"${compose[@]}" build api
"${compose[@]}" run --rm --no-deps api python /srv/deploy/check_settings.py --profile "$profile"
# Validate the Caddyfile without joining the private network. The caddy service
# holds a fixed address there, so a second container on that network collides
# with the running proxy and aborts every re-activation after the first.
caddy_image=$(awk -F= '/^CADDY_IMAGE=/{print $2}' "$settings")
docker run --rm --network none \
  --mount "type=bind,src=$script_dir/Caddyfile,dst=/etc/caddy/Caddyfile,readonly" \
  --env YOUTH_DOMAIN="$(awk -F= '/^YOUTH_DOMAIN=/{print $2}' "$settings")" \
  "${caddy_image:-caddy:2-alpine}" caddy adapt --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
"${compose[@]}" up -d --wait --wait-timeout 900 db clamav
# Never migrate under active API/maintenance writers; backup leaves them stopped.
bash "$script_dir/backup.sh"
"${compose[@]}" run --rm --no-deps api alembic upgrade head
"${compose[@]}" run --rm --no-deps api python -m app.cli seed-grant
ln -sfn "$release_dir" /opt/youth-service/current
systemctl restart youth-service.service
if [[ $profile == demo ]]; then
  echo 'DEMO containers started. Email one-time codes are NOT delivered, so citizen login'
  echo 'cannot complete. Do not put real applications or real personal data here.'
else
  echo 'Production containers started. Verify public HTTPS, SMTP and LINE separately before declaring readiness.'
fi
echo 'Outbound case notifications remain stopped; enable their service separately when authorized.'
