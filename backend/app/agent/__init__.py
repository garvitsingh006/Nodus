"""GH-007A: the first general-purpose NODUS investigation agent.

Single-agent only: one Google ADK ``LlmAgent``/``Runner`` pair per
investigation run. No multi-agent coordination, no Temporal workflow, no
handoff contract yet.

This package is a thin integration layer. It does not duplicate:
- model calling, routing, budget or retry logic (``backend.app.gateway``),
- skill trust/versioning/digest enforcement (``backend.app.skills``),
- tool authorization/validation/execution (``backend.app.tools``).

It only adapts those existing, independently-tested subsystems to the
Google ADK ``BaseLlm``/``BaseTool``/``Runner`` abstractions.
"""
from __future__ import annotations
