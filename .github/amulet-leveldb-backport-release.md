# Unofficial Amulet-LevelDB 3.0.7a0 Python 3.11–3.14 backport

**These are third-party, unofficial binary builds of Amulet-Team/Amulet-LevelDB**, not official Amulet-Team releases and not EndKeep itself.

Four **separate Linux x86-64 Wheels** are attached. Select the one whose CPython ABI matches the Python interpreter **running Endstone or your offline CLI**:

| Python | Wheel filename contains |
|---|---|
| Python 3.11 | `-cp311-cp311-manylinux_` |
| Python 3.12 | `-cp312-cp312-manylinux_` |
| Python 3.13 | `-cp313-cp313-manylinux_` |
| Python 3.14 | `-cp314-cp314-manylinux_` |

Built from upstream **3.0.7a0**, Git commit `736228e375e9f535b6522bd28168fc306a5354b9`. The only patch to the dependency project is changing `Requires-Python` from `>=3.14` to `>=3.11`. No changes are made to LevelDB's native C++ implementation or the pinned pybind11 dependencies.

The version in these experimental Wheels is `3.0.7a0+0.g736228e.dirty`; the `dirty` suffix comes from Versioneer detecting that metadata patch. It is **not** the original upstream PyPI Wheel.

**Installation**

1. Check your interpreter: `python -V`.
2. Download the single matching `.whl` file, **not** all four.
3. Install into that interpreter: `python -m pip install ./amulet_leveldb-*.whl`. If using the wildcard, make sure only one downloaded Wheel matches.

The Wheels were built with `cibuildwheel`, repaired with `auditwheel`, installed in isolated Linux environments, and run through a native LevelDB smoke test plus upstream `test_db.py` test suite on all four Python versions. See the CI run associated with this release and `SHA256SUMS` for integrity checking.

**Scope**: Linux x86-64 only, glibc ≥2.27 based on actual generated tags. Not validated with live Endstone/BDS worlds; test backup + restore with disposable world copies before deploying on a production server.

**Licensing**: Amulet-LevelDB is distributed under the **Amulet Team License 1.0.0**, which places limits on commercial and competing uses. Its license is included in every Wheel. EndKeep's MIT license is separate. Ensure your distribution and use comply with upstream's terms.

**Source**: https://github.com/Amulet-Team/Amulet-LevelDB/tree/3.0.7a0

**Build automation**: https://github.com/ReallocAll/endkeep/tree/experiment/amulet-leveldb-cp311-cp314
