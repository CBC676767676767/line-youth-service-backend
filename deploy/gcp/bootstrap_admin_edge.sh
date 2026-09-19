#!/usr/bin/env bash
# Prepare the staff portal edge host.
#
# This host serves only the staff entry, on its own public address. It holds no
# application data, no database and no secrets beyond its TLS certificates: it
# terminates HTTPS and proxies to the application host over the private subnet.
# Losing it exposes nothing that the application host does not already protect
# with password, TOTP, role and per-case authorization.
set -euo pipefail
[[ $(id -u) -eq 0 ]] || { echo 'Run this host setup with sudo.' >&2; exit 2; }
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

domain=${YOUTH_ADMIN_DOMAIN:-}
app_internal=${YOUTH_APP_INTERNAL:-}
[[ -n $domain ]] || { echo 'Set YOUTH_ADMIN_DOMAIN to the public staff hostname.' >&2; exit 2; }
[[ $app_internal =~ ^10\.[0-9.]+$ ]] || { echo 'Set YOUTH_APP_INTERNAL to the application private address.' >&2; exit 2; }

. /etc/os-release
case "$ID" in ubuntu|debian) ;; *) echo 'Only Debian or Ubuntu is supported.' >&2; exit 2;; esac
if ! command -v docker >/dev/null; then
  apt-get update
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  cat > /etc/apt/sources.list.d/youth-docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
systemctl enable --now docker

install -d -m 0755 /opt/youth-admin-edge
install -m 0644 "$script_dir/Caddyfile.admin" /opt/youth-admin-edge/Caddyfile
install -d -m 0700 /etc/youth-admin-edge
umask 077
cat > /etc/youth-admin-edge/edge.env <<EOF
YOUTH_ADMIN_DOMAIN=$domain
YOUTH_APP_INTERNAL=$app_internal
EOF
chmod 0600 /etc/youth-admin-edge/edge.env

cat > /opt/youth-admin-edge/compose.yaml <<'YAML'
name: youth-admin-edge
services:
  caddy:
    image: ${CADDY_IMAGE:-caddy:2-alpine}
    environment:
      YOUTH_ADMIN_DOMAIN: ${YOUTH_ADMIN_DOMAIN:?Set the staff hostname}
      YOUTH_APP_INTERNAL: ${YOUTH_APP_INTERNAL:?Set the application private address}
    ports: ["80:80", "443:443", "443:443/udp"]
    volumes:
      - /opt/youth-admin-edge/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    restart: unless-stopped
    logging:
      driver: local
      options: {max-size: "10m", max-file: "3"}
volumes:
  caddy_data: {name: youth-admin-edge-caddy-data}
  caddy_config: {name: youth-admin-edge-caddy-config}
YAML

cat > /etc/systemd/system/youth-admin-edge.service <<'UNIT'
[Unit]
Description=Staff portal edge
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=true
ExecStart=/usr/bin/docker compose --project-name youth-admin-edge --env-file /etc/youth-admin-edge/edge.env -f /opt/youth-admin-edge/compose.yaml up -d --wait
ExecStop=/usr/bin/docker compose --project-name youth-admin-edge --env-file /etc/youth-admin-edge/edge.env -f /opt/youth-admin-edge/compose.yaml stop

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now youth-admin-edge.service
echo "Staff edge is serving $domain and proxying to $app_internal:8000."
echo 'It stores no case data. The application host still enforces every authorization check.'
