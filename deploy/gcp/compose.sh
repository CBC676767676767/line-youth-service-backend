#!/usr/bin/env bash
set -euo pipefail
script_dir=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
release_dir=$(cd -P -- "$script_dir/../.." && pwd)
export YOUTH_RELEASE=${YOUTH_RELEASE:-$(basename -- "$release_dir")}
export YOUTH_ENV_FILE=${YOUTH_ENV_FILE:-/etc/youth-service/runtime.env}
[[ "$YOUTH_RELEASE" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || { echo 'Invalid release image tag.' >&2; exit 2; }
[[ -f "$YOUTH_ENV_FILE" ]] || { echo 'Private runtime settings are missing.' >&2; exit 2; }
exec docker compose --project-name youth-service-prod --env-file "$YOUTH_ENV_FILE" -f "$script_dir/compose.yaml" "$@"
