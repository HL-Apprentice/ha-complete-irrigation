#!/usr/bin/env python3
"""ConflictResolver prototype — throwaway, see README.md for question.

Time model: minutes since 00:00. Values > 1440 represent "tomorrow",
< 0 represent "yesterday". This lets us shift things across midnight
cleanly during resolution and only render the wrapped form for display.
"""
from __future__ import annotations

import shlex
import sys
from dataclasses import dataclass, replace
from typing import Iterable

BUFFER_MINUTES = 2  # default gap between back-to-back zones
CASCADE_CAP_MINUTES = 120  # don't push more than 2 hours past original
MAX_RESOLVE_ITERATIONS = 50  # safety net against infinite loops


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Run:
    zone: str
    start_min: int        # minutes since 00:00 (can wrap past 1440 after shifts)
    duration_min: int
    original_start: int   # what the user originally asked for, for reporting

    @property
    def end_min(self) -> int:
        return self.start_min + self.duration_min

    @property
    def shift_from_original(self) -> int:
        return self.start_min - self.original_start


# ---------------------------------------------------------------------------
# Conflict resolution policies
# ---------------------------------------------------------------------------

def resolve(runs: list[Run], policy: str) -> tuple[list[Run], list[str]]:
    """Return (resolved_runs, logs).

    policy ∈ {"A", "B", "C"}:
      A — defer new (push later overlapping run after earlier one ends + buffer)
      B — shift existing earlier (move earlier run back so new can stay put)
      C — split the difference (both yield equally)
    """
    if policy not in ("A", "B", "C"):
        raise ValueError(f"Unknown policy: {policy}")

    if not runs:
        return [], ["(no runs to resolve)"]

    logs: list[str] = []
    # Operate on sorted-by-start working copy; treat earlier-listed as "existing"
    work = sorted(runs, key=lambda r: r.start_min)

    for iteration in range(MAX_RESOLVE_ITERATIONS):
        conflict = _find_first_conflict(work)
        if conflict is None:
            if iteration > 0:
                logs.append(f"  (resolved after {iteration} adjustment{'s' if iteration > 1 else ''})")
            break
        i, j = conflict
        existing, new = work[i], work[j]
        overlap = existing.end_min + BUFFER_MINUTES - new.start_min
        # overlap > 0 means we need to move things

        if policy == "A":
            # Defer the new (later-starting) run
            shifted_new = replace(new, start_min=existing.end_min + BUFFER_MINUTES)
            shift = shifted_new.start_min - new.original_start
            if shift > CASCADE_CAP_MINUTES:
                logs.append(
                    f"  ⚠ Policy A would push {new.zone} {shift}min past original — "
                    "exceeds cascade cap. Marking as SKIP."
                )
                # Mark with sentinel start_min < 0 to indicate skip
                work[j] = replace(new, start_min=-9999)
            else:
                work[j] = shifted_new
                logs.append(
                    f"  • {new.zone}: deferred to {_fmt(shifted_new.start_min)} "
                    f"(was {_fmt(new.original_start)}, +{shifted_new.start_min - new.start_min}min)"
                )
        elif policy == "B":
            # Shift existing earlier so new can run at its requested time
            shifted_existing = replace(
                existing,
                start_min=new.start_min - existing.duration_min - BUFFER_MINUTES,
            )
            shift = abs(shifted_existing.shift_from_original)
            if shift > CASCADE_CAP_MINUTES:
                logs.append(
                    f"  ⚠ Policy B would shift {existing.zone} {shift}min from original — "
                    "exceeds cascade cap. Falling back to Policy A for this pair."
                )
                shifted_new = replace(new, start_min=existing.end_min + BUFFER_MINUTES)
                work[j] = shifted_new
                logs.append(
                    f"  • {new.zone}: deferred to {_fmt(shifted_new.start_min)} (fallback)"
                )
            else:
                work[i] = shifted_existing
                logs.append(
                    f"  • {existing.zone}: shifted earlier to {_fmt(shifted_existing.start_min)} "
                    f"(was {_fmt(existing.original_start)}, {shifted_existing.shift_from_original:+d}min)"
                )
        else:  # policy == "C"
            # Split the difference — each yields ~half the overlap+buffer
            total_shift = overlap  # = existing.end + buffer - new.start
            existing_shift = total_shift // 2  # earlier
            new_shift = total_shift - existing_shift  # later
            shifted_existing = replace(existing, start_min=existing.start_min - existing_shift)
            shifted_new = replace(new, start_min=new.start_min + new_shift)
            e_drift = abs(shifted_existing.shift_from_original)
            n_drift = abs(shifted_new.shift_from_original)
            if max(e_drift, n_drift) > CASCADE_CAP_MINUTES:
                logs.append(
                    f"  ⚠ Policy C would shift past cascade cap — "
                    "falling back to Policy A for this pair."
                )
                work[j] = replace(new, start_min=existing.end_min + BUFFER_MINUTES)
                logs.append(
                    f"  • {new.zone}: deferred to {_fmt(work[j].start_min)} (fallback)"
                )
            else:
                work[i] = shifted_existing
                work[j] = shifted_new
                logs.append(
                    f"  • {existing.zone}: −{existing_shift}min → {_fmt(shifted_existing.start_min)} | "
                    f"{new.zone}: +{new_shift}min → {_fmt(shifted_new.start_min)}"
                )

        # Re-sort and loop — cascading conflicts may have appeared
        work = sorted(work, key=lambda r: r.start_min if r.start_min >= 0 else 99999)
    else:
        logs.append(f"  ⚠ Hit max iterations ({MAX_RESOLVE_ITERATIONS}); resolver gave up.")

    return work, logs


def _find_first_conflict(runs: list[Run]) -> tuple[int, int] | None:
    """Return (i, j) of the first overlapping pair (i < j), or None."""
    for i in range(len(runs) - 1):
        for j in range(i + 1, len(runs)):
            a, b = runs[i], runs[j]
            if a.start_min < 0 or b.start_min < 0:  # skipped runs
                continue
            if b.start_min < a.end_min + BUFFER_MINUTES:
                return (i, j)
    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt(min_since_midnight: int) -> str:
    """Format minutes since 00:00 as HH:MM, with (+1d) / (-1d) suffix if wrapped."""
    if min_since_midnight < 0:
        return "SKIPPED"
    day_offset = min_since_midnight // 1440
    m = min_since_midnight % 1440
    hh, mm = divmod(m, 60)
    suffix = ""
    if day_offset > 0:
        suffix = f" (+{day_offset}d)"
    elif day_offset < 0:
        suffix = f" ({day_offset}d)"
    return f"{hh:02d}:{mm:02d}{suffix}"


def render_state(runs: list[Run], policy: str) -> str:
    out: list[str] = []
    out.append(f"\n── policy {policy} | buffer {BUFFER_MINUTES}min | cap {CASCADE_CAP_MINUTES}min ──")
    if not runs:
        out.append("  (no runs yet)")
        return "\n".join(out)
    out.append(f"  {'zone':<12} {'start':<14} {'end':<14} duration   original")
    for r in runs:
        original_note = ""
        if r.start_min != r.original_start and r.start_min >= 0:
            original_note = f"(was {_fmt(r.original_start)})"
        out.append(
            f"  {r.zone:<12} {_fmt(r.start_min):<14} "
            f"{_fmt(r.end_min) if r.start_min >= 0 else 'SKIPPED':<14} "
            f"{r.duration_min:>3} min   {original_note}"
        )
    return "\n".join(out)


def render_gantt(runs: list[Run], policy: str) -> str:
    """ASCII Gantt: 24 hours wide, 1 char ≈ 30 min."""
    width = 48  # 30 min per char
    out: list[str] = []
    out.append(f"\n── Gantt (policy {policy}) ──")
    # Hour scale
    scale = "  " + "".join(f"{h:02d}".ljust(2) for h in range(0, 24, 1))
    # 48 chars total = 1 char per half hour
    scale = "  " + "".join((f"{h:02d}" if h % 3 == 0 else "··").ljust(2) for h in range(0, 24))
    out.append(scale[:width + 2])
    tick = "  " + "|" * 24
    expanded = "  " + ("|·" * 24)
    out.append(expanded[:width + 2])
    for r in runs:
        bar = [" "] * width
        if r.start_min >= 0:
            # Clip to [0, 1440) for display; mark wrap with arrow
            display_start = r.start_min % 1440
            display_end = (r.end_min) % 1440 if r.end_min > 0 else 0
            sb = display_start // 30
            eb = max(sb + 1, (r.start_min + r.duration_min) // 30)
            # If wraps past day end, just bar to end and add ">"
            for x in range(max(0, sb), min(width, eb)):
                bar[x] = "█"
            if r.end_min > 1440:
                if width - 1 < len(bar):
                    bar[width - 1] = ">"
        bar_str = "".join(bar)
        out.append(f"  {bar_str}  {r.zone}: {_fmt(r.start_min)} – {_fmt(r.end_min) if r.start_min >= 0 else 'SKIPPED'}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

HELP = """
Commands:
  add <zone> <HH:MM> <minutes>    Queue a run
                                   e.g. add North 6:00 30
  policy A|B|C                     Pick resolution policy
                                   A=defer new  B=shift existing earlier  C=split
  resolve                          Run resolver, print resolved schedule + Gantt
  state                            Show current unresolved queue
  reset                            Clear all runs
  buffer <minutes>                 Change the back-to-back buffer (default 2)
  cap <minutes>                    Change cascade cap (default 120)
  demo                             Load a preset scenario set
  scenario <1-5>                   Load a specific test scenario
  help                             This message
  quit                             Exit
"""

SCENARIOS = {
    "1": (
        "Two-way 15-min overlap",
        [("Front", "6:00", 30), ("Back", "6:15", 30)],
    ),
    "2": (
        "Back-to-back (buffer should appear)",
        [("Front", "6:00", 30), ("Back", "6:30", 20)],
    ),
    "3": (
        "Three-way cascade",
        [("Front", "6:00", 30), ("Back", "6:15", 30), ("Side", "6:40", 30)],
    ),
    "4": (
        "Midnight crossing",
        [("Late1", "23:30", 45), ("Late2", "23:50", 30), ("Early", "0:10", 20)],
    ),
    "5": (
        "Cascade cap activation (4 schedules within 1 hour)",
        [("A", "6:00", 30), ("B", "6:10", 30), ("C", "6:20", 30), ("D", "6:30", 30), ("E", "6:40", 30)],
    ),
}


def parse_hhmm(s: str) -> int:
    """Convert 'H:MM' or 'HH:MM' to minutes since midnight."""
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"Bad time format: {s} (want HH:MM)")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"Time out of range: {s}")
    return h * 60 + m


def repl() -> int:
    global BUFFER_MINUTES, CASCADE_CAP_MINUTES
    runs: list[Run] = []
    policy = "A"

    print("ConflictResolver prototype — type `help` for commands, `quit` to exit.")
    print(render_state(runs, policy))

    while True:
        try:
            raw = input("\n> ").strip()
        except EOFError:
            print()
            return 0
        if not raw:
            continue
        try:
            parts = shlex.split(raw)
        except ValueError as e:
            print(f"  parse error: {e}")
            continue
        cmd = parts[0].lower()
        args = parts[1:]

        try:
            if cmd in ("quit", "exit"):
                return 0
            elif cmd == "help":
                print(HELP)
            elif cmd == "reset":
                runs = []
                print("  → cleared")
                print(render_state(runs, policy))
            elif cmd == "add":
                if len(args) != 3:
                    raise ValueError("usage: add <zone> <HH:MM> <minutes>")
                zone, hhmm, mins_s = args
                start = parse_hhmm(hhmm)
                mins = int(mins_s)
                if mins <= 0:
                    raise ValueError("duration must be > 0")
                runs.append(Run(zone=zone, start_min=start, duration_min=mins, original_start=start))
                print(f"  → added {zone} {_fmt(start)} for {mins}min")
                print(render_state(runs, policy))
            elif cmd == "policy":
                if not args or args[0].upper() not in ("A", "B", "C"):
                    raise ValueError("usage: policy A|B|C")
                policy = args[0].upper()
                print(f"  → policy set to {policy}")
                print(render_state(runs, policy))
            elif cmd == "buffer":
                if not args:
                    raise ValueError("usage: buffer <minutes>")
                BUFFER_MINUTES = int(args[0])
                print(f"  → buffer set to {BUFFER_MINUTES}min")
            elif cmd == "cap":
                if not args:
                    raise ValueError("usage: cap <minutes>")
                CASCADE_CAP_MINUTES = int(args[0])
                print(f"  → cascade cap set to {CASCADE_CAP_MINUTES}min")
            elif cmd == "state":
                print(render_state(runs, policy))
            elif cmd == "resolve":
                resolved, logs = resolve(runs, policy)
                print("\nResolver log:")
                for line in logs:
                    print(line)
                print(render_state(resolved, policy))
                print(render_gantt(resolved, policy))
            elif cmd == "demo":
                runs = []
                for sid in ("1", "2", "3"):
                    for z, t, m in SCENARIOS[sid][1]:
                        runs.append(Run(zone=f"{sid}-{z}", start_min=parse_hhmm(t), duration_min=m, original_start=parse_hhmm(t)))
                print("  → loaded demo set")
                print(render_state(runs, policy))
            elif cmd == "scenario":
                if not args or args[0] not in SCENARIOS:
                    print(f"  available scenarios: {', '.join(SCENARIOS.keys())}")
                    for k, (label, _) in SCENARIOS.items():
                        print(f"    {k}: {label}")
                    continue
                sid = args[0]
                label, items = SCENARIOS[sid]
                runs = [
                    Run(zone=z, start_min=parse_hhmm(t), duration_min=m, original_start=parse_hhmm(t))
                    for z, t, m in items
                ]
                print(f"  → loaded scenario {sid}: {label}")
                print(render_state(runs, policy))
            else:
                print(f"  unknown command: {cmd} (try `help`)")
        except (ValueError, KeyError) as e:
            print(f"  error: {e}")


if __name__ == "__main__":
    sys.exit(repl())
