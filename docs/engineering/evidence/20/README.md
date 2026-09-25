# Prompt 20 evidence: local capacity measurement

All files were produced on 2026-09-25 on the machine described in ADR 0019. That machine
was a Windows 11 laptop with 14 logical CPUs, 31.5 GB RAM and an NVMe SSD, and was about
86% busy with other sessions' work throughout.

| File | Command (from the repository root) |
|---|---|
| `capacity.json` | `.venv/Scripts/python scripts/measure_capacity.py --cases 300 --repeat 2 --scratch <temp> --out docs/engineering/evidence/20/capacity.json` (default scenario set) |
| `capacity-3000.json` | `.venv/Scripts/python scripts/measure_capacity.py --cases 3000 --scenario 0:16 --scratch <temp> --out docs/engineering/evidence/20/capacity-3000.json` |
| `profile-300-c64.txt` | `python -m cProfile -o prof64.out -m aibench run --plan plan.json --policy policy.json --workspace . --json`, run in a fresh 300-case, concurrency-64, no-delay project, with the fixture server in a separate process. Summarised to text; the binary profile was not kept |

**How the files were produced.**
- **Script version.** Both JSON files were produced before the script began recording
  `argv` and `peak_memory_kind`. On Windows, `peak_memory_mb` is the peak private commit,
  in MiB, of any process in the job.
- **Application.** Only the local mock application (`examples/apps/rate_limited_app.py`)
  was measured. No real application, provider or distributed deployment was measured.
