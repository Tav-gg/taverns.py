from __future__ import annotations

import re
from pathlib import Path

import taverns

ROOT = Path(__file__).resolve().parent.parent


def test_every_version_string_agrees():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE).group(1)
    rest = (ROOT / "taverns" / "rest.py").read_text(encoding="utf-8")
    assert taverns.__version__ == version
    assert f'"User-Agent": "taverns.py/{version}"' in rest
