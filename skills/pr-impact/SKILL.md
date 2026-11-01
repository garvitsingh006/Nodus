---
name: pr-impact
description: Determine which services, interfaces and downstream consumers could be affected by a real NEXUS pull request, with cited evidence and explicit uncertainty. Use when asked what a PR or proposed code change could affect, or which services/tests deserve attention before deployment.
compatibility: NODUS deterministic tool layer (Stage 12, ND-12A); no LLM required to execute the underlying tools.
---
# PR change impact

1. Resolve the pull request with `graph.entity_lookup`; if NOT_FOUND, stop
   and report the PR as unknown rather than guessing at its scope.
2. Record the graph revision via `graph.revision` so results can be compared
   against later state (a revised or retracted dependency changes the
   answer on a later call -- this skill never caches a prior answer as
   current).
3. Call `pr.change_impact` with the PR's entity id. This is the only tool
   that determines change scope and propagates it; never estimate affected
   services or consumers from the PR title/description text alone.
4. Read the result's `changed_resources`: each is labeled `declared` (the
   PR's own claimed affected service/contract) or `modifies_repository`
   (derived via the PR's modified repository's ownership). Report this
   provenance distinction -- a declared claim and a derived one are not the
   same strength of evidence.
5. Read `unmapped_changed_files` and report them explicitly as files whose
   service attribution is unverified -- never silently omit them or guess
   which service they belong to.
6. For each item in `findings`:
   - `classification` is `DIRECT_DEPENDENCY_POTENTIALLY_AFFECTED` or
     `TRANSITIVE_DEPENDENCY_POTENTIALLY_AFFECTED` -- report which, and never
     upgrade a transitive finding to direct.
   - A finding means a dependency path exists and establishes potential
     exposure. It is never proof of an actual incompatibility, a severity
     score, or a failure probability -- do not invent one.
   - Always surface `evidence_refs`, `relationship_path`, `uncertainty` and
     `suggested_verification` alongside the classification; never report
     the classification alone.
7. If `findings` is empty but `changed_resources` is non-empty, report that
   no downstream dependents were found in the graph for this PR's changed
   services -- not that the PR is safe to ship.
8. If `changed_resources` is also empty, report insufficient source
   coverage for this PR's change scope -- never fall back to inventing an
   impact from the PR's text.
9. Never request unrestricted Cypher or invoke a different graph tool to
   second-guess `pr.change_impact`'s traversal; its bounds (`max_depth`,
   `limit`) and `truncated` flag are the authoritative statement of how much
   of the graph was actually searched.

Never modify graph state. This skill only reads.

## Output

- The PR's entity id and graph revision.
- Changed resources, each labeled declared vs. repository-derived.
- Unmapped changed files, named explicitly.
- Each impact finding: changed resource, affected resource, relationship
  path, edge types, evidence references, classification, explanation,
  uncertainty, and suggested verification action.
- Truncation, stated explicitly when `truncated` is true.
