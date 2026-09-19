#!/usr/bin/env bash
# Create the reviewed vm-plan.json infrastructure. Billable: run it deliberately.
# It provisions network, firewall, address and instance only. It installs no
# software, transfers no source, writes no settings and touches no LINE config.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
plan="$script_dir/vm-plan.json"
[[ -r $plan ]] || { echo 'vm-plan.json is missing; the plan is the only spec source.' >&2; exit 2; }
command -v gcloud >/dev/null || { echo 'Install and authenticate the gcloud CLI first.' >&2; exit 2; }

field() { python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(eval('d'+sys.argv[2]))" "$plan" "$1"; }
project=$(field "['project']")
region=$(field "['region']")
zone=$(field "['zone']")
instance=$(field "['instance_name']")
machine=$(field "['machine_type']")
network=$(field "['network']['name']")
subnet=$(field "['network']['subnet']")
cidr=$(field "['network']['cidr']")
disk_type=$(field "['boot_disk']['type']")
disk_size=$(field "['boot_disk']['size_gib']")
address="${instance}-ip"
dry_run=${DRY_RUN:-false}

g() { # Echo in dry-run; otherwise run against the planned project.
  if [[ $dry_run == true ]]; then echo "DRY-RUN: gcloud $*"; else gcloud "$@" --project "$project"; fi
}
exists() { gcloud "$@" --project "$project" >/dev/null 2>&1; }

echo "Project $project / zone $zone / $machine / ${disk_size} GiB ${disk_type}"
if [[ $dry_run != true ]]; then
  account=$(gcloud config get-value account 2>/dev/null || true)
  [[ -n $account && $account != '(unset)' ]] || { echo 'Run: gcloud auth login' >&2; exit 2; }
  # A project without billing accepts the API calls and then fails on quota.
  billing=$(gcloud billing projects describe "$project" --format='value(billingEnabled)' 2>/dev/null || echo ERROR)
  [[ $billing == True ]] || { echo "Billing is not enabled on $project (got: $billing). Link a billing account first." >&2; exit 2; }
  echo "Authenticated as $account; billing is enabled."
fi

echo '== Enabling required services =='
g services enable compute.googleapis.com oslogin.googleapis.com iap.googleapis.com

echo '== Network =='
if exists compute networks describe "$network"; then echo "network $network exists"; else
  g compute networks create "$network" --subnet-mode=custom
fi
if exists compute networks subnets describe "$subnet" --region "$region"; then echo "subnet $subnet exists"; else
  g compute networks subnets create "$subnet" --network="$network" --region="$region" \
    --range="$cidr" --enable-private-ip-google-access
fi

echo '== Firewall =='
# Only Caddy is published. 8000/5432/3310 stay on the private Docker network.
if exists compute firewall-rules describe youth-allow-web; then echo 'youth-allow-web exists'; else
  g compute firewall-rules create youth-allow-web --network="$network" \
    --direction=INGRESS --action=ALLOW --rules=tcp:80,tcp:443,udp:443 \
    --source-ranges=0.0.0.0/0 --target-tags=youth-web \
    --description='Public HTTP/HTTPS to Caddy only'
fi
# SSH is reachable through IAP TCP forwarding, never from the open internet.
if exists compute firewall-rules describe youth-allow-iap-ssh; then echo 'youth-allow-iap-ssh exists'; else
  g compute firewall-rules create youth-allow-iap-ssh --network="$network" \
    --direction=INGRESS --action=ALLOW --rules=tcp:22 \
    --source-ranges=35.235.240.0/20 --target-tags=youth-ssh \
    --description='SSH from IAP TCP forwarding only'
fi

echo '== Static external IPv4 =='
if exists compute addresses describe "$address" --region "$region"; then echo "address $address exists"; else
  g compute addresses create "$address" --region="$region" --network-tier=PREMIUM
fi

echo '== Instance =='
if exists compute instances describe "$instance" --zone "$zone"; then
  echo "instance $instance exists; not recreating"
else
  image_project=debian-cloud
  image_family=debian-13
  if [[ $dry_run != true ]] && ! gcloud compute images describe-from-family "$image_family" \
      --project "$image_project" >/dev/null 2>&1; then
    echo "debian-13 unavailable; falling back to debian-12" >&2
    image_family=debian-12
  fi
  # No service account: the application needs no Google Cloud API permissions.
  g compute instances create "$instance" --zone="$zone" --machine-type="$machine" \
    --subnet="$subnet" --address="$address" \
    --image-family="$image_family" --image-project="$image_project" \
    --boot-disk-size="${disk_size}GB" --boot-disk-type="$disk_type" \
    --boot-disk-device-name="$instance" --no-boot-disk-auto-delete \
    --tags=youth-web,youth-ssh \
    --metadata=enable-oslogin=TRUE \
    --no-service-account --no-scopes \
    --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
    --deletion-protection
fi

if [[ $dry_run == true ]]; then echo 'Dry run complete; nothing was created.'; exit 0; fi
ip=$(gcloud compute addresses describe "$address" --region "$region" --project "$project" --format='value(address)')
cat <<EOF

Static external IPv4: $ip
Point the chosen domain's A record at that address, and remove any stale AAAA record.
SSH (IAP only): gcloud compute ssh $instance --zone $zone --project $project --tunnel-through-iap

The VM is bare. Next: package the source, transfer it, then bootstrap and activate
per deploy/gcp/README.md. Nothing here configured SMTP, LINE or the domain.
EOF
