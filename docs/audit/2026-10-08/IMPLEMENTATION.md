# Sequential audit implementation

The user authorized implementing every finding and recommended capability in
[REPORT.md](REPORT.md), [findings.csv](findings.csv), and
[feature-backlog.csv](feature-backlog.csv), with a separate verified commit and push per
improvement. The audit and its original evidence are historical; current implementation
state lives in [implementation-ledger.json](implementation-ledger.json).

Author: `Haisam Abbas <HaisamAbbas@outlook.com>`.
Delivery branch: `fix/plugin-core-match`, tracking `origin/fix/plugin-core-match`.

Each item follows the same sequence:

1. Implement its complete behavior and acceptance boundary.
2. Run meaningful affected tests and relevant lint/type checks.
3. Obtain an independent read-only review using the requested review-agent skill.
4. Resolve actionable review findings and verify the resulting change.
5. Commit, push without force, and verify the remote commit before beginning the next item.

G22.1-G22.4 cover batched work persistence, bounded queue paging, coordinator-backed
distributed workers, and open-loop/ramping load profiles. G22.5 adds image-first vision
scoring with bounded inline image input and paired reviewed-case calibration across judge
bindings; G22.6 adds a local read-only dashboard over shared stored-result services. Approved
live cross-provider calibration, cross-host performance evidence, and broader modality support
remain explicit limitations. No existing deferred label is treated as authorization to omit a
feature selected by the user.

G22.8 extends `deepeval.vision_g_eval` to score a recorded generated image beside the case
image, task, and reviewed answer. Both images use the strict inline PNG/JPEG/WebP contract;
remote paths and URLs remain rejected.

G22.9 adds `deepeval.audio_g_eval` for text or generated-audio output against one bounded
inline WAV/MP3 case. It validates supported WAV/MP3 framing before the isolated worker sends
labeled `input_audio` parts to the explicitly configured judge. Built-in dataset rows are
hard-capped at 1,000,000 bytes, so imported case audio has a lower practical limit than the
per-audio evaluator cap; HTTP transport limits can be raised in application configuration.
The G22.9 single-request audio task evaluator does not score turn timing or diarization.

G22.10 adds `deepeval.video_frames_g_eval` for text or supplied sampled-frame output. Case input
uses 2 to 8 strictly ordered timestamps and inline PNG/JPEG/WebP images, capped at 512 KiB per
sequence to fit within the built-in 1,000,000-byte dataset row limit alongside task and
reference text. Generated output allows 1 MiB per frame and 4 MiB per sequence. Frame order
and timestamp labels are retained in the judge request; the configured HTTP response limit
can be raised to carry maximum generated output. This scores only supplied samples; it does not
decode raw video containers or score motion between samples.

G22.11 adds seven local DeepEval voice metrics over stored, timestamped conversation turns. Inputs are inline bounded PCM WAV; the host rejects episode evidence over 50 turns or 7 MiB before worker dispatch. Responsiveness only reflects missing assistant audio in this stored format; the adapter does not capture live calls, measure live latency, detect reprompts, or score diarization. G22.12 adds DeepEval's five upstream multimodal image metrics (`image_coherence`, `image_helpfulness`, `image_reference`, `text_to_image`, and `image_editing`) over bounded inline PNG/JPEG/WebP evidence and an explicitly configured vision judge. Image evidence rejects URLs and paths, caps combined source/output data at 4 MiB, and records partial request/token accounting across retries and worker boundaries. G22.13 adds DeepEval's pinned MCP-Use, MCP Task Completion, and multi-turn MCP-Use metrics over a frozen inventory and recorded successful tool events. Typed MCP event provenance is required by default; untyped events need an explicit MCP-only opt-in, and missing or malformed episode telemetry is not applicable. The adapter does not contact live servers or score resource/prompt execution because stored events do not distinguish them. G22.14 adds `deepeval.transcription_accuracy_g_eval` over caller transcript text and recorded `provider_transcript` values using the pinned DeepEval 4.2.5 G-Eval API. All episode turns need a provider transcript; input is capped at 50 turns, 16,000 characters per transcript, and 1 MiB total. Audio is not submitted, and word error rate, acoustic diarization, and barge-in joining are not implemented. The dedicated upstream beta metric is absent from the pinned package; upstream multimodal audio metrics and diarization remain unsupported. See [G22.11 implementation](implementation/G22.11-voice-metrics.md), [G22.12 implementation](implementation/G22.12-upstream-image-metrics.md), [G22.13 implementation](implementation/G22.13-upstream-mcp-metrics.md), and [G22.14 implementation](implementation/G22.14-transcription-accuracy.md).

G23 adds source-turn planner usage attribution and combined workflow cost accounting. Reports
retain separate application, primary evaluator-pass, and planner views while the workflow total
includes every evaluator scoring pass. Planner dollar pricing and total-cost budget enforcement
remain unsupported; partial, canceled, concurrent duplicate, and legacy usage states stay
visible rather than being counted as complete.

The implementation order follows the report's remediation sequence. F14 is a small
prerequisite to obtain a clean type-check baseline; F03 and F05 then protect input
isolation and fidelity before scoring, budgets, identities, and CLI features are extended.

An item marked `verified` is ready for delivery. `delivered` requires a recorded commit and
successful push; Git and the remote ref remain authoritative. Reviews and checks are saved
under `evidence/`, and per-item implementation notes under `implementation/`.
