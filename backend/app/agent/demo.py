"""Demo mode: a small set of clearly labelled scripted commands. No AI model, no key, no network.

Every command runs the REAL tools through the same MCP client, tool executor and logging as live mode; only the
"reasoning" is a fixed script and the reply is written from a template out of the real tool results. So the output is
deterministic and honest: it never reads like a model's judgement, and it cannot say anything the tools did not return.

Commands (``help`` lists them in the app):
    find <text>                 search the document for text
    detect [categories]         list what the detectors notice (saves nothing)
    propose [categories]        save detected spans as proposals for the reviewer
    policy <question>           show the most similar policy passages, quoted and cited
    summary                     where the review stands
    export                      create the redacted file, if the reviewer has authorized it
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from collections.abc import Sequence
from typing import Any

from app.agent.loop import (
    AgentLimits,
    ExecutedTool,
    LoopResult,
    ToolEventCallback,
    ToolExecutor,
    ToolRunner,
)
from app.agent.provider import ToolCall
from app.mcp_server.schemas import MAX_PROPOSALS_PER_CALL
from app.models.conversations import RunStatus

FOOTER = "\n\n(Demo mode: a scripted reply built from the tool results. No AI model was used.)"

CATEGORY_NAMES = {
    "person": "PERSON", "people": "PERSON", "name": "PERSON", "names": "PERSON",
    "organization": "ORGANIZATION", "organisation": "ORGANIZATION", "org": "ORGANIZATION", "company": "ORGANIZATION", "companies": "ORGANIZATION",
    "location": "LOCATION", "place": "LOCATION",
    "email": "EMAIL", "emails": "EMAIL",
    "phone": "PHONE", "phones": "PHONE",
    "ssn": "SSN",
    "address": "ADDRESS", "addresses": "ADDRESS",
}  # fmt: skip
# What `propose` saves when you name no category. Company names stay visible unless you ask for them.
DEFAULT_PROPOSE = ["PERSON", "EMAIL", "PHONE", "SSN"]
MAX_FIND_LINES = 10
MAX_POLICY_PASSAGES = 3
SNIPPET_CHARS = 110
QUOTE_CHARS = 180

HELP = """Demo mode: these scripted commands run the real tools. No AI model is involved.

  find <text>             search the document, e.g.  find Northwind
  detect [categories]     list what the detectors notice; saves nothing, e.g.  detect email phone
  propose [categories]    save detected spans as proposals for you to review (default: person email phone ssn)
  policy <question>       show the most similar policy passages with quotes, e.g.  policy can company names stay visible?
  summary                 show where the review stands
  export                  create the redacted file (only after you click "Let the assistant export")

Categories: person, organization, location, email, phone, ssn, address.
Nothing is ever approved or exported by this assistant: you decide in the Findings and Export tabs."""


class _Stopped(Exception):
    def __init__(self, kind: str) -> None:
        self.kind = kind  # "cancelled" or "timeout"


def _clip(text: str, limit: int) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _first_sentence(text: str) -> str:
    """An exact slice of the passage (so quote verification passes): its first sentence, or a clipped start."""
    flat = re.sub(r"\s+", " ", text).strip()
    match = re.search(r"[.!?](\s|$)", flat)
    sentence = flat[: match.end()].strip() if match else flat
    return sentence if len(sentence) <= QUOTE_CHARS else sentence[:QUOTE_CHARS].rsplit(" ", 1)[0]


def _parse_categories(rest: str) -> tuple[list[str] | None, str | None]:
    """``(categories, problem)``. No words means "not specified" (None)."""
    words = [w for w in re.split(r"[\s,]+", rest.lower()) if w]
    if not words:
        return None, None
    unknown = [w for w in words if w not in CATEGORY_NAMES]
    if unknown:
        return None, f"I do not know the category '{unknown[0]}'. Use: person, organization, location, email, phone, ssn, address."
    return sorted({CATEGORY_NAMES[w] for w in words}), None


class DemoAssistant:
    def __init__(self, tools: ToolRunner, limits: AgentLimits | None = None) -> None:
        self._limits = limits or AgentLimits()
        self._executor = ToolExecutor(tools, self._limits)

    async def run(
        self,
        *,
        document_id: str,
        history: Sequence[tuple[str, str]],
        user_text: str,
        cancel: asyncio.Event | None = None,
        on_tool_event: ToolEventCallback | None = None,
    ) -> LoopResult:
        result = LoopResult()
        started = time.monotonic()
        command, rest = self._parse(user_text)
        handler = {
            "find": self._find,
            "detect": self._detect,
            "propose": self._propose,
            "policy": self._policy,
            "summary": self._summary,
            "export": self._export,
        }.get(command)

        async def call(name: str, arguments: dict[str, Any]) -> ExecutedTool:
            remaining = self._limits.max_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise _Stopped("timeout")
            outcome, executed = await self._executor.execute(
                ToolCall(id=f"demo_{len(result.tool_events) + 1}", name=name, arguments=arguments),
                document_id,
                result,
                remaining,
                cancel,
                on_tool_event,
            )
            if outcome != "ok" or executed is None:
                raise _Stopped(outcome)
            result.rounds += 1
            return executed

        try:
            if handler is None:
                text = HELP if command == "help" else f"I did not understand “{_clip(user_text, 60)}”.\n\n{HELP}"
            else:
                text = await handler(call, rest)
        except _Stopped as stopped:
            if stopped.kind == "cancelled":
                result.status = RunStatus.CANCELLED
                result.text = "Stopped at your request. Anything already proposed stays in the Findings panel."
                return result
            result.limits_hit.append("max_seconds")
            text = f"I stopped because I reached the time limit of {int(self._limits.max_seconds)} seconds. Anything I proposed is in the Findings panel."
        result.text = text + FOOTER
        return result

    @staticmethod
    def _parse(user_text: str) -> tuple[str, str]:
        parts = user_text.strip().lstrip("/").split(None, 1)
        if not parts:
            return "help", ""
        return parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _tool_problem(name: str, executed: ExecutedTool) -> str | None:
        if executed.result.ok and executed.visible:
            return None
        if executed.result.ok:
            return f"The result of {name} was too large to show. Try something narrower."
        return f"The {name} tool reported a problem ({executed.result.error_code}): {_clip(executed.result.error or '', 200)}"

    # ------------------------------------------------------------------ commands
    async def _find(self, call, rest: str) -> str:
        if not rest:
            return "Tell me what to look for, e.g.  find Northwind"
        executed = await call("search_document", {"query": rest, "max_results": MAX_FIND_LINES})
        if (problem := self._tool_problem("search_document", executed)):
            return problem
        data = executed.result.data or {}
        found, more = data.get("returned", 0), data.get("truncated")
        lines = []
        if found == 0:
            lines.append(f"No matches for “{_clip(rest, 60)}”.")
        else:
            lines.append(f"Found {found}{'+' if more else ''} match(es) for “{_clip(rest, 60)}”:")
            for occurrence in data.get("occurrences", [])[:MAX_FIND_LINES]:
                lines.append(f"  • page {occurrence.get('page_number')}: {_clip(occurrence.get('context', ''), SNIPPET_CHARS)}")
            if more:
                lines.append("  …and more. Narrow the search to see the rest.")
        skipped = data.get("unsupported_pages") or []
        if skipped:
            lines.append(f"Pages {', '.join(map(str, skipped))} are scanned or mixed and were not searched.")
        return "\n".join(lines)

    async def _detect_candidates(self, call, categories: list[str] | None) -> tuple[ExecutedTool, dict[str, Any]]:
        executed = await call("detect_sensitive_entities", {"categories": categories} if categories else {})
        return executed, executed.result.data or {}

    async def _detect(self, call, rest: str) -> str:
        categories, problem = _parse_categories(rest)
        if problem:
            return problem
        executed, data = await self._detect_candidates(call, categories)
        if (problem := self._tool_problem("detect_sensitive_entities", executed)):
            return problem
        counts = Counter(c.get("category") for c in data.get("candidates", []))
        if not counts:
            return "The detectors found nothing" + (f" in {', '.join(categories)}." if categories else ".") + self._notes(data)
        breakdown = ", ".join(f"{name} {n}" for name, n in sorted(counts.items()))
        more = f" (showing {data.get('returned')} of {data.get('total_candidates')})" if data.get("truncated") else ""
        return (
            f"The detectors noticed {data.get('total_candidates', 0)} candidate(s){more}: {breakdown}.\n"
            "Candidates are suggestions only and nothing was saved. Say  propose  to save them as proposals for your review."
            + self._notes(data)
        )

    @staticmethod
    def _notes(data: dict[str, Any]) -> str:
        # The detector always adds a generic "candidates are suggestions" note; every reply here already says what
        # happened to the candidates, so only the specific notes (for example a missing NER model) are repeated.
        notes = [n for n in data.get("notes", []) if isinstance(n, str) and "suggestions only" not in n]
        return ("\n" + "\n".join(f"Note: {n}" for n in notes[:3])) if notes else ""

    async def _propose(self, call, rest: str) -> str:
        categories, problem = _parse_categories(rest)
        if problem:
            return problem
        chosen = categories or DEFAULT_PROPOSE
        executed, data = await self._detect_candidates(call, chosen)
        if (problem := self._tool_problem("detect_sensitive_entities", executed)):
            return problem
        candidates = data.get("candidates", [])
        if not candidates:
            return f"Nothing to propose: the detectors found no {', '.join(chosen)} text." + self._notes(data)

        items = [
            {
                "page_number": c["page_number"],
                "char_start": c["char_start"],
                "char_end": c["char_end"],
                "category": c["category"],
                "expected_text": c["text"],
                "explanation": _clip(f"Detected by {', '.join(c.get('sources', [])) or 'a detector'}; review needed.", 280),
            }
            for c in candidates
        ]
        created = existing = refused = 0
        for start in range(0, len(items), MAX_PROPOSALS_PER_CALL):
            batch = await call("propose_redactions", {"proposals": items[start : start + MAX_PROPOSALS_PER_CALL]})
            if (problem := self._tool_problem("propose_redactions", batch)):
                return problem
            out = batch.result.data or {}
            created += len(out.get("created", []))
            existing += len(out.get("already_existed", []))
            refused += len(out.get("refused", []))
        left_out = sorted({"ORGANIZATION", "LOCATION", "ADDRESS"} - set(chosen)) if categories is None else []
        text = (
            f"Proposed {created} new redaction(s) in {', '.join(chosen)} ({existing} already existed, {refused} not saved).\n"
            "They are only proposals: nothing is approved or redacted until you approve each one in the Findings panel."
        )
        if left_out:
            text += f"\nNot proposed: {', '.join(left_out)}. Add them by name if you want them, e.g.  propose organization"
        return text + self._notes(data)

    async def _policy(self, call, rest: str) -> str:
        if not rest:
            return "Ask a question about the policies, e.g.  policy can company names stay visible?"
        executed = await call("retrieve_policy_guidance", {"query": rest[:500]})
        if (problem := self._tool_problem("retrieve_policy_guidance", executed)):
            return problem
        data = executed.result.data or {}
        passages = data.get("passages", [])[:MAX_POLICY_PASSAGES]
        if data.get("status") != "ok" or not passages:
            return (
                f"{data.get('message') or 'No policy passage was returned.'}\n"
                "That does not prove the policy is silent on this: the search may simply have missed it. "
                "Read the Policy tab if it matters."
            )
        lines = ["The most similar policy passages (read them to decide whether they answer your question):"]
        for passage in passages:
            lines.append(
                f"  • {passage.get('policy_title')}, {passage.get('section')} "
                f"(similarity {passage.get('score', 0):.2f}):\n    “{_first_sentence(passage.get('text', ''))}” [{passage.get('citation_id')}]"
            )
        return "\n".join(lines)

    async def _summary(self, call, rest: str) -> str:
        executed = await call("get_review_summary", {})
        if (problem := self._tool_problem("get_review_summary", executed)):
            return problem
        s = (executed.result.data or {}).get("summary", {})
        by_status = s.get("by_status", {})
        parts = ", ".join(f"{n} {name}" for name, n in sorted(by_status.items())) or "no findings yet"
        lines = [f"Review status: {parts}.", f"Undecided: {s.get('unresolved', 0)}. Approved: {s.get('approved', 0)}."]
        if s.get("can_export"):
            lines.append("The reviewer can export now.")
        else:
            lines.extend(f"Blocked: {b}" for b in s.get("export_blockers", [])[:5])
        lines.extend(f"Warning: {w}" for w in s.get("warnings", [])[:5])
        return "\n".join(lines)

    async def _export(self, call, rest: str) -> str:
        executed = await call("export_approved_redactions", {})
        if not executed.result.ok:
            error = executed.result.error or ""
            if "authorization_required" in error:
                return (
                    "I cannot create the redacted file yet. A reviewer has to click “Let the assistant export” on the card that appears below "
                    "this message in the Chat tab (it lasts 5 minutes and allows one export). "
                    "The Export tab has its own button that needs nothing from me."
                )
            return self._tool_problem("export_approved_redactions", executed) or "The export did not run."
        data = executed.result.data or {}
        lines = [
            f"Export {data.get('status')}: {data.get('approved_count', 0)} approved redaction(s); "
            f"{'ready to download in the Export tab' if data.get('downloadable') else 'not downloadable yet'}.",
        ]
        if data.get("next_step"):
            lines.append(str(data["next_step"]))
        return "\n".join(lines)
