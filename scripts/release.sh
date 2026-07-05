#!/usr/bin/env bash
#
# One-shot release publisher.
#
# Runs the local 3-check, tags the current HEAD, pushes commit + tag,
# and creates the GitHub Release. The release-create step is the one
# we kept forgetting (tags alone don't trigger Releases, and HACS only
# reads Releases) — so it lives in a script that can't be skipped.
#
# Usage:
#   scripts/release.sh v1.5.3
#
# Pre-conditions (checked before any state change):
#   • On branch `main` with a clean working tree
#   • Current HEAD's manifest.json + pyproject.toml versions match the arg
#   • scripts/check.sh passes (lint + tests + manifest)
#
# Smoke test is NOT auto-run here — run scripts/smoke-test.sh yourself
# before calling this so you can spot Docker/HA setup issues earlier.

set -euo pipefail

cd "$(dirname "$0")/.."

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
RESET='\033[0m'
step()  { echo; echo -e "${YELLOW}→ $1${RESET}"; }
pass()  { echo -e "${GREEN}✓ $1${RESET}"; }
die()   { echo -e "${RED}✗ $1${RESET}"; exit 1; }

# ─── 1. Args + preconditions ─────────────────────────────────────────
TAG="${1:-}"
[ -n "$TAG" ] || die "Usage: scripts/release.sh vX.Y.Z"
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Tag must look like vX.Y.Z (got '$TAG')"
VERSION="${TAG#v}"

step "1/6  Verify branch + working tree"
branch=$(git rev-parse --abbrev-ref HEAD)
[ "$branch" = "main" ] || die "Must be on main (currently on '$branch')"
[ -z "$(git status --porcelain)" ] || die "Working tree not clean — commit or stash first"
pass "on main, working tree clean"

step "2/6  Verify version strings match $VERSION"
manifest_v=$(python3 -c 'import json; print(json.load(open("custom_components/complete_irrigation/manifest.json"))["version"])')
pyproj_v=$(grep -E '^version = ' pyproject.toml | head -1 | sed -E 's/version = "([^"]+)"/\1/')
# The panel's on-screen version is a hardcoded JS constant — gate it here so it can
# never silently drift from the release again (it was stuck at v1.19.0 for many releases).
panel_v=$(grep -oE 'PANEL_VERSION *= *"[^"]+"' custom_components/complete_irrigation/frontend/complete-irrigation-panel.js | head -1 | sed -E 's/.*"v?([^"]+)".*/\1/')
[ "$manifest_v" = "$VERSION" ] || die "manifest.json version is $manifest_v, expected $VERSION"
[ "$pyproj_v" = "$VERSION" ] || die "pyproject.toml version is $pyproj_v, expected $VERSION"
[ "$panel_v" = "$VERSION" ] || die "PANEL_VERSION is $panel_v, expected $VERSION (bump it in complete-irrigation-panel.js)"
pass "manifest.json + pyproject.toml + PANEL_VERSION all say $VERSION"

step "3/6  Verify tag does not already exist"
git rev-parse --verify "refs/tags/$TAG" >/dev/null 2>&1 && \
  die "Tag $TAG already exists locally — pick a new version"
gh release view "$TAG" >/dev/null 2>&1 && \
  die "GitHub release $TAG already exists — pick a new version"
pass "$TAG is free locally + on GitHub"

step "4/6  Local 3-check (lint + tests + manifest)"
bash scripts/check.sh >/dev/null 2>&1 || die "scripts/check.sh failed — run it and fix the output"
pass "3-check green"

# ─── 5. Tag + push ──────────────────────────────────────────────────
step "5/6  Tag + push commit + tag"
git tag "$TAG"
# Push the current branch (in case the commit isn't on the remote yet)
# and the tag itself, in one round-trip.
git push origin main "$TAG"
pass "pushed $TAG to origin"

# ─── 6. GitHub Release ──────────────────────────────────────────────
step "6/6  Create GitHub Release (this is what HACS reads)"
# Use the last commit's message as the release notes, stripping the
# trailing Co-Authored-By trailer (which clutters the changelog).
notes=$(git log -1 --format='%B' "$TAG" | awk '
  /^Co-Authored-By:/ { next }
  { buf = buf $0 "\n" }
  END { sub(/\n+$/, "", buf); print buf }
')
gh release create "$TAG" --title "$TAG" --latest --notes "$notes" >/dev/null
url=$(gh release view "$TAG" --json url -q .url)
pass "release published: $url"

echo
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${GREEN}  $TAG published. HACS will pick it up within ~15 min.${RESET}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
