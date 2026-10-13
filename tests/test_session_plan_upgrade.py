"""After upgrading to rc35, reopening a session made by rc33 failed: "plan-17d5f20c1f5cb3ab.json
holds a different plan; refusing to overwrite it". rc35 added `warmup_repetitions` and
`load_profile` to plans; their defaults are left out of the plan's hash, so an unchanged plan
kept its ID and its file name, but the file was written with them, so its text differed from
the file the earlier release wrote. A plan file is now written in the form that is hashed."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tests.session_support import SessionHarness


def test_a_session_plan_file_is_the_form_its_name_hashes(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"}, objectives=("catch wrong answers",))
    try:
        decision = ctl.current_decision()
        written = json.loads((ctl.directory / decision.plan_file).read_text(encoding="utf-8"))
        assert "warmup_repetitions" not in written and "load_profile" not in written
        canonical = json.dumps(written, sort_keys=True, separators=(",", ":")).encode("utf-8")
        assert decision.plan_hash == "sha256:" + hashlib.sha256(canonical).hexdigest()
    finally:
        ctl.storage.db.close()


def test_a_session_drafted_by_an_earlier_release_redrafts_after_the_upgrade(
    tmp_path: Path,
) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"}, objectives=("catch wrong answers",))
    try:
        path = ctl.directory / ctl.current_decision().plan_file
        # As rc33 wrote it: the same plan, without the fields rc35 added.
        plan = json.loads(path.read_text(encoding="utf-8"))
        plan.pop("warmup_repetitions", None)
        plan.pop("load_profile", None)
        path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        result = ctl._redraft({"refreshed": "upgrade"})  # the same plan, drafted again
        assert result is not None and result.status == "applied", result
        assert ctl.current_decision().plan_file == path.name
    finally:
        ctl.storage.db.close()
