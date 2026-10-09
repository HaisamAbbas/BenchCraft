# Independent review — F07

Skill: `C:/Users/haisam.abbas/.codex/skills/.system/review-agent/SKILL.md`.
Reviewer: `/root/review_f07`; read-only review of the complete F07 change.

The first review found that the broad report branch also changed remote-job result
denominators on old runs without a work graph. The fix limits the frozen denominator to
actual rescoring passes or engine runs with work items. Legacy/remote passes use their own
stored results. The new remote-style regression test covers an uploaded result set smaller
than the run's final execution set.

The reviewer rechecked the change and reported: **No findings.**
