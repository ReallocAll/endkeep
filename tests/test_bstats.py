from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from endstone_endkeep.plugin import EndKeepPlugin


def _plugin_stub() -> SimpleNamespace:
    warnings: list[str] = []
    return SimpleNamespace(
        _metrics=None,
        BSTATS_PLUGIN_ID=34593,
        logger=SimpleNamespace(warning=warnings.append),
        warnings=warnings,
    )


def test_bstats_uses_builtin_endstone_metrics_and_only_starts_once() -> None:
    plugin = _plugin_stub()
    with patch("endstone_endkeep.plugin.Metrics") as metrics_factory:
        EndKeepPlugin._start_metrics(plugin)
        EndKeepPlugin._start_metrics(plugin)

        metrics_factory.assert_called_once_with(plugin, 34593)
        instance = metrics_factory.return_value
        assert plugin._metrics is instance

        EndKeepPlugin._stop_metrics(plugin)
        EndKeepPlugin._stop_metrics(plugin)
        instance.shutdown.assert_called_once_with()
        assert plugin._metrics is None


def test_bstats_initialization_error_does_not_disable_backup() -> None:
    plugin = _plugin_stub()

    with patch("endstone_endkeep.plugin.Metrics", side_effect=RuntimeError("network setup failed")):
        EndKeepPlugin._start_metrics(plugin)

    assert plugin._metrics is None
    assert any("backup features unaffected" in warning for warning in plugin.warnings)


def test_bstats_shutdown_error_is_contained() -> None:
    plugin = _plugin_stub()
    metrics = SimpleNamespace(shutdown=lambda: (_ for _ in ()).throw(RuntimeError("shutdown failed")))
    plugin._metrics = metrics

    EndKeepPlugin._stop_metrics(plugin)

    assert plugin._metrics is None
    assert any("shut down bStats" in warning for warning in plugin.warnings)


def test_bstats_not_started_if_repository_initialization_fails() -> None:
    plugin = _plugin_stub()
    plugin.save_default_config = lambda: None
    plugin._reconcile_and_reload_config = lambda: None
    plugin._configure_runtime = lambda: (_ for _ in ()).throw(RuntimeError("repository failed"))
    plugin.logger.critical = plugin.warnings.append
    plugin._start_metrics = lambda: (_ for _ in ()).throw(AssertionError("metrics started"))

    EndKeepPlugin.on_enable(plugin)

    assert any("Failed to enable EndKeep" in warning for warning in plugin.warnings)


def test_bstats_shutdown_runs_even_if_runtime_close_raises() -> None:
    plugin = _plugin_stub()
    plugin._close_runtime = lambda: (_ for _ in ()).throw(RuntimeError("runtime close failed"))
    stopped = []
    plugin._stop_metrics = lambda: stopped.append(True)

    try:
        EndKeepPlugin.on_disable(plugin)
    except RuntimeError as exc:
        assert str(exc) == "runtime close failed"
    else:
        raise AssertionError("expected runtime close error")

    assert stopped == [True]
