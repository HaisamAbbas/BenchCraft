# Local dashboard

`aibench dashboard` opens a browser view of runs and sessions already recorded in the
current `.aibench/` workspace. Use `--workspace PROJECT` to select a project root,
`--port PORT` to change the local port, `--port 0` to let the OS choose one, or `--no-open`
to print the address without launching a browser.

The dashboard lists the latest 50 runs and sessions. It can show stored report facts for a
run and compare two stored runs. Reports use the shared report service with content withheld:
application answers, references, contexts, conversation turns and raw artifacts are not
returned. Comparisons use the shared comparison service and run only after the user selects
two runs. They never execute an application or evaluator. Refresh the page to read new
workspace state.

The dashboard is read-only. Its API accepts `GET` requests for run/session lists, stored run
report facts and stored-run comparisons; write methods return `405`. It opens a separate
SQLite query-only connection for each request and does not start a run, score a run, change a
session or write workspace data. Responses are not cached by the browser. At most four
requests are processed concurrently; incomplete requests time out, and additional requests
receive `503` while all slots are in use.

The local JSON endpoints are `GET /api/runs?limit=50&offset=0` (maximum page size 100),
`GET /api/sessions?limit=50` (maximum 100), `GET /api/runs/{run_id}/report`, and
`GET /api/compare?baseline={run_id}&current={run_id}`. Report generation and comparisons
are limited to 5,000 source rows per run across work items, attempts, results, run events,
trace observations and dataset cases; larger runs receive `413`. This includes legacy runs
without a work-item graph. The count uses indexed storage queries before report work starts.
These requests still use the existing CLI services and are not streamed or incrementally
computed. Each indexed table scan stops after 5,001 rows, so the guard itself does not count
an oversized table to completion.

The server binds only to `127.0.0.1`, checks the local `Host` and browser `Origin`, and sends
no CORS permission. It has no user authentication. Remote access and reverse-proxy exposure
are unsupported; do not expose this command on a network. A future remote mode requires an
authenticated deployment design and its own authorization review. The dashboard adds no
hosting or browser-library dependency, so the CLI remains usable without starting it.
