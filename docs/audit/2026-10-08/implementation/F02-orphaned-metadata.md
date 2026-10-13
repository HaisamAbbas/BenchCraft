# F02 follow-up — Incomplete installed-distribution metadata

## Defect

Python 3.12 exposed orphaned `*.dist-info` directories with no `METADATA` file. The
distribution metadata returned `None` for its name and version, and dependency fingerprinting
passed the missing name to `re.sub`, aborting run creation with `TypeError`.

## Change

Dependency inventories now return an unknown identity when any discovered distribution lacks
a usable name or version. A genuinely empty inventory still has a stable empty-set hash. The
application environment identity no longer replaces an unknown inventory with that empty-set
hash; a run may start, but resume refuses an unverifiable Python environment unless the owner
declares an environment digest.

## Verification

- Orphaned metadata returns an unknown dependency hash, including when valid distributions are
  also present; an empty inventory remains stable.
- The real cache regression and resume-refusal path pass under Python 3.11.16 and 3.12.10:
  four focused tests passed on each interpreter.
- Fresh Windows 11 / Python 3.12.10 CI-style full suite: 1,853 passed, 222 skipped.
- Windows 11 / Python 3.11.16 full suite on the immediately preceding source: 1,850 passed,
  222 skipped; the focused follow-up tests pass on the updated source.
- Windows-target Mypy passed for all 175 source files, Ruff lint passed on changed files, and
  `git diff --check` passed.

The latest hosted matrix available during this follow-up, run
[38357086081](https://github.com/HaisamAbbas/BenchCraft/actions/runs/38357086081), ran before
this code change and failed all six core pytest jobs. Its public annotations omit failing test
names, so hosted validation of this follow-up remains pending.

The read-only defect-first review is in [F02-orphaned-metadata-review.md](F02-orphaned-metadata-review.md).
