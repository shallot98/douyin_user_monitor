# Authorized browser playback repair

This is the execution contract for the existing plan directory, not a registered Trellis task. Trellis remains disabled. The user authorized execution of [the repair plan](repair-plan.md) on 2026-09-13.

## Requirements

- Preserve original media bytes, original archive references, and download history.
- Use D2 as the main source for repaired playback files.
- Never download or transcode historical videos on the project host, or send full media through its upload cache.
- Restore explicitly verified historical cloud paths, including filenames and meaningful whitespace; do not guess replacements.
- Reuse valid H.264 playback files. Prepare additional H.264/AAC/faststart files outside the project host only when needed.
- Keep unavailable, encrypted, empty, audio-only, and incomplete files explicit until a valid source is verified.
- Preserve concurrent incremental monitoring updates with targeted, locked state writes.

## Acceptance

- Known historical D2 objects resolve with all D2 candidates ahead of backup candidates.
- Regression tests cover exact aliases, renamed filenames, whitespace, source changes, and explicit missing-object errors.
- Browser checks verify real video frames, audio availability, seeking, and duration for representative repaired files.
- Record actual results and unresolved failures separately; task submission and object presence are not playback acceptance.
- Run backend unit tests with a 60-second timeout. Avoid release builds and unrelated refactors.

## Evidence

Use [the inventory](../../../../runtime/web-playback-inventory-20260913/report.md) and [verified historical paths](../../../../runtime/web-playback-inventory-20260913/path-recovery-evidence.json). Production installation and final acceptance are coordinated by the main agent.
