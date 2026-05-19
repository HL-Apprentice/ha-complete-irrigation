# P1 — ConflictResolver Prototype

**Status:** prototype (throwaway). Delete when verdict is captured.
**Skill:** matt pocock `prototype` (LOGIC branch)

## Question being answered

Does our 3-policy conflict resolver handle every realistic real-world
scenario before we commit to building the production module?

## How to run

```bash
python3 main.py
```

Drops you into an interactive REPL. Type `help` to see commands.

## Quick tour

```
> add Front 6:00 30          # add a run: zone, start time, duration in minutes
> add Back 6:15 30           # add another run that overlaps
> policy A                   # set conflict policy: A=defer, B=shift earlier, C=split
> resolve                    # show the resolved timeline
> reset                      # clear all runs
> quit                       # exit
```

After every command the prototype prints the current state so you can
see what changed.

## What to validate

Push these scenarios through each policy (A / B / C):

1. Two-way 15-min overlap
2. Back-to-back runs (zero gap → 2-min buffer auto-added)
3. Three-way cascade (one shift creates a new overlap)
4. Schedules that cross midnight
5. Cascading deferrals beyond the cap (2 hours past original or 3 deferrals)
6. Mix of overlapping and non-overlapping runs

When done, write up the verdict in `VERDICT.md` (and delete this prototype).
