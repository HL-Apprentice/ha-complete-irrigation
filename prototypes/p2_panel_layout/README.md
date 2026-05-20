# P2 — Panel Layout Prototype

**Status:** prototype (throwaway). Pick a winner, then delete.
**Skill:** matt pocock `prototype` (UI branch)

## Question being answered

What's the right layout for the custom panel? Three radically different
options. Same mock data in all three. You pick the winner.

## How to run

```bash
cd prototypes/p2_panel_layout
python3 -m http.server 8765
```

Then open: **http://localhost:8765**

Once it's loaded, switch between variants using the buttons at the
bottom of the page. You can also flip light/dark theme via the toggle
to make sure both look reasonable.

You can stop the server when done by pressing `Ctrl+C` in the terminal.

## What the variants are

1. **Single full-width column** — everything stacks vertically. Weather at
   the top, calendar below, then zone tiles in a wide row. Closest to
   what you described in the requirements.

2. **Sidebar nav + main content** — a vertical menu on the left lets you
   jump between Today / Schedules / Zones / Sensors / Settings. The right
   side shows whatever section you're on.

3. **Tabbed top** — same idea as #2 but with the menu as tabs across the
   top instead of a sidebar.

## What to look for

- Which feels easier to scan at a glance?
- Which gives the weather + calendar + zone status enough room to breathe?
- Which feels less cluttered on a phone-sized window? (try resizing)
- Does either theme look broken?

## When done

Reply with a number (1, 2, or 3). I'll capture the verdict, delete the
prototype, and start building the real panel against the winner.
