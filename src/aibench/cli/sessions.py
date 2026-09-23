"""`aibench sessions list` / `show SESSION_ID` / `delete SESSION_ID` (§13, §14).

Views of persisted benchmark sessions: the conversation, each decision with the revision
it produced, open questions, action requests and the runs they started. `delete` removes a
conversation but keeps every run it started (manifests, results, artifacts, events): runs
are benchmark records, not session data (10-T4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from aibench.core.models import deep_unfreeze
from aibench.security.redaction import sanitize_value
from aibench.sessions.drafting import draft_summary
from aibench.sessions.store import SessionStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from aibench.tui.render import safe

app = typer.Typer(help="Inspect persistent benchmark sessions.")
console = Console(emoji=False)
err_console = Console(stderr=True, emoji=False)

_WORKSPACE = typer.Option(
    None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
)
_JSON = typer.Option(False, "--json", help="Machine-readable output.")


def _open(workspace: Path | None) -> Storage:
    ws = Workspace.at(workspace or Path.cwd())
    if not ws.db_path.is_file():
        err_console.print(f"[red]no aibench workspace at {safe(str(ws.root))}[/red]")
        raise typer.Exit(code=2)
    return Storage(Database.open_workspace(ws))


@app.command("list")
def list_sessions(
    workspace: Path | None = _WORKSPACE,
    limit: int = typer.Option(50, "--limit", help="Maximum sessions to show."),
    json_output: bool = _JSON,
) -> None:
    """Sessions in this workspace, most recently updated first."""
    storage = _open(workspace)
    try:
        sessions = SessionStore(storage).list_sessions(limit)
    finally:
        storage.db.close()
    rows = [
        {
            "session_id": s.session_id,
            "revision": s.revision,
            "active_run_id": s.active_run_id,
            "project_root": s.project_root,
            "updated_at": s.updated_at.isoformat(),
        }
        for s in sessions
    ]
    if json_output:
        console.print_json(data=sanitize_value(rows))
        return
    if not rows:
        console.print("[dim]No sessions in this workspace.[/dim]")
        return
    for row in rows:
        run = f"  run={row['active_run_id']}" if row["active_run_id"] else ""
        console.print(
            f"[bold]{safe(row['session_id'])}[/bold]  revision={row['revision']}"
            f"{safe(run)}  "
            f"updated_at={row['updated_at']}"
        )


def _details(store: SessionStore, session_id: str) -> dict[str, Any] | None:
    session = store.get_session(session_id)
    if session is None:
        return None
    decisions = store.list_decisions(session_id)
    current = next(d for d in decisions if d.decision_id == session.decision_id)
    return {
        "session_id": session.session_id,
        "project_root": session.project_root,
        "revision": session.revision,
        "active_run_id": session.active_run_id,
        "draft": draft_summary(deep_unfreeze(current.draft)),
        "decisions": [
            {
                "decision_id": d.decision_id,
                "revision": d.revision,
                "source": d.source,
                "source_turn_id": d.source_turn_id,
                "supersedes": d.supersedes,
                "changes": deep_unfreeze(d.structured_change),
                "plan_file": d.plan_file,
                "plan_hash": d.plan_hash,
                "executable": d.executable,
            }
            for d in decisions
        ],
        "questions": [q.model_dump(mode="json") for q in store.questions(session_id)],
        "actions": [
            a.model_dump(mode="json", exclude={"findings"}) for a in store.list_actions(session_id)
        ],
        "turns": [
            {
                "sequence": t.sequence,
                "turn_id": t.turn_id,
                "role": t.role,
                "kind": t.kind,
                "content": t.content,
                "decision_refs": list(t.decision_refs),
                "action_refs": list(t.action_refs),
                "status_line": (deep_unfreeze(t.outcome) or {}).get("status_line"),
            }
            for t in store.turns(session_id)
        ],
    }


@app.command("show")
def show_session(
    session_id: str = typer.Argument(..., help="Session to show."),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """A session's conversation, decisions, questions and actions."""
    storage = _open(workspace)
    try:
        data = _details(SessionStore(storage), session_id)
    finally:
        storage.db.close()
    if data is None:
        err_console.print(f"[red]no session {safe(session_id)!r}[/red]")
        raise typer.Exit(code=2)
    if json_output:
        console.print_json(data=sanitize_value(data))
        return
    draft = data["draft"]
    state = "executable" if draft["executable"] else "not executable yet"
    console.print(f"[bold]{safe(data['session_id'])}[/bold]  revision {data['revision']} ({state})")
    if data["active_run_id"]:
        console.print(f"  current run: {safe(data['active_run_id'])}")
    for turn in data["turns"]:
        number = safe(f"[{turn['sequence']}]")
        console.print(f"  {number} {turn['role']}: {safe(turn['content'])}")
        if turn["status_line"]:
            console.print(f"      [dim]{safe(turn['status_line'])}[/dim]")
    for decision in data["decisions"]:
        console.print(
            f"  revision {decision['revision']} ({decision['source']}): "
            f"{safe(str(decision['changes'] or 'initial draft'))}"
        )
    for question in data["questions"]:
        if question["status"] == "open":
            console.print(f"  [yellow]open question:[/yellow] {safe(question['prompt'])}")
    for action in data["actions"]:
        run = f" -> {action['run_id']}" if action["run_id"] else ""
        reason = f" ({safe(action['reason'])})" if action["reason"] else ""
        console.print(f"  action {action['kind']}: {action['state']}{run}{reason}")


@app.command("delete")
def delete_session(
    session_id: str = typer.Argument(..., help="Session to delete."),
    workspace: Path | None = _WORKSPACE,
    yes: bool = typer.Option(False, "--yes", help="Confirm: delete the conversation."),
    json_output: bool = _JSON,
) -> None:
    """Delete a conversation. Its runs and their results are kept."""
    from aibench.sessions.controller import SessionController, SessionError
    from aibench.storage.artifacts import ArtifactStore

    storage = _open(workspace)
    ws = Workspace.at(workspace or Path.cwd())
    try:
        if SessionStore(storage).get_session(session_id) is None:
            err_console.print(f"[red]no session {safe(session_id)}[/red]")
            raise typer.Exit(code=2)
        controller = SessionController(
            session_id,
            storage=storage,
            artifacts=ArtifactStore(ws.artifacts_dir),
            workspace_root=ws.root,
        )
        runs = controller.session_runs()
        if not yes:
            err_console.print(
                f"This deletes the conversation {safe(session_id)} (turns, decisions, "
                f"questions, actions). Its {len(runs)} run(s) are kept. Re-run with --yes."
            )
            raise typer.Exit(code=2)
        try:
            result = controller.delete()
        except SessionError as exc:
            err_console.print(f"[red]{safe(str(exc))}[/red]")
            raise typer.Exit(code=2) from exc
    finally:
        storage.db.close()
    if json_output:
        console.print_json(data=sanitize_value(result))
        return
    console.print(
        f"deleted session {safe(session_id)}; kept {len(result['runs_kept'])} run(s): "
        + safe(", ".join(result["runs_kept"]) or "none")
    )
