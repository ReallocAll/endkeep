from __future__ import annotations

from endstone_endkeep.logical.diff import DiffStats
from endstone_endkeep.plugin import EndKeepPlugin
from endstone_endkeep.repository.logicalize import delta_progress_counts


def test_base_progress_formats_millions_and_k_per_second() -> None:
    rendered = EndKeepPlugin._format_progress(
        {
            "current": 1_482_913,
            "total": None,
            "unit": "records",
            "detail": "scan+compress",
            "snapshot": "20261007-203006",
            "elapsed_seconds": 31.0,
            "stage_elapsed_seconds": 31.0,
            "approximate": False,
        }
    )

    assert rendered == ("Progress: 1.48M records · 47.8k/s · snapshot=20261007-203006 · elapsed=00:31")


def test_delta_progress_formats_estimated_percent_millions_and_rate() -> None:
    rendered = EndKeepPlugin._format_progress(
        {
            "current": 1_310_720,
            "total": 2_201_690,
            "unit": "keys",
            "detail": "diff+compress",
            "snapshot": "20261007-234502",
            "elapsed_seconds": 55.0,
            "stage_elapsed_seconds": 24.0,
            "approximate": True,
        }
    )

    assert rendered == ("Progress: ~59.5% · 1.31M keys · 54.6k/s · snapshot=20261007-234502 · elapsed=00:24")


def test_delta_progress_estimate_converges_without_exceeding_total() -> None:
    previous_total = 2_000_000
    stats = DiffStats(
        unchanged=900_000,
        changed=100_000,
        inserted=25_000,
        deleted=50_000,
    )
    processed, estimated_total = delta_progress_counts(stats, previous_total)
    assert processed == 1_075_000
    assert estimated_total == 2_025_000
    assert processed <= estimated_total

    complete = DiffStats(
        unchanged=1_700_000,
        changed=200_000,
        inserted=50_000,
        deleted=100_000,
    )
    processed, estimated_total = delta_progress_counts(complete, previous_total)
    assert processed == estimated_total == 2_050_000


def test_generic_progress_still_formats_explicit_total() -> None:
    rendered = EndKeepPlugin._format_progress(
        {
            "current": 1_284_391,
            "total": 2_163_740,
            "unit": "records",
            "detail": "verifying logical state",
            "snapshot": "20261007-234502",
            "elapsed_seconds": 12.0,
            "stage_elapsed_seconds": 12.0,
            "approximate": False,
        }
    )
    assert rendered.startswith("Progress: 1.28M / 2.16M records (59.4%)")
