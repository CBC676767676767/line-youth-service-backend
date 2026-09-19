#!/usr/bin/env bash
set -euo pipefail
[[ $(id -u) -eq 0 ]] || { echo 'Run this host setup with sudo.' >&2; exit 2; }
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
. /etc/os-release
case "$ID" in ubuntu|debian) ;; *) echo 'Only Debian or Ubuntu is supported.' >&2; exit 2;; esac
if ! command -v docker >/dev/null; then
  apt-get update
  apt-get install -y ca-certificates curl python3
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
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
docker compose version >/dev/null || { echo 'Install the Docker Compose plugin before continuing.' >&2; exit 2; }
systemctl enable --now docker
install -d -m 0755 /opt/youth-service/releases
install -d -m 0700 /etc/youth-service /var/backups/youth-service
install -m 0644 "$script_dir/youth-service.service" /etc/systemd/system/youth-service.service
install -m 0644 "$script_dir/youth-notifications.service" /etc/systemd/system/youth-notifications.service
systemctl daemon-reload
systemctl enable youth-service.service
echo 'Docker and boot service are prepared. Nothing was exposed; initialize private settings, then activate a release.'
