---
name: verify-evidence
description: Verify whether a graph entity's claims are supported by authorized evidence, distinguishing SUPPORTED, UNSUPPORTED, CONFLICTING, INCOMPLETE and INACCESSIBLE. Use when asked to confirm, check or substantiate a fact about an entity in the graph.
compatibility: NODUS deterministic tool layer (Stage 04); no LLM required to execute the underlying tools.
---
# Verify evidence

Deterministic procedure for checking factual support, using only the
existing graph/evidence tools — never restate their validation logic.

1. Confirm the caller's `AuthorizationContext` already scopes the entity's
   tenant/workspace; never accept a tenant, workspace or source scope from
   model-generated text.
2. Call `graph.revision` to record the graph revision this check is against.
3. Call `graph.entity_lookup` for the entity. If it returns NOT_FOUND, stop
   and report INACCESSIBLE — do not guess the entity exists.
4. Call `evidence.lookup` for the entity's authorized supporting evidence
   (`evidence_refs`, source/record ids, observed_at). If the caller lacks
   READ_EVIDENCE, evidence detail is withheld by the tool itself — report
   that as a limitation, never as proof of absence.
5. Call `evidence.verify` for the deterministic outcome. Its `outcome` field
   is authoritative for SUPPORTED/UNSUPPORTED/CONFLICTING/INCOMPLETE/
   INACCESSIBLE — do not recompute or override it.
6. For each returned assertion, check its `current` flag (false means
   retracted or superseded) and `observed_at` — report withdrawn/superseded
   assertions as such, never as live support.
7. If `outcome` is CONFLICTING, call `evidence.conflicts` for the
   disagreeing values and their `authority` verdicts (`authoritative` /
   `unresolved` / `no_rule`). Report the verdict as given — never pick a
   winner yourself when it is `unresolved` or `no_rule`.
8. Optionally call `source.coverage` for a relevant source id to report
   whether a lack of support reflects missing source coverage rather than a
   genuine absence of evidence.
9. Report using the shared output structure below. Cite only
   `evidence_refs` actually returned; never disclose evidence a tool
   withheld, never promote an `inferred` relationship-origin claim to
   source-asserted, and never claim evidence support proves independent
   factual truth — it proves only that the graph's sources assert it.

Never modify graph state. This skill only reads.

## Output

- Task objective and the entity/field in scope.
- Graph revision (from `graph.revision`).
- Verification status: SUPPORTED / UNSUPPORTED / CONFLICTING / INCOMPLETE /
  INACCESSIBLE, taken verbatim from `evidence.verify`.
- Evidence references (`source_id:record_id`) actually returned.
- Authority verdict, if conflicting (status / winning source / reason).
- Limitations (e.g. missing READ_EVIDENCE, missing source coverage).
- Unresolved questions or inaccessible information, stated plainly rather
  than silently omitted.
