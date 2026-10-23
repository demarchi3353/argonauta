import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_patch_js_units():
    # Pass files, not the directory: Node 22 would try to load a directory argument as a module.
    files = sorted(str(p) for p in (ROOT / "tests" / "js").glob("*.test.js"))
    proc = subprocess.run(["node", "--test", *files], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
