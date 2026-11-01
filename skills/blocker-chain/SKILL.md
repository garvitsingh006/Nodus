---
name: blocker-chain
description: Trace which engineering issues are blocking a given issue or work item, across teams, distinguishing unresolved, resolved and historical blockers with cited evidence. Use when asked what is blocking an issue, which team is holding up work, or whether a blocker chain is clear.
compatibility: NODUS deterministic tool layer (Stage 12, ND-12B); no LLM required to execute the underlying tools.
---
# Blocker chain

1. Resolve the target issue with `graph.entity_lookup`; if NOT_FOUND, stop
   and report it as unknown rather than guessing at its blockers.
2. Record the graph revision via `graph.revision` so results can be compared
   against later state (a resolved or retracted blocker changes the answer
   on a later call -- never treat a prior answer as still current).
3. Call `work.blocker_chain` with the issue's entity id. This is the only
   tool that determines the blocker chain; never infer a blocker from an
   issue's title/description text, and never invent one not returned by
   this tool.
4. For each item in `findings`, read `status` before reporting it:
   - `UNRESOLVED` -- an active BLOCKS edge whose blocking issue is not
     tracked as resolved. Report this as a real, current obstacle.
   - `RESOLVED` -- the BLOCKS edge is still structurally present but the
     blocking issue's own tracked status is resolved. Report this as
     cleared, not as a current obstacle, but do not delete it from the
     chain.
   - `HISTORICAL` -- the BLOCKS edge itself is no longer current (retracted
     or superseded). Report it only as historical context.
5. Read `cross_team`: `true`/`false` only when both issues' team ownership
   is evidence-backed; `null`/absent means team ownership could not be
   established for one or both issues -- report this as unknown, never
   guess which team is involved.
6. Always surface `evidence_refs`, `chain_path`, `chain_length`,
   `uncertainty` and `suggested_verification` alongside the status; never
   report the status alone.
7. Never claim a blocker implies a runtime dependency or a deployment
   blocker -- `work.blocker_chain` only ever states that a declared
   engineering BLOCKS relationship exists.
8. If `unresolved_blocker_ids` is empty, report that no unresolved upstream
   blockers were found in the graph for this issue -- not that the issue is
   guaranteed to proceed on schedule.
9. Never request unrestricted Cypher or second-guess `work.blocker_chain`'s
   traversal with a different tool; its `max_depth`/`limit` and `truncated`
   flag are the authoritative statement of how much of the graph was
   actually searched.

Never modify graph state. This skill only reads.

## Output

- The issue's entity id and graph revision.
- Direct blocker ids.
- Each finding: blocking issue, blocked issue, chain path, chain length,
  status (unresolved/resolved/historical), cross-team flag, evidence
  references, explanation, uncertainty, and suggested verification action.
- Unresolved blocker ids, summarized.
- Truncation, stated explicitly when `truncated` is true.
