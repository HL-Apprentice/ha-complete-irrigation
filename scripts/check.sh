#!/usr/bin/env bash
#
# Local pre-push verification — the same checks CI runs, but on your
# machine before a single byte hits GitHub.
#
# Three checks, in order:
#   1. Lint  (ruff check + format)
#   2. Tests (pytest, in YOUR venv — HA tests run here)
#   3. CI parity (pytest with ONLY requirements-dev.txt — catches imports that
#      resolve only because your machine has the package; CI has no HA)
#   4. Manifest sanity (key order + required fields)
#
# A 4th "smoke-test in real HA" check is documented in CONTRIBUTING.md
# and runs from `scripts/smoke-test.sh` once the local HA env is set up.
#
# Usage:
#   scripts/check.sh
#
# Exits with status 0 if all checks pass, non-zero on the first failure.

set -euo pipefail

# Make this script work no matter where it's invoked from.
cd "$(dirname "$0")/.."

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
RESET='\033[0m'

step() {
  echo
  echo -e "${YELLOW}→ $1${RESET}"
}

pass() {
  echo -e "${GREEN}✓ $1${RESET}"
}

fail() {
  echo -e "${RED}✗ $1${RESET}"
  exit 1
}

# ─── 1. Lint ────────────────────────────────────────────────────────
step "1/4  Ruff lint"
if ! python3 -m ruff check custom_components/ tests/ scripts/; then
  fail "lint check failed — fix the issues above (try: python3 -m ruff check --fix ...)"
fi
if ! python3 -m ruff format --check custom_components/ tests/ scripts/; then
  fail "formatting check failed — run: python3 -m ruff format custom_components/ tests/ scripts/"
fi
pass "lint clean"

# ─── 2. Unit tests ──────────────────────────────────────────────────
step "2/4  Unit tests (pytest, your venv)"
if ! python3 -m pytest tests/ -q; then
  fail "tests failed"
fi
pass "all tests passing"

# ─── 3. CI parity ───────────────────────────────────────────────────
# Check 2 runs in YOUR venv, which very likely has homeassistant / voluptuous /
# aiohttp installed so the HA-dependent tests actually run. CI installs ONLY
# requirements-dev.txt. That gap once let CI stay red across six releases while
# this script printed green: a new test imported services.py (-> voluptuous) and
# died at COLLECTION in CI, which is a hard error, not a skip.
#
# So: re-run the suite in a throwaway venv built from requirements-dev.txt alone.
# Cheap (cached between runs, ~8s) and it catches exactly that class of bug —
# an import that only resolves because your machine happens to have the package.
step "3/4  CI parity (pytest with ONLY requirements-dev.txt)"
CI_VENV="${CI_PARITY_VENV:-$HOME/.cache/irrigation-CI-mirror}"
if [ -n "${SKIP_CI_PARITY:-}" ]; then
  echo "  (skipped: SKIP_CI_PARITY set)"
else
  if [ ! -x "$CI_VENV/bin/python" ]; then
    echo "  creating CI-mirror venv at $CI_VENV ..."
    python3 -m venv "$CI_VENV" >/dev/null 2>&1 || fail "could not create the CI-mirror venv"
  fi
  "$CI_VENV/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
  if ! "$CI_VENV/bin/pip" install -q -r requirements-dev.txt >/dev/null 2>&1; then
    fail "could not install requirements-dev.txt into the CI-mirror venv"
  fi
  if ! "$CI_VENV/bin/python" -m pytest tests/ -q; then
    fail "tests fail with only requirements-dev.txt installed — CI will be RED.
       Your venv has extra packages that are masking this. Either import the pure
       module instead of an HA-dependent one, guard with pytest.importorskip, or
       add the dependency to requirements-dev.txt if the test genuinely needs it."
  fi
  pass "CI parity green (imports resolve without HA installed)"
fi

# ─── 4. Manifest sanity ─────────────────────────────────────────────
step "4/4  Manifest sanity"
MANIFEST="custom_components/complete_irrigation/manifest.json"

# Required keys
for key in domain name version codeowners config_flow iot_class; do
  if ! python3 -c "import json,sys; m=json.load(open('$MANIFEST')); sys.exit(0 if '$key' in m else 1)"; then
    fail "manifest missing required key: $key"
  fi
done

# Hassfest's rule: domain first, name second, rest alphabetical
python3 - <<'PY'
import json, sys

m = json.load(open("custom_components/complete_irrigation/manifest.json"))
keys = list(m.keys())
if keys[0] != "domain" or keys[1] != "name":
    print(f"manifest: first two keys must be 'domain' then 'name', got {keys[:2]}")
    sys.exit(1)
rest = keys[2:]
if rest != sorted(rest):
    print(f"manifest: keys after 'name' must be alphabetical")
    print(f"  got:    {rest}")
    print(f"  wanted: {sorted(rest)}")
    sys.exit(1)
PY
pass "manifest valid"

echo
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${GREEN}  All local checks passed (incl. CI parity). Safe to commit + push.${RESET}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
