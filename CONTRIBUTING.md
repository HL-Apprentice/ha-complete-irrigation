# Contributing — Pre-push Checklist

Every change to this integration must pass **three independent checks** before it touches a user's Home Assistant. Two are automated and one is manual. This document defines them.

## Why this exists

Early versions broke unrelated parts of Home Assistant for users (frontend icons stopped loading after a botched panel registration). The triggers were issues that would have been caught locally before pushing. The three-check protocol exists so that doesn't happen again.

## The three checks

### Check 1 — Local lint + tests + manifest sanity

Run on your machine before every commit:

```bash
scripts/check.sh
```

This verifies:
- **Ruff lint** passes on `custom_components/`, `tests/`, `scripts/`
- **Ruff formatter** check passes (no unformatted files)
- **Unit tests** pass (`pytest tests/`)
- **Manifest sanity**: required keys present, ordering valid (`domain`, `name`, then alphabetical)

If anything fails, fix it before committing. Most issues are one-liners (`ruff check --fix ...` handles auto-fixable ones).

### Check 2 — CI on push

Pushing to GitHub triggers `.github/workflows/validate.yml`, which runs:

- **HA Hassfest** — validates the integration against HA's strict standards
- **HACS validation** — validates the integration is HACS-installable
- **Ruff lint** (mirror of Check 1, in case someone pushed without running locally)
- **Unit tests** (mirror of Check 1, run on Python 3.12 like real HA)

All four must be green before tagging a release. Don't merge or tag a release while any are red.

### Check 3 — Real Home Assistant smoke test

Before tagging any release (`v0.x.y`), the integration must load cleanly in a real Home Assistant. Automated via:

```bash
scripts/smoke-test.sh
```

This spins up Home Assistant in Docker (via `dev/docker-compose.yml`), mounts our integration, and verifies:

1. The container starts
2. HA listens on :8123 within 120s
3. HA discovers our component (`complete_irrigation` mentioned in log)
4. No `ERROR`/`CRITICAL` log lines mention our component
5. No `ERROR`/`CRITICAL` log lines mention `panel` / `frontend` / `static`
6. HA's frontend bundle serves (`/service_worker.js` returns 200)
7. HA's root URL resolves (200 after redirects)

Tears down the container automatically. Logs persist in `dev/ha-config/home-assistant.log` for debugging.

For manual UI exploration (config flow walkthrough, panel rendering, etc.), use the spin-up commands in [`dev/README.md`](dev/README.md).

**No release tag (`v*`) without all three checks green.**

## When to bump the version

Use [Semantic Versioning](https://semver.org/):
- **Patch** (`0.2.1`): bug fixes, lint fixes, docs, refactors that don't change behavior
- **Minor** (`0.3.0`): new features, breaking config-flow changes, new entities
- **Major** (`1.0.0`): first stable release; thereafter, breaking changes to entities/services

Bump in both `manifest.json` AND `pyproject.toml`. Then publish with `scripts/release.sh vX.Y.Z` — that script tags, pushes, **and creates the GitHub Release**. HACS only reads GitHub Releases, not raw tags, so the Release step is non-negotiable.

## Workflow for a typical change

```bash
# 1. Make your changes
$EDITOR custom_components/complete_irrigation/...

# 2. Run local checks
scripts/check.sh

# 3. Commit (small, focused commits preferred)
git add -A
git commit -m "feat: short description of what changed"

# 4. Push (triggers CI)
git push origin main

# 5. Wait for CI to pass on GitHub
gh run watch

# 6. If this is a release:
#    a. Smoke-test in real HA (Check 3)
scripts/smoke-test.sh
#    b. Bump version in manifest.json + pyproject.toml
#    c. Commit + push the bump
#    d. Publish — tag + push + create GitHub Release in one step:
scripts/release.sh v0.2.1
#    HACS picks the new Release up within ~15 min.
```

> **Why `release.sh`?** Pushing a tag does NOT automatically create a
> GitHub Release. HACS only reads Releases, so a tagged-but-not-released
> version is invisible to users. `scripts/release.sh` bundles tag +
> push + `gh release create` so the Release step can't be skipped.

## Module architecture

Two layers:

1. **Pure-logic modules** (no HA dependency, TDD-friendly):
   `helpers.py`, future `RunPlanner`, `ConflictResolver`, `MoistureGate`, `WeatherGate`, `ScheduleStore`
2. **HA-coupled adapters** (orchestrate the pure modules, glue to HA):
   `__init__.py`, `config_flow.py`, `coordinator.py` (future), `sensor.py` (future), `calendar.py` (future)

Push as much logic as possible into the pure-logic layer. Tests cover it directly. The HA-coupled layer should be thin and mostly delegation.

## TDD discipline

Per the matt pocock `tdd` skill: **one test → one implementation, vertical not horizontal.**

- Write a failing test first
- Write the minimum code that makes it pass
- Refactor only after green
- Never batch up many tests before any implementation

See the test suite in `tests/test_helpers.py` for the pattern.

## Style

- Python: ruff (config in `pyproject.toml`) — 100-char line limit, isort+pyupgrade+bugbear+naming-N+simplify enabled
- JS/CSS: no formatter required at v0.x; just keep things readable
- Commit messages: conventional commits style (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`)

## Filing issues

Issues use a small label vocabulary:

- **Category**: `bug` or `enhancement`
- **State**: `needs-triage` → (`needs-info` | `ready-for-agent` | `ready-for-human` | `wontfix`)
- **Type**: `vertical-slice` for tracer-bullet work items

See [docs/PRD.md](docs/PRD.md) for the project plan and [docs/ISSUES.md](docs/ISSUES.md) (when populated) for slice tracking.
