from __future__ import annotations

from pathlib import Path

import pytest

from endstone_endkeep.repository.lock import RepositoryLock, RepositoryLockError


def test_repository_lock_is_exclusive(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    with RepositoryLock(repo):
        with pytest.raises(RepositoryLockError):
            with RepositoryLock(repo):
                pass
