#!/usr/bin/env bash
# Acceptance checks for a running deployment, from the VM itself.
#
# Every check is an observation, not a claim of readiness: a green run proves the
# edge serves the right routes, hides the private ones, and rejects unsigned
# webhooks. It does not prove email delivery, LINE replies, antivirus quality or
# any case decision. Those need their own explicitly authorized tests.
set -uo pipefail
[[ $(id -u) -eq 0 ]] || { echo 'Run verification with sudo.' >&2; exit 2; }
script_dir=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
settings=${YOUTH_ENV_FILE:-/etc/youth-service/runtime.env}
[[ -f $settings ]] || { echo 'Private runtime settings are missing.' >&2; exit 2; }
domain=$(awk -F= '/^YOUTH_DOMAIN=/{print $2}' "$settings")
[[ -n $domain ]] || { echo 'YOUTH_DOMAIN is not set.' >&2; exit 2; }
plan="$script_dir/vm-plan.json"
webhook=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['public_webhook_path'])" "$plan")

passed=0
failed=0
note() { printf '  %-7s %s\n' "$1" "$2"; }
check() { # check <expected-status> <description> <curl args...>
  local expected=$1 description=$2; shift 2
  local actual
  actual=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "$@" 2>/dev/null)
  if [[ $actual == "$expected" ]]; then
    note PASS "$description ($actual)"; passed=$((passed + 1))
  else
    note FAIL "$description (expected $expected, got $actual)"; failed=$((failed + 1))
  fi
}

echo "== Container state =="
bash "$script_dir/compose.sh" ps --format 'table {{.Service}}\t{{.Status}}' || true

echo
echo "== TLS =="
if echo | openssl s_client -connect "$domain:443" -servername "$domain" 2>/dev/null \
     | openssl x509 -noout -subject -issuer -dates 2>/dev/null; then
  note PASS "certificate presented for $domain"; passed=$((passed + 1))
else
  note FAIL "no usable certificate for $domain"; failed=$((failed + 1))
fi

echo
echo "== Public routes that must work =="
check 200 "citizen entry /"            "https://$domain/"
check 200 "precheck /precheck"         "https://$domain/precheck"
check 200 "staff entry /admin/"        "https://$domain/admin/"

echo
echo "== Private routes that must not be reachable from the internet =="
for path in /docs /redoc /openapi.json /health/ready /health/live /line-simulator; do
  check 404 "blocked $path" "https://$domain$path"
done

echo
echo "== Webhook =="
# An unsigned body must be refused by signature verification, not by a missing route.
# 404 here means the channel is pointed at a path the application never registers.
check 401 "unsigned webhook refused at $webhook" \
  -X POST "https://$domain$webhook" -H 'Content-Type: application/json' \
  -H 'X-Line-Signature: not-a-valid-signature' --data '{"destination":"x","events":[]}'

echo
echo "== HTTP redirect =="
check 308 "http://$domain/ redirects to https" "http://$domain/"

echo
printf 'checks passed: %s, failed: %s\n' "$passed" "$failed"
if [[ $failed -gt 0 ]]; then
  echo 'Deployment is NOT verified. Fix the failures above before announcing availability.' >&2
  exit 1
fi
cat <<'EOF'
Edge checks passed. Still unproven by this script, and each needs its own check:
  - a real email one-time code arriving in a citizen mailbox
  - LINE delivering a signed event and the bot replying
  - ClamAV rejecting a genuinely malicious upload
  - a restore drill from the local backup
EOF
