"""`aibench sessions list` / `aibench sessions show SESSION_ID` (§13, 08-T1).

Read-only views of persisted benchmark sessions: the conversation, each decision with the
revision it produced, open questions, action requests and the runs they started. The
interactive terminal that continues a session is Prompt 09.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape

from aibench.core.models import deep_unfreeze
from aibench.sessions.drafting import draft_summary
from aibench.sessions.store import SessionStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

app = typer.Typer(help="Inspect persistent benchmark sessions.")
console = Console()
err_console = Console(stderr=True)

_WORKSPACE = typer.Option(
    None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
)
_JSON = typer.Option(False, "--json", help="Machine-readable output.")


def _open(workspace: Path | None) -> Storage:
    ws = Workspace.at(workspace or Path.cwd())
    if not ws.db_path.is_file():
        err_console.print(f"[red]no aibench workspace at {escape(str(ws.root))}[/red]")
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
        console.print_json(data=rows)
        return
    if not rows:
        console.print("[dim]No sessions in this workspace.[/dim]")
        return
    for row in rows:
        run = f"  run={row['active_run_id']}" if row["active_run_id"] else ""
        console.print(
            f"[bold]{row['session_id']}[/bold]  revision={row['revision']}{run}  "
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
        err_console.print(f"[red]no session {escape(session_id)!r}[/red]")
        raise typer.Exit(code=2)
    if json_output:
        console.print_json(data=data)
        return
    draft = data["draft"]
    state = "executable" if draft["executable"] else "not executable yet"
    console.print(
        f"[bold]{escape(data['session_id'])}[/bold]  revision {data['revision']} ({state})"
    )
    if data["active_run_id"]:
        console.print(f"  current run: {escape(data['active_run_id'])}")
    for turn in data["turns"]:
        number = escape(f"[{turn['sequence']}]")
        console.print(f"  {number} {turn['role']}: {escape(turn['content'])}")
        if turn["status_line"]:
            console.print(f"      [dim]{escape(turn['status_line'])}[/dim]")
    for decision in data["decisions"]:
        console.print(
            f"  revision {decision['revision']} ({decision['source']}): "
            f"{escape(str(decision['changes'] or 'initial draft'))}"
        )
    for question in data["questions"]:
        if question["status"] == "open":
            console.print(f"  [yellow]open question:[/yellow] {escape(question['prompt'])}")
    for action in data["actions"]:
        run = f" -> {action['run_id']}" if action["run_id"] else ""
        reason = f" ({escape(action['reason'])})" if action["reason"] else ""
        console.print(f"  action {action['kind']}: {action['state']}{run}{reason}")
