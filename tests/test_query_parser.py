from __future__ import annotations

import pytest

from endstone_endkeep.bds.query_parser import QueryManifestError, QueryManifestParser


class FakeTranslatable:
    def __init__(self, text: str, params: list[str]) -> None:
        self.text = text
        self.params = params


def test_parse_manifest_from_translation_parameter() -> None:
    message = FakeTranslatable(
        "commands.save.query.success",
        [
            "level/db/000828.log:107820, level/db/000001.ldb:42, "
            "level/level.dat:3326, level/levelname.txt:5"
        ],
    )
    manifest = QueryManifestParser.parse_messages([message])
    assert manifest.world_name == "level"
    assert manifest.file_count == 4
    assert manifest.total_bytes == 111193
    assert [entry.path.as_posix() for entry in manifest.sidecar_entries] == [
        "level/level.dat",
        "level/levelname.txt",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        "/absolute/file:1",
        "../level.dat:1",
        "level/../outside:1",
        r"level\\db\\CURRENT:1",
        "level/db/A:1, other/db/B:1",
        "level/db/A:not-a-number",
        "level/db/A:1, level/db/A:2",
    ],
)
def test_reject_unsafe_or_malformed_manifest(payload: str) -> None:
    with pytest.raises(QueryManifestError):
        QueryManifestParser.parse(payload)
