"""services.yaml must satisfy HA's own selector schema (v1.60.1).

hassfest validates services.yaml in CI, but nothing did locally — so a selector
HA rejects sailed through `scripts/check.sh` green and only failed after the tag
was pushed (v1.60.0: `step: 0.0001` on a number selector, which is below HA's
minimum step, cascading into a bogus "required key not provided ... ['target']").

This closes that local/CI gap: it runs in the normal suite, using HA's real
selector helper, and skips cleanly on the CI-parity pass where HA isn't
installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML only present with HA installed")
sel = pytest.importorskip(
    "homeassistant.helpers.selector",
    reason="HA not installed (CI-parity pass) — hassfest covers this in CI",
)

SERVICES_YAML = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "complete_irrigation"
    / "services.yaml"
)


def _fields() -> list[tuple[str, str, dict]]:
    data = yaml.safe_load(SERVICES_YAML.read_text())
    out = []
    for service, body in (data or {}).items():
        for name, field in ((body or {}).get("fields") or {}).items():
            out.append((service, name, field))
    return out


def test_services_yaml_parses_and_has_services():
    data = yaml.safe_load(SERVICES_YAML.read_text())
    assert isinstance(data, dict) and data
    assert "add_zone_region" in data  # the v1.60 addition that broke CI


@pytest.mark.parametrize(
    "service,name,field", _fields(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_every_selector_is_valid(service, name, field):
    selector = field.get("selector")
    if selector is None:
        return  # a field may legitimately have no selector
    try:
        sel.selector(selector)
    except Exception as err:  # surface the service+field, not a bare schema error
        pytest.fail(f"{service}.{name} has a selector HA rejects: {err}")


# NOTE: a "every field has a description" check was tried here and dropped — it
# flags 27 pre-existing fields unrelated to this bug. Tightening those is its own
# change, not something to smuggle into a hotfix.


def test_the_regression_that_caused_this_file_is_still_rejected():
    # Guards the guard: if HA ever loosened this, the test above would silently
    # stop covering the exact shape that broke v1.60.0.
    import voluptuous as vol

    with pytest.raises(vol.Invalid):
        sel.selector({"number": {"min": 0, "max": 1, "step": 0.0001, "mode": "box"}})
    sel.selector({"number": {"min": 0, "max": 1, "step": "any", "mode": "box"}})
