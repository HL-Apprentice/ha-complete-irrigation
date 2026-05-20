# ADR 0002 — Panel layout: Variant 2 (sidebar nav) with collapsible bar

**Date:** 2026-05-19
**Status:** Accepted
**Supersedes:** P2 prototype (deleted)

## Context

Three radically different panel layouts were prototyped (`prototypes/p2_panel_layout/`):

1. Single full-width column — original spec
2. Sidebar nav + main content
3. Tabbed top + main content

Each rendered the same mock data (3 zones, full Tempest weather header,
today/this-week calendar strip, status states for idle/running/skipped).

User flipped through all three with both light and dark themes.

## Decision

**Variant 2 — Sidebar nav + main content area.**

Additional requirement on top of the prototype:

- **The sidebar must be collapsible/hideable** via a toggle. When collapsed,
  the sidebar shrinks to an icon-only rail (or hides entirely on narrow
  screens), reclaiming horizontal space for the main content. Collapsed
  state persists across panel visits.

## Rationale

- The sidebar separates browsing modes ("Today" view vs. Schedules,
  Zones, Sensors, Settings) cleanly without cluttering any single screen.
- Each section can grow features without affecting the others.
- Collapsibility addresses the only concrete downside of the sidebar
  (less horizontal room for the Today dashboard).
- Familiar pattern to anyone who's used HA's own sidebar or modern
  admin apps.

## Implications for Slice 1 and beyond

1. **Lit panel structure**:
   - Top-level component renders a `<nav>` sidebar + `<main>` content.
   - Sidebar items (Today / Schedules / Zones / Sensors / Weather /
     Notifications / Settings) drive a simple client-side router.
   - Default view: **Today** — weather header + calendar strip + zone tiles.

2. **Collapse behavior**:
   - Hamburger/chevron toggle in the sidebar header.
   - Three states: expanded (full text), collapsed (icon-only rail,
     ~56px wide), hidden (on narrow viewports < 700px, sidebar
     auto-hides to a hamburger menu).
   - Persistent: save state to `localStorage` keyed to the integration.

3. **Section routing**:
   - Use URL hash or HA's panel routing (`config.url_path` segments)
     for deep-linkable sections.
   - Each section is a separate Lit element so they load lazily.

4. **Theme variables**:
   - Continue using `var(--card-background-color)`, `var(--primary-color)`,
     etc. (already validated in the prototype).
   - Sidebar uses `var(--card-background-color)` with a divider, just
     like the production HA sidebar.

5. **Mobile**:
   - At < 700px width, sidebar auto-collapses to hidden.
   - Hamburger button in the top-left of main content slides the
     sidebar in as an overlay.

## Consequences

- Slice 1 (PRD issue #4) implements the panel skeleton with all sidebar
  items as placeholders; subsequent slices fill in section content.
- The prototype's `style.css` selectors for `.layout-2 .sidebar` and
  `.layout-2 .main` are a good starting point — adapt to Lit's shadow
  DOM and add the collapse states.
- No design changes needed beyond the prototype's variant 2.
