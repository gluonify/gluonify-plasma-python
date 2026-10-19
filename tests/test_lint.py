"""The code follows the ruff rules of pyproject.toml (gluonify-plasma README, "Quality gates"): a violation fails the tests."""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ruff_finds_no_violation() -> None:
    ruff = shutil.which("ruff", path=str(Path(sys.executable).parent)) or shutil.which("ruff")
    assert ruff, "ruff is missing: `uv sync` installs it (dev group)"
    result = subprocess.run([ruff, "check", str(ROOT)], capture_output=True, text=True, check=False, cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
