"""Budget ledger: reserve before dispatch, reconcile after (§15 "Budget control", 06-T2).

Tracked separately per role: application, evaluator and planner (a manual-plan run makes no
planner calls, so planner stays at zero — measured, not assumed).

- Hard limits (`max_application_calls`, `max_evaluator_calls`, `max_wall_seconds`): a dispatch
  is refused before it would exceed them. Every dispatched call counts, failed or not.
- `max_judge_tokens` (hard) can only be enforced for evaluators that report tokens. It is
  checked before each evaluation against tokens already spent, so in-flight evaluations can
  overshoot it by at most what they use; evaluations with unknown token use are counted and
  reported as `unenforced`, never silently treated as zero.
- `max_cost_usd` is soft: the projection is known cost plus the plan's per-call estimates
  for calls whose cost is unknown or still in flight. Providers give no enforceable cost
  bound, so the summary labels it an estimate. An unknown cost is never counted as zero:
  plan compilation requires the estimates the projection needs, and any unknown-cost call
  without an estimate is reported as `unenforced`. A model-free evaluator with complete
  accounting costs a measured zero.
- Resume replays every committed attempt (`record_prior_*`), so hard limits, tokens and
  known costs carry across sessions; calls that were dispatched but never committed
  (a crash mid-call) are counted as spent with unknown cost.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aibench.core.plans import BudgetLimits


@dataclass
class _Role:
    calls: int = 0
    in_flight: int = 0
    known_cost: float = 0.0
    unknown_cost_calls: int = 0
    tokens: int = 0
    unknown_token_calls: int = 0


@dataclass
class BudgetLedger:
    limits: BudgetLimits
    started: float = field(default_factory=time.monotonic)
    elapsed_before: float = 0.0  # wall time already spent in earlier sessions of this run
    application: _Role = field(default_factory=_Role)
    evaluator: _Role = field(default_factory=_Role)
    planner: _Role = field(default_factory=_Role)

    # ------------------------------------------------------------------ reservations

    def elapsed(self) -> float:
        return self.elapsed_before + (time.monotonic() - self.started)

    def _common_denial(self) -> str | None:
        if (
            self.limits.max_wall_seconds is not None
            and self.elapsed() >= self.limits.max_wall_seconds
        ):
            return f"max_wall_seconds={self.limits.max_wall_seconds} reached"
        return None

    def projected_cost(self, *, extra_application: int = 0, extra_evaluation: int = 0) -> float:
        est_app = self.limits.estimated_cost_per_application_call_usd or 0.0
        est_eval = self.limits.estimated_cost_per_evaluation_usd or 0.0
        return (
            self.application.known_cost
            + self.evaluator.known_cost
            + est_app
            * (self.application.unknown_cost_calls + self.application.in_flight + extra_application)
            + est_eval
            * (self.evaluator.unknown_cost_calls + self.evaluator.in_flight + extra_evaluation)
        )

    def reserve_application(self) -> str | None:
        """Reserve one application call; returns the denial reason, or None if reserved."""
        denial = self._common_denial()
        cap = self.limits.max_application_calls
        if (
            denial is None
            and cap is not None
            and self.application.calls + self.application.in_flight >= cap
        ):
            denial = f"max_application_calls={cap} reached"
        soft = self.limits.max_cost_usd
        if denial is None and soft is not None and self.projected_cost(extra_application=1) > soft:
            denial = f"max_cost_usd={soft} (soft estimate) reached"
        if denial is None:
            self.application.in_flight += 1
        return denial

    def reserve_evaluation(self) -> str | None:
        denial = self._common_denial()
        cap = self.limits.max_evaluator_calls
        if (
            denial is None
            and cap is not None
            and self.evaluator.calls + self.evaluator.in_flight >= cap
        ):
            denial = f"max_evaluator_calls={cap} reached"
        tokens = self.limits.max_judge_tokens
        if denial is None and tokens is not None and self.evaluator.tokens >= tokens:
            denial = f"max_judge_tokens={tokens} reached"
        soft = self.limits.max_cost_usd
        if denial is None and soft is not None and self.projected_cost(extra_evaluation=1) > soft:
            denial = f"max_cost_usd={soft} (soft estimate) reached"
        if denial is None:
            self.evaluator.in_flight += 1
        return denial

    # ------------------------------------------------------------------ reconciliation

    def settle_application(self, *, dispatched: bool, cost: float | None) -> None:
        self.application.in_flight -= 1
        if not dispatched:
            return  # nothing reached the application: nothing spent
        self.application.calls += 1
        self._add_cost(self.application, cost)

    def settle_evaluation(self, resources: dict[str, Any] | None) -> None:
        """`resources` is the result's resources, or None if no evaluator call was made."""
        self.evaluator.in_flight -= 1
        self._account_evaluation(resources)

    # Resume: replay committed spend from earlier sessions of the same run.

    def record_prior_application(self, cost: Any) -> None:
        self.application.calls += 1
        self._add_cost(self.application, cost)

    def record_prior_evaluation(self, resources: dict[str, Any]) -> None:
        self._account_evaluation(resources)

    def record_prior_elapsed(self, seconds: float) -> None:
        self.elapsed_before += seconds

    def _account_evaluation(self, resources: dict[str, Any] | None) -> None:
        if resources is None or resources.get("latency_ms") is None:
            return  # skipped / not applicable before the evaluator ran
        self.evaluator.calls += 1
        if resources.get("accounting") == "complete" and resources.get("model_calls") == 0:
            # A model-free evaluator: zero tokens and zero cost are measured, not assumed.
            self._add_cost(self.evaluator, resources.get("cost") or 0.0)
            return
        self._add_cost(self.evaluator, resources.get("cost"))
        tokens = resources.get("tokens")
        if isinstance(tokens, Mapping) and tokens:
            self.evaluator.tokens += sum(v for v in tokens.values() if isinstance(v, int))
        else:
            self.evaluator.unknown_token_calls += 1

    @staticmethod
    def _add_cost(role: _Role, cost: Any) -> None:
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            role.known_cost += float(cost)
        else:
            role.unknown_cost_calls += 1

    # ------------------------------------------------------------------ reporting

    def summary(self) -> dict[str, Any]:
        def role(r: _Role) -> dict[str, Any]:
            return {
                "calls": r.calls,
                "known_cost_usd": round(r.known_cost, 6),
                "calls_with_unknown_cost": r.unknown_cost_calls,
                "reported_tokens": r.tokens,
                "calls_with_unknown_tokens": r.unknown_token_calls,
            }

        limits = self.limits
        return {
            "application": role(self.application),
            "evaluator": role(self.evaluator),
            "planner": role(self.planner),
            "elapsed_seconds": round(self.elapsed(), 3),
            "limits": {
                "hard": {
                    "max_application_calls": limits.max_application_calls,
                    "max_evaluator_calls": limits.max_evaluator_calls,
                    "max_judge_tokens": limits.max_judge_tokens,
                    "max_wall_seconds": limits.max_wall_seconds,
                },
                "soft": {"max_cost_usd": limits.max_cost_usd},
            },
            "projected_cost_usd_estimate": round(self.projected_cost(), 6),
            "unenforced": self._unenforced(),
        }

    def _unenforced(self) -> list[str]:
        limits, notes = self.limits, []
        if limits.max_judge_tokens is not None and self.evaluator.unknown_token_calls:
            notes.append("max_judge_tokens: some evaluations did not report tokens")
        if limits.max_cost_usd is not None:
            for name, role, estimate in (
                ("application", self.application, limits.estimated_cost_per_application_call_usd),
                ("evaluator", self.evaluator, limits.estimated_cost_per_evaluation_usd),
            ):
                if role.unknown_cost_calls and estimate is None:
                    notes.append(
                        f"max_cost_usd: {role.unknown_cost_calls} {name} call(s) had unknown "
                        "cost and no estimate, so they are not in the projection"
                    )
        return notes
