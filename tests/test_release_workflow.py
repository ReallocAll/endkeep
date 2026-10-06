from pathlib import Path


def test_manual_release_builds_and_publishes_in_same_workflow_run() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'if [[ "$INPUT_DRY_RUN" == "true" ]]; then' in workflow
    assert 'PUBLISH=false\n              REF="$GITHUB_SHA"' in workflow
    assert 'BUILD=true\n              PUBLISH=true\n              REF="v$VERSION"' in workflow
    assert "this workflow run will build and publish the assets" in workflow
    assert "tag-triggered Release run will build and publish assets" not in workflow
