from pathlib import Path


def test_manual_release_builds_and_publishes_in_same_workflow_run() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'if [[ "$INPUT_DRY_RUN" == "true" ]]; then' in workflow
    assert 'PUBLISH=false\n              REF="$GITHUB_SHA"' in workflow
    assert 'BUILD=true\n              PUBLISH=true\n              REF="v$VERSION"' in workflow
    assert "this workflow run will build and publish the assets" in workflow
    assert "tag-triggered Release run will build and publish assets" not in workflow


def test_main_release_commit_can_tag_and_publish() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "branches:\n      - main" in workflow
    assert "startsWith(github.event.head_commit.message, 'chore: release v')" in workflow
    assert 'SUBJECT="$(git log -1 --pretty=%s)"' in workflow
    assert "TAG_NEEDED=true" in workflow
    assert 'git push origin "refs/tags/v$VERSION"' in workflow
    assert "git tag --force" not in workflow
    assert "git push --force origin" not in workflow
    assert "Refusing to move existing release tag" in workflow
