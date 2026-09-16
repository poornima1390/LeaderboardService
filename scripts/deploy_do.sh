#!/usr/bin/env bash
#
# Deploy to DigitalOcean App Platform.
#
# Creates the app on first run and updates it in place afterwards, so the same
# command is safe to run repeatedly. Secrets are injected from the environment
# at apply time; none are committed to this repository.
#
# Requires:
#   doctl, authenticated  (doctl auth init)
#   envsubst              (GNU gettext)
#
# Usage:
#   export LB_API_KEY=$(openssl rand -hex 32)
#   export LB_ADMIN_API_KEY=$(openssl rand -hex 32)
#   ./scripts/deploy_do.sh
#
set -euo pipefail

APP_NAME="leaderboard-service"
SPEC_TEMPLATE=".do/app.yaml"
GITHUB_REPO="${GITHUB_REPO:-poornima1390/LeaderboardService}"

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }

# --- Preflight -------------------------------------------------------------
command -v doctl >/dev/null || die "doctl is not installed"
command -v envsubst >/dev/null || die "envsubst is not installed (apt install gettext-base)"
[[ -f "$SPEC_TEMPLATE" ]] || die "$SPEC_TEMPLATE not found; run from the repository root"

doctl account get >/dev/null 2>&1 || die "doctl is not authenticated. Run: doctl auth init"

# Generate keys if absent rather than failing, but say so loudly — a key you
# did not record is a key you cannot use to submit a score.
if [[ -z "${LB_API_KEY:-}" ]]; then
  LB_API_KEY="$(openssl rand -hex 32)"
  info "LB_API_KEY was unset; generated one. RECORD IT NOW:"
  printf '    API_KEY=%s\n' "$LB_API_KEY"
fi
if [[ -z "${LB_ADMIN_API_KEY:-}" ]]; then
  LB_ADMIN_API_KEY="$(openssl rand -hex 32)"
  info "LB_ADMIN_API_KEY was unset; generated one. RECORD IT NOW:"
  printf '    ADMIN_API_KEY=%s\n' "$LB_ADMIN_API_KEY"
fi
export LB_API_KEY LB_ADMIN_API_KEY GITHUB_REPO

# --- Render the spec -------------------------------------------------------
# An explicit variable list, so App Platform's own bindings — for example
# ${leaderboard-db.DATABASE_URL} — are passed through untouched instead of
# being substituted to an empty string.
rendered="$(mktemp -t lb-app-spec-XXXXXX.yaml)"
trap 'rm -f "$rendered"' EXIT
envsubst '$GITHUB_REPO $LB_API_KEY $LB_ADMIN_API_KEY' < "$SPEC_TEMPLATE" > "$rendered"

grep -q 'leaderboard-db.DATABASE_URL' "$rendered" \
  || die "database binding was clobbered during substitution; refusing to deploy"

info "Validating spec"
doctl apps spec validate "$rendered" >/dev/null || die "spec validation failed"

# --- Create or update ------------------------------------------------------
app_id="$(doctl apps list --format ID,Spec.Name --no-header \
  | awk -v n="$APP_NAME" '$2 == n {print $1; exit}')"

if [[ -z "$app_id" ]]; then
  info "Creating app '$APP_NAME'"
  app_id="$(doctl apps create --spec "$rendered" --format ID --no-header --wait)"
  info "Created app $app_id"
else
  info "Updating existing app $app_id"
  doctl apps update "$app_id" --spec "$rendered" --wait >/dev/null
fi

# --- Report ----------------------------------------------------------------
app_url="$(doctl apps get "$app_id" --format DefaultIngress --no-header)"
info "Deployment finished"
printf '    app id : %s\n    url    : %s\n' "$app_id" "$app_url"

if [[ -n "$app_url" ]]; then
  info "Probing ${app_url}/health"
  # 200 "degraded" is the expected answer without Redis attached (Spec.md D2).
  for _ in $(seq 1 30); do
    code="$(curl -s -o /tmp/lb-health.json -w '%{http_code}' "${app_url}/health" || true)"
    if [[ "$code" == "200" ]]; then
      printf '    HTTP %s\n' "$code"
      cat /tmp/lb-health.json; echo
      exit 0
    fi
    sleep 5
  done
  die "health check never returned 200. Inspect: doctl apps logs $app_id --type run"
fi
