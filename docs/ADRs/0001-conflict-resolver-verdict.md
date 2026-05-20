# ADR 0001 — ConflictResolver state machine validated

**Date:** 2026-05-19
**Status:** Accepted
**Supersedes:** P1 prototype (deleted)

## Context

Before implementing the production `ConflictResolver` module, we needed
to validate that the three policies agreed in the design phase (A defer
new / B shift existing earlier / C split the difference) actually produce
correct, sensible output across realistic real-world scenarios — including
edge cases like cascading conflicts, back-to-back schedules, midnight
crossings, and the cascade-cap activation.

Per the `prototype` skill (LOGIC branch), a throwaway terminal app was
built at `prototypes/p1_conflict_resolver/` to push the state machine
through these cases interactively.

## Decision

The three policies behave as specified. The prototype passed the
following scenarios with correct, expected output:

### Scenario 1 — Two-way 15-min overlap

Given `Front 6:00–6:30` + `Back 6:15–6:35`:

- **Policy A:** Front unchanged (6:00–6:30), Back deferred to 6:32–7:02
  (+17 min from requested, includes 2-min buffer). ✓
- **Policy B:** Back stays at requested 6:15, Front shifted earlier to
  5:43–6:13 (−17 min, includes 2-min buffer). ✓
- **Policy C:** Front to 5:52–6:22 (−8), Back to 6:24–6:54 (+9), 2-min
  buffer between (16 min total shift = 15 overlap + 2 buffer, split). ✓

### Scenario 5 — Five overlapping schedules within 1 hour

Given `A–E` starting every 10 minutes from 6:00 with 30-min duration:

- Policy A correctly cascades into back-to-back sequence with 2-min
  buffers, ending up at 6:00 / 6:32 / 7:04 / 7:36 / 8:08. Final shift
  is 118 min on the latest run, just under the 120-min cascade cap. ✓
- The 9-iteration resolution path is observable in the prototype logs,
  confirming the iterative re-resolve loop terminates correctly when
  no further conflicts exist.

### Cascade cap

When a single run's required shift would exceed `CASCADE_CAP_MINUTES`
(default 120), the resolver:
1. Logs a warning identifying the run and the proposed shift.
2. For Policy B: falls back to Policy A semantics for that pair.
3. For Policy C: falls back to Policy A semantics for that pair.
4. For Policy A pushed past cap: marks the run as `SKIPPED` rather than
   deferring further.

This matches the spec: avoid pushing runs indefinitely; prefer skip +
notify over compounding cascades.

### Back-to-back buffer

Confirmed: when two runs are scheduled to start exactly when the previous
ends (e.g., `Front 6:00–6:30` + `Back 6:30–6:50`), Policy A correctly
applies the 2-min buffer, pushing Back to 6:32.

### Midnight crossing

Scenario 4 (`Late1 23:30–24:15`, `Late2 23:50–24:20`, `Early 0:10–0:30`)
resolved correctly under all three policies. Times beyond 24:00 are
internally represented as `>= 1440` minutes-since-midnight and render
as `HH:MM (+1d)` in the UI.

## Implications for the production ConflictResolver

1. **Time representation: integer minutes since midnight.** Allow values
   below 0 and above 1440 internally; clamp only at display. Avoids
   modular-arithmetic bugs in midnight-crossing cases.

2. **Iterative resolve loop with a cap.** Use `MAX_RESOLVE_ITERATIONS`
   (default 50 in prototype) as a safety net. In normal operation,
   resolution finishes in 1–10 iterations.

3. **Policy fallback on cascade-cap activation.** Policies B and C
   automatically fall back to Policy A for the offending pair rather
   than failing the entire save. Policy A pushed past cap marks the
   run as SKIPPED.

4. **Per-run `original_start` tracking.** Required for cascade-cap
   evaluation. Carry through schedule edits — don't reset on each
   resolve cycle.

5. **Reason strings on every shift.** The production module should
   surface a human-readable reason per adjusted run, matching the
   prototype's log output. These flow to the panel UI and notifications.

## Consequences

- Implementation of Slice #8 (`ConflictResolver` in PRD issue #8) can
  proceed with confidence in the state machine.
- The prototype is deleted. This ADR is the durable record of what was
  learned.
- No design changes needed — the policies as specified in the design
  conversation produce sensible output without modification.
