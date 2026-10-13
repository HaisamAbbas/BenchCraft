# Ragas dependency security exceptions

The Ragas environment is audited in CI with `pip-audit`. Only the two unpatched upstream
advisories below are excluded from that job. Any other advisory fails the audit.

## [GHSA-95ww-475f-pr4f](https://github.com/advisories/GHSA-95ww-475f-pr4f) — Ragas multimodal SSRF

The pinned `ragas==0.4.3` release has no patched version listed by the upstream advisory. The
issue is in the multimodal faithfulness URL/local-file helpers. BenchCraft's adapter selects
only the text `ragas.metrics.collections.Faithfulness`, validates that retrieved context is
`list[str]`, and never invokes the multimodal metric or image-processing helper. The real
plugin test replaces that helper with a failure and scores an ordinary text case. The adapter
also rejects non-text context before calling Ragas. This is a reviewed reachability exception;
the installed package remains vulnerable if another caller exposes its multimodal path.

## [GHSA-w8v5-vhqr-4h9v](https://github.com/advisories/GHSA-w8v5-vhqr-4h9v) — DiskCache unsafe pickle

Ragas requires `diskcache`, whose current upstream release has no fix for unsafe pickle
deserialization when an attacker can write to a cache directory that the victim later reads.
BenchCraft does not instantiate Ragas' `DiskCacheBackend` or configure a Ragas cache backend.
The same real plugin test makes `DiskCacheBackend` construction fail and verifies the text
metric still completes. The exception covers only this adapter path; callers that configure
Ragas disk caching must treat the cache directory as trusted and not writable by attackers.

CI ignores these exact GitHub advisory IDs only in the Ragas environment audit. The exception
must be removed when Ragas publishes a fixed release or the adapter no longer needs the
affected dependency. The worker boundary is process isolation, not a general security sandbox.
