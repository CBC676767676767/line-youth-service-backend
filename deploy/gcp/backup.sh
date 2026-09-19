#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ $(id -u) -eq 0 ]] || { echo 'Run the backup with sudo.' >&2; exit 2; }
script_dir=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
release_dir=$(cd -P -- "$script_dir/../.." && pwd)
export YOUTH_RELEASE=${YOUTH_RELEASE:-$(basename -- "$release_dir")}
compose=(bash "$script_dir/compose.sh")
root=/var/backups/youth-service
# activate.sh backs up on every run, and these share one 50 GiB disk with
# PostgreSQL and Docker. Keep a bounded window instead of growing forever.
keep=${YOUTH_BACKUP_KEEP:-5}
[[ $keep =~ ^[1-9][0-9]*$ ]] || { echo 'YOUTH_BACKUP_KEEP must be a positive integer.' >&2; exit 2; }
floor_mib=${YOUTH_BACKUP_MIN_FREE_MIB:-5120}
[[ $floor_mib =~ ^[1-9][0-9]*$ ]] || { echo 'YOUTH_BACKUP_MIN_FREE_MIB must be a positive integer.' >&2; exit 2; }
install -d -m 0700 "$root"
# Filling the disk mid-dump would take PostgreSQL down with it; stop first instead.
free_mib=$(df -Pm "$root" | awk 'NR==2 {print $4}')
if [[ -z $free_mib || $free_mib -lt $floor_mib ]]; then
  echo "Only ${free_mib:-unknown} MiB free under $root; need ${floor_mib} MiB." >&2
  echo 'Copy backups off the VM or lower YOUTH_BACKUP_KEEP, then retry.' >&2
  exit 2
fi
# Stop every writer before capturing PostgreSQL and its corresponding files.
"${compose[@]}" --profile notifications stop caddy api maintenance notification-worker
backup_dir=$(mktemp -d "$root"/backup-"$(date -u +%Y%m%dT%H%M%SZ)"-XXXXXX)
"${compose[@]}" exec -T db pg_dump -U youth -d youth -Fc > "$backup_dir/database.dump"
if docker volume inspect youth-prod-files >/dev/null 2>&1; then
  docker run --rm --network none --user 0:0 --read-only \
    --mount type=volume,src=youth-prod-files,dst=/data,readonly \
    --mount "type=bind,src=$backup_dir,dst=/backup" \
    --entrypoint tar "youth-service:$YOUTH_RELEASE" -czf /backup/files.tar.gz -C /data .
else
  printf 'No application file volume existed at backup time.\n' > "$backup_dir/no-file-volume.txt"
fi
printf '%s\n' "$YOUTH_RELEASE" > "$backup_dir/release.txt"
find "$backup_dir" -type f -exec chmod 0600 {} +

# Prune only after this backup succeeded, so a failure never leaves zero copies.
# The directory name embeds a UTC timestamp, so a reverse lexical sort is newest-first.
pruned=0
while IFS= read -r stale; do
  [[ -n $stale ]] || continue
  rm -rf -- "${root:?}/$stale"
  pruned=$((pruned + 1))
done < <(find "$root" -mindepth 1 -maxdepth 1 -type d -name 'backup-*' -printf '%f\n' \
           | sort -r | tail -n "+$((keep + 1))")

printf 'Consistent local backup: %s\n' "$backup_dir"
printf 'Retention: kept %s most recent, removed %s older; %s MiB free.\n' \
  "$keep" "$pruned" "$(df -Pm "$root" | awk 'NR==2 {print $4}')"
printf 'These stay on this VM only. Copy them to approved encrypted off-VM storage.\n'
printf 'Services remain stopped; activate a release or restart youth-service.\n'
