# F02 follow-up review — Incomplete installed-distribution metadata

The requested read-only review inspected the complete follow-up diff, the dependency-hash call
sites in the registry and run identity paths, the application resume guard, and the added
regressions. No `AGENTS.md` files apply in this repository.

The review checked that malformed distribution metadata cannot become a `TypeError` or a false
empty-environment identity; that genuinely empty inventories remain deterministic; and that an
unknown application dependency inventory is refused on resume. The Python 3.11.16 and 3.12.10
focused regressions, Ruff lint, and Windows-target Mypy passed.

**No findings.** The hosted core matrix has not yet run on this follow-up commit; the previous
hosted failures remain unexplained because the available annotations contain no test identifiers.
