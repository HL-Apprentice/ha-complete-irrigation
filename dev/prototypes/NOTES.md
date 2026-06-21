# PROTOTYPE — drip water calculator (v2 "plant-aware irrigation")

**Throwaway.** Run: `python3 dev/prototypes/water_calc_proto.py`
Keeper = `water_calc.py` (pure logic). Shell = `water_calc_proto.py` (delete after).

## Question it answers

Can plants with **different** water needs share **one** irrigation loop and
each still get the right amount of water by tuning **emitters** (count × GPH),
given that every plant on a loop shares the same runtime? And does the
**WUCOLS plant-factor × ETo** model produce sane, fine-tunable emitter recs?

Formula: `need_gal/wk = ETo(in/wk) × plant_factor × canopy_area_ft² × 0.623 / drip_eff`
then size emitters so `delivered ≈ need` at the loop's shared runtime.

## Answer (verdict — 2026-06-16)

**YES for moderately-different plants; the real product value is detecting
when plants are too mismatched to share a loop.**

- The model produces sane, tunable recommendations (Garden/Flowers/Trees
  loops all balance to OK with simple emitter sets).
- Emitter tuning **does** reconcile plants whose needs are within a few× of
  each other on a shared runtime.
- It **cannot** reconcile wildly-mismatched plants: a very-low cactus
  (0.22 GPH) on the same loop as a moderate oleander (17× more) means the
  cactus over-waters (even 1×0.5 GPH is too much) OR the thirsty plant needs
  an impractical emitter count that blows the line-flow capacity. The tool
  correctly **flags "split onto separate loops"** instead of emitting a bad
  config. → The app should DETECT + WARN on loop/plant mismatch, not hide it.
- **Loop flow-capacity check is essential**: citrus at a 240 m / 2×-wk runtime
  needs ~35 GPH but a typical line does ~16 — the tool reveals the loop
  physically can't deliver, pointing to longer/more-frequent runs or splitting.
- **"Suggested runtime" must co-optimize with capacity and frequency**, not
  runtime alone (shortening runtime fixed the cactus but exploded the
  oleander's emitter count past capacity).

## Implications for the real v2 build (on the v2 branch)

1. Keep `water_calc.py`'s pure functions — lift them as the hydraulics core.
2. Per-loop "design report" with OK/UNDER/OVER + mismatch + capacity warnings
   is the headline feature, not just emitter numbers.
3. Plant water need = WUCOLS category (baseline) **×** ETo feed (weather) —
   both, as the user asked. Wire ETo from a weather/ET source; WUCOLS factor
   from a species picker (manual category to start).
4. Add: co-optimize runtime + runs/week + emitters; suggest loop re-grouping
   when a loop holds irreconcilable plants.
5. Spray/rotor zones (turf) use a different delivery model than drip emitters —
   out of scope for this calculator; handle separately.
