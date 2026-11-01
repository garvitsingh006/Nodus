---
name: decision-genealogy
description: Reconstruct the history, rationale, supporting evidence and supersession of an engineering architecture decision. Use when asked why a decision was made, what alternatives were considered, what it replaced or was replaced by, or what it relates to.
compatibility: NODUS deterministic tool layer (Stage 12, ND-12C); no LLM required to execute the underlying tools.
---
# Decision genealogy

1. Resolve the target decision with `graph.entity_lookup`; if NOT_FOUND,
   stop and report it as unknown rather than guessing at its history.
2. Record the graph revision via `graph.revision` so results can be
   compared against later state (a new supersession or revised evidence
   changes the answer on a later call).
3. Call `decision.genealogy` with the decision's entity id. This is the
   only tool that resolves rationale, alternatives, related resources and
   genealogy; never invent an approval history, motivation or supersession
   relationship not returned by this tool.
4. Read `structured_fields_available`:
   - `true` -- report `rationale`, `alternatives_considered`, `context`,
     `selected_decision`, `expected_consequences`, `decision_status` and
     `decision_date` as this decision's documented record.
   - `false` -- report `uncertainty` verbatim and do not fabricate a
     rationale or alternatives from the decision's title alone.
5. Report `related_resources` as evidence-backed connections only (each
   carries its own `relation_type` and `evidence_refs`); never claim a
   relationship to something not in this list.
6. Read `genealogy` for predecessor/successor links:
   - `relation: PREDECESSOR` -- an earlier decision this one explicitly
     supersedes.
   - `relation: SUCCESSOR` -- a later decision that explicitly supersedes
     this one.
   - `status: CURRENT` vs `HISTORICAL` -- whether the SUPERSEDES
     relationship itself is still active or has been retracted/superseded.
   - If `genealogy` is empty, report this as an accurate single-decision
     history (no documented predecessor or successor) -- never manufacture
     one from chronology or topic similarity.
7. `decision_date` is the historical effective date; it establishes
   ordering for display only, never a causal or supersession claim by
   itself.
8. Never request unrestricted Cypher or second-guess `decision.genealogy`
   with a different tool; its `max_depth`/`limit` and `truncated` flag are
   the authoritative statement of how much of the graph was actually
   searched.

Never modify graph state. This skill only reads.

## Output

- The decision's entity id, title and graph revision.
- Rationale, alternatives, context and status, or the uncertainty
  explaining why they are unavailable.
- Related resources with their relation type and evidence.
- Predecessor/successor genealogy, each labeled current or historical, with
  evidence and chain path.
- Truncation, stated explicitly when `truncated` is true.
