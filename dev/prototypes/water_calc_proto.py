#!/usr/bin/env python3
"""PROTOTYPE TUI — throwaway shell over water_calc.py. DELETE when answered.

Drive the drip water calculator by hand: change ETo (weather), change a
loop's runtime, and watch the per-plant emitter recommendations re-balance.
The interesting moments are "wait, that shouldn't be possible" — those are
bugs in the *idea*. The keeper is water_calc.py; this shell is disposable.

Run from the repo root:
    python3 dev/prototypes/water_calc_proto.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import water_calc as wc  # noqa: E402

B, D, R = "\x1b[1m", "\x1b[2m", "\x1b[0m"
GREEN, YELLOW, RED, CYAN = "\x1b[32m", "\x1b[33m", "\x1b[31m", "\x1b[36m"

# ── sample Phoenix yard (in-memory; grounded in the real loops) ──────
ETO = 1.8  # in/week — Phoenix high-summer reference ET
EFF = wc.DEFAULT_EFFICIENCY

LOOPS = {
    "citrus": wc.Loop("citrus", runtime_minutes=240, runs_per_week=2, max_flow_gph=16),
    "trees": wc.Loop("trees", runtime_minutes=270, runs_per_week=1, max_flow_gph=20),
    "shrubs": wc.Loop("shrubs", runtime_minutes=135, runs_per_week=3, max_flow_gph=12),
    "flowers": wc.Loop("flowers", runtime_minutes=60, runs_per_week=3, max_flow_gph=8),
    "garden": wc.Loop("garden", runtime_minutes=15, runs_per_week=7, max_flow_gph=10),
}
PLANTS = [
    wc.Plant("Lemon tree", "high", 113, "citrus"),
    wc.Plant("Orange tree", "high", 95, "citrus"),
    wc.Plant("Mesquite", "low", 150, "trees"),
    wc.Plant("Palo verde", "low", 130, "trees"),
    wc.Plant("Saguaro cactus", "very_low", 9, "shrubs"),  # design conflict here
    wc.Plant("Oleander", "moderate", 30, "shrubs"),
    wc.Plant("Texas sage", "low", 16, "shrubs"),
    wc.Plant("Petunias bed", "moderate", 6, "flowers"),
    wc.Plant("Marigolds bed", "moderate", 5, "flowers"),
    wc.Plant("Tomatoes", "moderate", 8, "garden"),
    wc.Plant("Peppers", "moderate", 5, "garden"),
]


def emitter_label(combo: list[tuple[int, float]]) -> str:
    if not combo:
        return "—"

    def fmt(s: float) -> str:
        return str(int(s)) if s == int(s) else str(s)

    return " + ".join(f"{c}×{fmt(s)}" for c, s in combo) + " GPH"


def status_color(status: str) -> str:
    return {"OK": GREEN, "UNDER": YELLOW, "OVER": RED}.get(status, "")


def render() -> None:
    print("\x1b[2J\x1b[H", end="")
    print(f"{B}🌵  Drip Water Calculator — PROTOTYPE{R}   {D}(WUCOLS plant factor × ETo){R}")
    print(
        f"{B}ETo{R} {CYAN}{ETO:.2f} in/wk{R}   {B}drip eff{R} {EFF:.2f}   "
        f"{D}formula: need = ETo × PF × area × 0.623 / eff{R}\n"
    )
    for lid, loop in LOOPS.items():
        plants = [p for p in PLANTS if p.loop_id == lid]
        rep = wc.loop_report(loop, plants, ETO, EFF)
        print(
            f"{B}▌ {lid.upper()}{R}  {D}runtime{R} {loop.runtime_minutes}m  "
            f"{D}runs/wk{R} {loop.runs_per_week}  {D}flow{R} "
            f"{rep['total_flow_gph']:.1f}/{loop.max_flow_gph:.0f} GPH  "
            f"{D}suggested runtime ≈ {rep['suggested_runtime']}m{R}"
        )
        print(
            f"  {D}{'plant':<16}{'cat':<10}{'need/wk':>8}  {'emitters':<16}"
            f"{'gives/wk':>9}  status{R}"
        )
        for row in rep["rows"]:
            p = row["plant"]
            col = status_color(row["status"])
            print(
                f"  {p.name:<16}{p.wucols_category:<10}{row['need_week']:>7.1f}g  "
                f"{emitter_label(row['emitters']):<16}{row['delivered_week']:>8.1f}g  "
                f"{col}{row['status']:<5} {row['pct_off']:+.0%}{R}"
            )
        for w in rep["warnings"]:
            print(f"  {YELLOW}⚠ {w}{R}")
        print()
    print(
        f"{D}cmds:{R} {B}eto{R} <in/wk>  {B}runtime{R} <loop> <min>  {B}runs{R} <loop> <n>  "
        f"{B}auto{R} <loop>  {B}eff{R} <0-1>  {B}r{R}efresh  {B}q{R}uit"
    )


def handle(cmd: str) -> bool:
    """Mutate the in-memory yard from one command line. Returns False to quit."""
    global ETO, EFF
    parts = cmd.split()
    if not parts:
        return True
    op = parts[0].lower()
    try:
        if op in ("q", "quit"):
            return False
        if op in ("r", ""):
            return True
        if op == "eto":
            ETO = max(0.1, float(parts[1]))
        elif op == "eff":
            EFF = min(1.0, max(0.1, float(parts[1])))
        elif op == "runtime":
            lid, mins = parts[1], int(parts[2])
            lp = LOOPS[lid]
            LOOPS[lid] = wc.Loop(lp.id, max(1, mins), lp.runs_per_week, lp.max_flow_gph)
        elif op == "runs":
            lid, n = parts[1], int(parts[2])
            lp = LOOPS[lid]
            LOOPS[lid] = wc.Loop(lp.id, lp.runtime_minutes, max(1, n), lp.max_flow_gph)
        elif op == "auto":
            lid = parts[1]
            lp = LOOPS[lid]
            plants = [p for p in PLANTS if p.loop_id == lid]
            best = wc.suggest_runtime(lp, plants, ETO, EFF)
            LOOPS[lid] = wc.Loop(lp.id, best, lp.runs_per_week, lp.max_flow_gph)
    except (IndexError, ValueError, KeyError):
        pass  # prototype: ignore malformed input, just re-render
    return True


def main() -> None:
    render()
    while True:
        try:
            line = input("\n> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not handle(line):
            break
        render()
    print("bye 👋")


if __name__ == "__main__":
    main()
