#!/usr/bin/env python3
"""Assemble panel i18n language packs (v1.58).

Merges extracted translation part-files into the CI_I18N block of the panel JS,
between the CI-I18N-PACKS-BEGIN/END markers. Patterns are emitted as inline
RegExp literals. Usage:

    python3 scripts/i18n_assemble.py de part1.json part2.json [...]

Idempotent: re-running replaces the whole block. On clashes: STRING keys — later
parts win; PATTERN sources — first occurrence wins (order = test order, so keep
specific patterns in earlier parts than generic ones).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PANEL = Path(__file__).resolve().parent.parent / (
    "custom_components/complete_irrigation/frontend/complete-irrigation-panel.js"
)
BEGIN = "// CI-I18N-PACKS-BEGIN"
END = "// CI-I18N-PACKS-END"


def main() -> None:
    lang = sys.argv[1]
    strings: dict[str, str] = {}
    patterns: list[list[str]] = []
    seen_pat: set[str] = set()
    for p in sys.argv[2:]:
        with open(p) as fh:
            data = json.load(fh)
        for k, v in (data.get("strings") or {}).items():
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                strings[k] = v
        for pair in data.get("patterns") or []:
            if (
                isinstance(pair, list)
                and len(pair) == 2
                and all(isinstance(x, str) for x in pair)
                and pair[0] not in seen_pat
            ):
                # sanity: the regex must compile in JS-ish python flavor
                import re

                try:
                    re.compile(pair[0])
                except re.error:
                    continue
                seen_pat.add(pair[0])
                patterns.append(pair)

    # Drop identity translations (waste bytes, no effect).
    strings = {k: v for k, v in strings.items() if k != v}

    def js_str(s: str) -> str:
        return json.dumps(s, ensure_ascii=False)

    lines = ["  const CI_I18N = {};", f"  CI_I18N[{js_str(lang)}] = {{", "    strings: {"]
    for k in sorted(strings):
        lines.append(f"      {js_str(k)}: {js_str(strings[k])},")
    lines.append("    },")
    lines.append("    patterns: [")
    for src, rep in patterns:
        # RegExp-from-string keeps escaping simple + JSON-safe.
        lines.append(f"      [new RegExp({js_str(src)}), {js_str(rep)}],")
    lines.append("    ],")
    lines.append("  };")
    block = "\n".join(lines)

    src_text = PANEL.read_text()
    b = src_text.index(BEGIN) + len(BEGIN)
    e = src_text.index(END)
    out = src_text[:b] + "\n" + block + "\n  " + src_text[e:]
    PANEL.write_text(out)
    print(f"injected {len(strings)} strings + {len(patterns)} patterns for '{lang}'")


if __name__ == "__main__":
    main()
