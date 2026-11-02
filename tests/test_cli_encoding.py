"""CLI help must work on legacy terminal encodings too."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("encoding", ["latin-1", "ascii", "utf-8"])
def test_help_on_legacy_terminal_encoding(encoding):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONIOENCODING=encoding + ":strict")
    result = subprocess.run(
        [sys.executable, str(root / "main.py"), "--help"],
        cwd=root, env=env, capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode(encoding)
    assert b"QC Folder Manager" in result.stdout
    assert b"--config" in result.stdout
