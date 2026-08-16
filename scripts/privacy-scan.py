#!/usr/bin/env python3
"""Deterministic privacy scan of the whole tracked tree — NOT the diff.

WHY THIS EXISTS
---------------
A real machine on the author's LAN was published as a documentation example in
services.yaml. It shipped in v1.44.0 and stayed public across 32 releases. Every
one of those releases ran a manual pre-publish review, and none caught it.

The reason is structural, and worth stating so nobody "fixes" it by resolving to
be more careful:

  * The line was in a release DIFF exactly once. After that the file was never
    touched again, so a diff-scoped review could not see it no matter how
    carefully it was run.
  * The manual sweep searched by credential SHAPE, which is right for
    credentials and useless for a private IP — an RFC1918 address looks nothing
    like a credential.
  * A language model asked to "check for private information" samples; it does
    not enumerate. That is the wrong tool for fixed patterns.

So this scan is deterministic, covers every tracked file, and fails the build.
Judgment calls (is this hostname real? is this a person's name?) still belong to
a human or a model — this only has to make the FINDING part exhaustive.

DESIGN NOTE
-----------
This file deliberately does NOT encode the author's home coordinates, address,
or hostnames. Doing so would put the very data we are protecting into a public
repo. Instead it flags coordinate-SHAPED and host-SHAPED values and requires
each to be explicitly allowlisted below, so adding one is a visible decision.

Usage:  python3 scripts/privacy-scan.py
Exit:   0 clean, 1 findings.
"""

from __future__ import annotations

import re
import subprocess
import sys

# Values reviewed and deliberately accepted. Keep this list SHORT and justified —
# every entry is a place the scan has been told to stay quiet.
ALLOWLIST = {
    # Generic RFC5737/RFC1918-style documentation endpoints shown in the UI and
    # in services.yaml. Not anyone's real machine.
    "192.168.1.10",
    "192.168.1.50",
    # Loopback / any-address are not private information.
    "127.0.0.1",
    "0.0.0.0",
    # Synthetic 60 m x 60 m test bbox on a round base point (see
    # tests/test_yard_map_template.py). Round to trailing zeros on purpose so it
    # reads as manufactured; it is not a parcel anyone lives on.
    "33.20000000, -111.49935567",
    "33.20000000,-111.49935567",
}

# Files where example data legitimately lives. Still scanned — this only widens
# what counts as an acceptable hit, it does not skip the file.
BINARY_EXT = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".zip", ".pdf")


def _rule(name: str, pattern: str, why: str) -> tuple[str, re.Pattern[str], str]:
    return (name, re.compile(pattern), why)


RULES = [
    _rule(
        "private-ip",
        r"(?<![\d.])(?:192\.168|10\.|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?![\d.])",
        "a machine on someone's LAN; reveals internal addressing",
    ),
    _rule(
        "mac-address",
        r"(?<![:\w])(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}(?![:\w])",
        "hardware identifier, ties a device to a person",
    ),
    _rule(
        "local-user-path",
        r"/(?:Users|home)/[A-Za-z0-9._-]+/",
        "a developer's home directory, usually their real name",
    ),
    _rule(
        "email",
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        "personal contact detail",
    ),
    _rule(
        # A decimal coordinate PAIR. This is the one that matters most for a
        # yard-mapping integration: a lat/lon pair is a house.
        "coordinate-pair",
        r"(?<![\d.])[-+]?(?:[0-8]?\d|90)\.\d{3,}\s*,\s*[-+]?(?:1[0-7]\d|[0-9]?\d|180)\.\d{3,}(?![\d.])",
        "a decimal lat/lon pair locates a physical address",
    ),
    _rule(
        "us-street-address",
        r"\b\d{2,6}\s+[NSEW]\.?\s+[A-Z][a-z]+\s+(?:St|Ave|Rd|Ct|Dr|Ln|Blvd|Way|Pl)\b",
        "a street address",
    ),
    _rule(
        "us-zip-plus-state",
        r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b",
        "state + ZIP, usually part of a real address",
    ),
]

# Filenames that must never be tracked at all.
JUNK = re.compile(r"(?:^|/)(?:\.DS_Store|\.env(?:\.[\w.]+)?|.*\.log)$")


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout
    return [f for f in out.splitlines() if f]


def scan() -> list[tuple[str, int, str, str, str]]:
    findings: list[tuple[str, int, str, str, str]] = []
    for path in tracked_files():
        if JUNK.search(path):
            findings.append((path, 0, "junk-file", path, "must not be tracked"))
            continue
        if path.lower().endswith(BINARY_EXT):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            for name, rx, why in RULES:
                for m in rx.finditer(line):
                    hit = m.group(0)
                    if hit in ALLOWLIST:
                        continue
                    # noreply committer identities and obvious example domains
                    # are not personal contact details.
                    if name == "email" and (
                        "users.noreply.github.com" in hit
                        # RFC2606 reserved domains, at any subdomain depth
                        # (user@api.example.com is as fake as user@example.com)
                        or re.search(r"@(?:[\w-]+\.)*(?:example|test|invalid|localhost)\b", hit)
                        # "icon@2x.png" and friends are filenames, not addresses
                        or re.match(r"^[\w.-]+@\d+x\.", hit)
                    ):
                        continue
                    findings.append((path, n, name, hit, why))
    return findings


def main() -> int:
    findings = scan()
    if not findings:
        print(f"privacy scan clean — {len(tracked_files())} tracked files, 0 findings")
        return 0

    print(f"privacy scan FOUND {len(findings)} item(s):\n", file=sys.stderr)
    for path, line, kind, hit, why in findings:
        where = f"{path}:{line}" if line else path
        print(f"  [{kind}] {where}\n      {hit}\n      why: {why}", file=sys.stderr)
    print(
        "\nEach of these ships to every person who installs this integration.\n"
        "Fix it, or add the exact value to ALLOWLIST in scripts/privacy-scan.py\n"
        "with a comment saying why it is safe.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
