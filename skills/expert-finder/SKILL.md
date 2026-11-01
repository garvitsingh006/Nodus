---
name: expert-finder
description: Find relevant engineering contributors, reviewers and formal owners for a service, repository or interface, based on actual source-backed work. Use when asked who knows a service, who owns it, or who should review a change to it.
compatibility: NODUS deterministic tool layer (Stage 12, ND-12D); no LLM required to execute the underlying tools.
---
# Expert finder

1. Resolve the target resource with `graph.entity_lookup`; if NOT_FOUND,
   stop and report it as unknown rather than guessing at contributors.
2. Record the graph revision via `graph.revision` so results can be
   compared against later state.
3. Call `people.find_experts` with the target's entity id. This is the only
   tool that identifies candidates; never invent a contributor, reviewer or
   owner not returned by this tool, and never identify someone by name
   similarity alone -- only by canonical employee id.
4. Read `classification` for each candidate before reporting them:
   - `FORMAL_OWNER` -- backed by an authoritative OWNS record. Report this
     as actual, current ownership.
   - `POTENTIAL_SME` -- multiple relevant evidence signals (e.g. both
     authored and reviewed work, or several contributions). Report as a
     suggested expert, not a declared owner.
   - `CONTRIBUTOR` -- a single authored contribution (PR, issue assignment,
     or architecture decision). Report as having touched this resource, not
     as an owner or broad expert.
   - `REVIEWER` -- review evidence only. Report as having reviewed related
     work, nothing more.
   Never equate these categories or upgrade one to another.
5. Always surface `evidence` (role, resource, evidence references) and
   `uncertainty` alongside the classification; never report a classification
   without its supporting evidence.
6. If `insufficient_evidence` is true, report that no defensible candidate
   was found -- never fall back to a guess or a name-based suggestion.
7. Never request unrestricted Cypher or second-guess `people.find_experts`
   with a different tool; its `limit`/`evidence_limit` and `truncated` flag
   are the authoritative statement of how much of the graph was searched.

Never modify graph state. This skill only reads. It never produces
personnel performance judgments, productivity scores or company-wide
rankings -- only resource-specific, evidence-backed suggestions.

## Output

- The target's entity id and graph revision.
- Each candidate: employee id, name, classification, formal-owner flag,
  evidence (role/resource/evidence refs), explanation, and uncertainty.
- Whether evidence was insufficient to suggest anyone.
- Truncation, stated explicitly when `truncated` is true.
