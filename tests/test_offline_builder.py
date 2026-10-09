from __future__ import annotations

import subprocess
import sys
import tomllib
from importlib.metadata import distribution
from pathlib import Path

import pytest

from endstone_endkeep.offline.cli import _default_repository, _new_progress_bar, build_parser, install_server_launcher


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


def test_core_import_does_not_load_endstone_runtime() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import endstone_endkeep.logical.format; "
                "loaded = sorted(name for name in sys.modules if name.startswith('endstone')); "
                "assert 'endstone' not in sys.modules, loaded"
            ),
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_wheel_exposes_endkeep_console_script() -> None:
    entry_points = distribution("endstone-endkeep").entry_points
    assert any(entry.name == "endkeep" and entry.value == "endstone_endkeep.offline.cli:main" for entry in entry_points)


def test_cli_discovers_configured_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _default_repository() == tmp_path / "backups" / "repo"
    plugin_config = tmp_path / "plugins" / "endkeep" / "config.toml"
    plugin_config.parent.mkdir(parents=True)
    plugin_config.write_text('[storage]\npath = "custom-backups"\n', encoding="utf-8")
    assert tomllib.loads(plugin_config.read_text(encoding="utf-8"))["storage"]["path"] == "custom-backups"
    assert _default_repository() == tmp_path / "custom-backups" / "repo"
    plugin_config.write_text(f'[storage]\npath = "{tmp_path / "absolute"}"\n', encoding="utf-8")
    assert _default_repository() == tmp_path / "absolute" / "repo"
    assert build_parser().parse_args(["--repo", "manual/repo", "list"]).repo == Path("manual/repo")


def test_server_local_cli_launcher_uses_package_location(tmp_path: Path) -> None:
    launcher = install_server_launcher(tmp_path / "plugins" / "endkeep")
    assert launcher.is_file()
    assert launcher.stat().st_mode & 0o111
    completed = subprocess.run([str(launcher), "--help"], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert "EndKeep repository" in completed.stdout


def test_progress_requires_only_standard_library(capsys: pytest.CaptureFixture[str]) -> None:
    from endstone_endkeep.offline import cli

    assert "tqdm" not in Path(cli.__file__).read_text(encoding="utf-8")
    assert "tqdm" not in Path("requirements-offline.txt").read_text(encoding="utf-8")
    bar = _new_progress_bar(total=2, desc="Checking", unit="obj")
    bar.update(1)
    bar.update(1)
    bar.close()
    assert "Checking: 2/2 obj" in capsys.readouterr().err
