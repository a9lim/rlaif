## What

<!-- One or two sentences on what changed -->

## Why

<!-- What problem does this solve? Please link issues with "Fixes #N" if applicable -->

## Test plan

- [ ] `uv run ruff check .` passes
- [ ] `uv run pyright src/rlaif/` passes
- [ ] `uv run pytest` passes
- [ ] `uv run rlaif dry-run` exits 0
- [ ] If this touches `safety.py`: I have re-read `tests/test_safety.py` and added or updated tests for the new behavior
- [ ] If this changes a tool description string: I have updated the matching `SPEC_*_DESCRIPTION` in `tests/test_server.py` in the same PR
- [ ] If this is a release (version bump in `src/rlaif/__init__.py`): I have read CONTRIBUTING.md and confirm the release workflow will publish to PyPI on merge

## Notes

<!-- Anything reviewers should know: safety implications, followups, known limitations. If this is a config or CLI change, please also note whether the README needs an update. -->
