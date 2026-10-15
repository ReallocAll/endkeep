from __future__ import annotations

import sys
from pathlib import Path

from endstone_endkeep.repository.lock import RepositoryLock
from endstone_endkeep.repository.logicalize import Logicalizer
from endstone_endkeep.repository.transaction import RepositoryTransaction
from tests.standalone_fixture import add_raw, build_fixture


def main() -> None:
    storage, point, ready = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    build_fixture(storage)
    add_raw(storage, "20261006-200000", [(b"a", b"last"), (b"c", b"3")], b"last")
    original = RepositoryTransaction.commit

    def commit(transaction, manifest):
        def pause(current):
            if current == point:
                ready.write_text(current, encoding="ascii")
                # Parent kills this process; no Python cleanup can run.
                import threading

                threading.Event().wait(30)
                raise TimeoutError("parent did not terminate crash fixture")

        transaction.fault_hook = pause
        return original(transaction, manifest)

    RepositoryTransaction.commit = commit
    with RepositoryLock(storage / "worker-owner"), RepositoryLock(storage / "repo"):
        Logicalizer(storage, compression_level=6, compression_threads=1).logicalize(storage / "raw" / "20261006-200000")


if __name__ == "__main__":
    main()
