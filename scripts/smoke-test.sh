#!/usr/bin/env bash
#
# Smoke test — Check 3 of the pre-push protocol.
#
# Spins up a disposable Home Assistant in Docker, mounts our integration,
# waits for HA to come online, then checks the log for:
#   • the integration was discovered / loadable
#   • NO ERROR-level log lines mention our domain
#   • HA's frontend serves successfully
#
# Tears down the container afterward (logs preserved in dev/ha-config/).
#
# Usage:
#   scripts/smoke-test.sh

set -euo pipefail

cd "$(dirname "$0")/.."

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
RESET='\033[0m'

step() { echo; echo -e "${YELLOW}→ $1${RESET}"; }
pass() { echo -e "${GREEN}✓ $1${RESET}"; }
die()  { echo -e "${RED}✗ $1${RESET}"; cleanup; exit 1; }

cleanup() {
  echo
  step "Cleaning up: docker compose down"
  (cd dev && docker compose down >/dev/null 2>&1 || true)
}

# ─── 1. Start HA ────────────────────────────────────────────────────
step "1/4  docker compose up -d (test HA container)"
(cd dev && docker compose up -d) >/dev/null
pass "container started"

# ─── 2. Wait for HA's HTTP server ───────────────────────────────────
step "2/4  Waiting for HA to listen on :8123 (up to 120s)"
DEADLINE=$(($(date +%s) + 120))
until curl -sf -o /dev/null http://localhost:8123/ 2>/dev/null \
    || curl -sf -o /dev/null http://localhost:8123/manifest.json 2>/dev/null; do
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    die "HA didn't respond on :8123 within 120s"
  fi
  sleep 3
  printf "."
done
echo
pass "HA responding"

# Give HA another 5s to finish component discovery
sleep 5

# ─── 3. Check log for our integration ───────────────────────────────
step "3/4  Scanning HA log for errors related to complete_irrigation"
LOG=$(docker compose -f dev/docker-compose.yml logs homeassistant 2>&1)

# 3a. Did HA discover our component?
if ! echo "$LOG" | grep -q "complete_irrigation" ; then
  echo "  HA didn't mention our domain at all — manifest may have been skipped."
  echo "  (This can happen if HA was unhappy with the manifest format.)"
  die "no mention of complete_irrigation in HA log"
fi
pass "HA found our component"

# 3b. Are there ANY ERROR-level lines mentioning our domain?
ERRORS=$(echo "$LOG" | grep -E "(ERROR|CRITICAL)" | grep -i "complete_irrigation" || true)
if [ -n "$ERRORS" ]; then
  echo "$ERRORS"
  die "found ERROR-level log lines for complete_irrigation"
fi
pass "no ERROR-level lines for complete_irrigation"

# 3c. Are there ANY ERROR-level lines related to frontend / panel registration?
PANEL_ERRORS=$(echo "$LOG" | grep -E "(ERROR|CRITICAL)" | grep -Ei "(panel|frontend|static)" || true)
if [ -n "$PANEL_ERRORS" ]; then
  echo "$PANEL_ERRORS"
  die "found ERROR-level lines mentioning panel/frontend/static"
fi
pass "no panel/frontend errors"

# ─── 4. HA frontend bundle is being served ──────────────────────────
step "4/4  HA frontend bundle is serving"
# /service_worker.js is a HA frontend asset that returns 200 regardless
# of onboarding state — reliable health signal across HA versions.
if ! curl -sf -o /dev/null http://localhost:8123/service_worker.js; then
  die "HA frontend asset (/service_worker.js) didn't serve — frontend may be unhealthy"
fi
pass "HA frontend serving"

# Also verify that following the root redirect lands somewhere valid
# (onboarding page if first-run, or main app if previously set up).
if ! curl -sLf -o /dev/null http://localhost:8123/; then
  die "HA's root page didn't resolve cleanly"
fi
pass "HA root resolves (200 after redirects)"

# Done — clean up.
cleanup

echo
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${GREEN}  Smoke test PASSED. Safe to tag release.${RESET}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
