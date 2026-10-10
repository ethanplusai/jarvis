"""The voice page's panels share their small DOM pieces.

The Conversation panel and the Business desk had each grown a copy of the
same things — `el`, the armed two-click button, the `<time>` element — and
two copies is how one gets the fix and the other keeps the bug. They live in
`frontend/src/uibits.ts` now; this holds the line. The dashboard's `ui.ts`
is a different bundle with a different `el` signature and is left alone.
"""

import re
from pathlib import Path

SRC = Path(__file__).parent.parent / "frontend" / "src"


def _read(name: str) -> str:
    return (SRC / name).read_text(encoding="utf-8")


def _definitions(pattern: str) -> dict[str, int]:
    found = {}
    for path in SRC.rglob("*.ts"):
        n = len(re.findall(pattern, path.read_text(encoding="utf-8")))
        if n:
            found[path.relative_to(SRC).as_posix()] = n
    return found


def test_the_armed_button_is_defined_once():
    assert _definitions(r"function armed\(") == {"uibits.ts": 1}


def test_the_element_helper_is_defined_once_per_bundle():
    assert _definitions(r"function el<") == {"uibits.ts": 1, "dashboard/ui.ts": 1}


def test_the_time_element_is_built_in_one_place():
    assert _definitions(r'createElement\("time"\)') == {}
    assert _definitions(r'el\("time"') == {"uibits.ts": 1}


def test_both_panels_import_the_shared_pieces():
    for name in ("business.ts", "conversation.ts"):
        assert 'from "./uibits"' in _read(name), name
    assert "armed(" in _read("business.ts") and "armed(" in _read("conversation.ts")
    assert "timeElement(" in _read("business.ts") and "timeElement(" in _read("conversation.ts")
