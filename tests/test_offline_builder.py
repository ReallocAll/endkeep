from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_generated_offline_script_is_self_contained(tmp_path: Path) -> None:
    output = tmp_path / "endkeep-offline.py"
    subprocess.run(
        [sys.executable, "tools/build_offline.py", "--output", str(output)],
        check=True,
    )
    generated = output.read_text(encoding="utf-8")
    assert "from endstone_endkeep" not in generated
    assert "import endstone_endkeep" not in generated
    assert "_endkeep_standalone" in generated

    completed = subprocess.run(
        [sys.executable, str(output), "--help"],
        check=False,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Offline EndKeep repository" in completed.stdout
