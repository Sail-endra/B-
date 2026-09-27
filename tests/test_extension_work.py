from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_extension_bounded_work_and_course_matching():
    repo = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["node", "--test", str(repo / "tests" / "extension_work.test.cjs")],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
