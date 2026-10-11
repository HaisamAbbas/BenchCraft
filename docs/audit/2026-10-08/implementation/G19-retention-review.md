# G19.6 independent review

The first review found that `open_directory()` converted an already-extended Windows drive path
as though it were a UNC path. `_extended_path()` now preserves existing `\\?\` paths before
converting ordinary UNC paths, with regressions for drive, UNC, extended-drive, and extended-UNC
forms. The reviewer confirmed the helper regression and successfully opened the project through an
extended path.

The second review reported no remaining code findings. It flagged the implementation note's stale
test total; that count has been updated to the final result of 99 passed and 16 skipped.

Verification on this Windows host passed: the combined workspace, integrity, and artifact batch
(99 passed, 16 skipped); focused retention, usage, and extended-path tests (12 passed, 3 skipped);
Ruff lint and formatting; Mypy for the three changed Python modules; and `git diff --check`.
POSIX-only directory-descriptor, fsync, and race tests were skipped and need execution on a POSIX
host or CI runner.
