# Brand assets

Visual identity for the integration. Re-generate any time the icon
needs to change by running `python3 scripts/build-icons.py` from the
repo root.

## Files

| File | Size | Purpose |
|---|---|---|
| `icon.svg` | scalable | Authoritative vector source for the water-drop icon. Hand-edit this if the design needs updates, then re-run the build script. |
| `icon.png` | 256×256 | HACS branding (1x) and inline use in the README. Transparent background. |
| `icon@2x.png` | 512×512 | HACS branding @2x for hi-DPI displays. Same content as `icon.png`. |
| `social-preview.png` | 1280×640 | GitHub social preview image (set in repo Settings → Social preview). Drives link previews on Twitter/Discord/Slack. Generated programmatically by `scripts/build-icons.py` (Python+PIL); the icon is composited onto a dark gradient backdrop with the integration name + tagline. |

## Where each file is used

### GitHub

- **README header** — `icon.png` shown at the top of the repo README.
- **Repo social preview** — `social-preview.png` set manually by the
  repo owner at:
  *Repo Settings → Social preview → Upload an image*.
  Drives the OG image shown by Twitter/Discord/Slack link unfurls and
  the repo's preview when shared.

### HACS (one-time submission)

The icon shown next to the integration in HACS comes from the
[home-assistant/brands](https://github.com/home-assistant/brands) repo,
not this repo. To get our water-drop icon there:

1. Fork [`home-assistant/brands`](https://github.com/home-assistant/brands).
2. Create the directory `custom_integrations/complete_irrigation/`.
3. Copy our PNGs in:
   - `icon.png` (256×256) → `custom_integrations/complete_irrigation/icon.png`
   - `icon@2x.png` (512×512) → `custom_integrations/complete_irrigation/icon@2x.png`
4. Optionally also add a logo (typically wider, e.g. 256×56 with a
   wordmark next to the icon). Not required.
5. Open a PR to home-assistant/brands. Their reviewers usually merge
   within a few days; once merged, the icon shows up in HACS
   automatically on the next polling cycle.

Reference docs: <https://www.home-assistant.io/docs/blog/2020-05-26-logos-custom-integrations/>

## Regenerating

```bash
python3 scripts/build-icons.py
```

Produces all three PNGs from the canonical Python spec. If you want
to change the color palette, edit the constants at the top of
`scripts/build-icons.py` AND update `icon.svg` so both source-of-truth
files stay in sync.
