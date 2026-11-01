---
name: investigate-conflict
description: Investigate contradictory graph claims about an entity using the deterministic authority engine, returning the conflicting claims, their evidence and a resolution status that is never guessed. Use when two sources disagree about the same entity field.
compatibility: NODUS deterministic tool layer (Stage 04); no LLM required to execute the underlying tools.
---
# Investigate conflict

1. Identify the disputed entity with `graph.entity_lookup`; its `conflicts`
   field (per-field list of disagreeing values) names what is actually in
   dispute. An entity with an empty `conflicts` dict is not in dispute.
2. Call `evidence.conflicts` for the full per-field conflict values and the
   entity's `authority` verdicts.
3. For each disputed field, call `evidence.lookup` (and, if deeper source
   detail is needed, `evidence.verify`) to retrieve the assertions backing
   each disagreeing value, including each assertion's `current` flag and
   `observed_at` — a retracted assertion is still part of the historical
   record but is not live support for its value.
4. Read the field's authority verdict `status`:
   - `authoritative` -> a configured rule names a winning source
     (`winning_source_id`, `rule_id`); report it as the resolution.
   - `unresolved` -> multiple rules tied at the same precedence; report the
     tie explicitly, do not pick one.
   - `no_rule` -> no authority rule applies to this scope/entity type;
     report the conflict as unresolved, not as resolved-by-omission.
5. Never resolve a conflict yourself: do not select the newest assertion,
   the most frequent value, or any value not named by an `authoritative`
   verdict. If `status` is not `authoritative`, the conflict is unresolved
   — say so plainly.
6. Never delete, hide or relabel a contradictory claim — `conflicts` is the
   complete record and must be reported in full alongside the verdict.
7. Never elevate an `inferred` relationship-origin claim into a
   source-asserted fact while reporting a resolution.
8. The deterministic authority engine's verdict is final for this skill —
   never override it with a different ranking.

Never modify graph state. This skill only reads.

## Output

- Task objective and the disputed entity/field(s).
- Graph revision.
- All conflicting values, verbatim from `conflicts`.
- Evidence references per conflicting value.
- Authority/resolution status: authoritative (with winning source and
  rule) / unresolved / no_rule — reported exactly as returned.
- Limitations (e.g. missing READ_EVIDENCE) and unresolved questions.
