"""Repository grounding — the deterministic core of HonestCode.

Everything an AI agent writes is a claim about the repository it is working in.
This package checks those claims against the repository itself:

* :mod:`honestcode.verify.surface`   reconstructs a class's member surface
* :mod:`honestcode.verify.verify`    walks call sites and resolves receivers
* :mod:`honestcode.verify.evidence`  emits the agent-facing protocol

No LLM calls, no network, no heuristics that cannot be explained.
"""

from honestcode.verify.evidence import (
    Finding,
    build_report,
    render_text,
)

__all__ = ["Finding", "build_report", "render_text"]
