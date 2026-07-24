# Panel translations (i18n)

The custom panel translates itself at render time through per-language packs
compiled into `frontend/complete-irrigation-panel.js` (between the
`CI-I18N-PACKS-BEGIN/END` markers). The panel follows the user's HA profile
language; anything untranslated falls back to English.

## Regenerating / adding a language

Each language is a set of part files: `{"strings": {"English": "Übersetzung"},
"patterns": [["regex with (\\d+) groups", "replacement with $1"]]}`.

- **strings** — exact text-node matches (entities decoded, trimmed).
- **patterns** — for dynamic text; applied **cumulatively in order**, so put
  specific patterns in earlier parts than generic ones (dedup is first-wins for
  pattern sources, later-wins for string keys).

Build (replaces the whole pack block, idempotent):

    python3 scripts/i18n_assemble.py de i18n/de/i18n-de-part*.json

Then `node --check` the panel and run `scripts/check.sh`.

The HA **setup wizard** translates separately via
`custom_components/complete_irrigation/translations/<lang>.json` (mirror
`en.json`'s keys exactly).
