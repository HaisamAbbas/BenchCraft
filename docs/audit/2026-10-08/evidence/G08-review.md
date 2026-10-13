# G08 independent review

The read-only review examined the seed controls, persisted application/runtime provenance,
initial-run fail-closed path, resume identity comparison, bounded Git subprocess handling,
untracked-file scanner, and run inspection output.

The reviewer reproduced two actionable defects during review. First, changing a non-ignored
untracked prompt/config file left the commit and tracked diff unchanged, so resume could proceed
with different application input. The run identity now hashes those files under explicit list,
count, and content limits; a regression verifies changed untracked content blocks resume.
Second, a Windows junction could make the scanner hash a file outside the application root.
The scanner now rejects reparse points at each path component, and a Windows regression creates
a junction to an external directory and verifies fingerprinting fails closed.

Final independent review: no findings. The reviewer verified the junction fix and confirmed the
human `runs show` additions remain compatible with older Git identity records. Its focused
provenance and run-inspection batch passed 11 tests, followed by 12 passing run-inspection tests
after the last display change. Project verification also passed the 55-test run/CLI/recovery
batch, 6 final focused VCS regressions, Ruff (`src tests plugins`), Mypy (149 source files), and
`git diff --check`.
