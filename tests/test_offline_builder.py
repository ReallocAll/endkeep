from __future__ import annotations

import subprocess
import sys
import tomllib
from importlib.metadata import distribution
from pathlib import Path

import pytest

from endstone_endkeep.offline.cli import _default_repository, _new_progress_bar, _progress_write, build_parser


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


def test_offline_requirements_preserve_tqdm() -> None:
    assert "tqdm>=4.70,<5" in Path("requirements-offline.txt").read_text(encoding="utf-8")


def test_offline_progress_uses_original_tqdm_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    class FakeTqdm:
        def __init__(self) -> None:
            self.kwargs = {}
            self.messages: list[str] = []
            self.count = 0
            self.closed = False

        def __call__(self, **kwargs):
            self.kwargs = kwargs
            return self

        def update(self, amount: int) -> None:
            self.count += amount

        def close(self) -> None:
            self.closed = True

        def write(self, text: str, *, file) -> None:
            assert file is sys.stderr
            self.messages.append(text)

    fake = FakeTqdm()
    monkeypatch.setitem(sys.modules, "tqdm", SimpleNamespace(tqdm=fake))
    bar = _new_progress_bar(total=25, desc="Checking", unit="obj")
    bar.update(5)
    bar.close()
    _progress_write("details")
    assert fake.kwargs == {
        "total": 25,
        "desc": "Checking",
        "unit": "obj",
        "unit_scale": False,
        "dynamic_ncols": True,
        "mininterval": 0.2,
        "file": sys.stderr,
        "leave": True,
    }
    assert fake.count == 5
    assert fake.closed
    assert fake.messages == ["details"]
