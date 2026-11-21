"""
Enrichment Agent — CLI front-end for the Lead-Gen MCP.
=======================================================
Uses LangChain's `create_agent` (LangGraph) + ChatGroq, talking to the
enrichment MCP over stdio.

Key design decisions
---------------------
* Three Groq API keys rotate round-robin (GROQ_API_KEY_1/2/3) so we
  spread requests across free-tier quota limits.
* Automatic key fallback on rate-limiting (429) or invalid API keys (401).
* Ultra-conservative rate limiting: 3 req/min, 20 s between calls.
* Aggressive context compaction keeps us well under Groq token limits.
* `recursion_limit` in the LangGraph config caps the tool-call chain
  length per turn (replaces the old, non-existent `max_tool_calls` key).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import time
from collections import deque
from typing import Any

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_groq import ChatGroq
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.errors import GraphRecursionError


load_dotenv()


# ============================================================
# CONFIG
# ============================================================

MAX_REQUESTS_PER_MINUTE = 3
MAX_HISTORY_MESSAGES = 10  # was 8
MAX_TOOL_RESULT_CHARS = 1_600
MAX_HISTORY_CHARS = 6_000
MIN_REQUEST_INTERVAL = 20.0

# LangGraph recursion_limit = max graph nodes per invocation.
# Each tool call + LLM response = 2 nodes.
# Full workflow: enrich_business + find_decision_makers + qualify_lead = 3 tools = 6 nodes min.
# Set to 20 to allow lead discovery + full workflow + one retry step.
RECURSION_LIMIT = 20  # was 8 (only 4 tool calls — far too low)

GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
AGENT_MAX_TOKENS = max(150, int(os.getenv("AGENT_MAX_TOKENS", "512")))


# ============================================================
# KEY ROTATION
# ============================================================


def _load_groq_keys() -> list[str]:
    """Collect all non-empty GROQ_API_KEY_1/2/3 values."""
    keys: list[str] = []
    for i in (1, 2, 3):
        k = os.getenv(f"GROQ_API_KEY_{i}", "").strip()
        if k:
            keys.append(k)
    legacy = os.getenv("GROQ_API_KEY", "").strip()
    if legacy and legacy not in keys:
        keys.append(legacy)
    return keys


_groq_key_cycle: "itertools.cycle[str] | None" = None


def _next_groq_key() -> str:
    global _groq_key_cycle
    keys = _load_groq_keys()
    if not keys:
        raise RuntimeError(
            "No Groq API key found. Set GROQ_API_KEY_1 (and optionally _2/_3) "
            "in agent/.env."
        )
    if _groq_key_cycle is None:
        _groq_key_cycle = itertools.cycle(keys)
    return next(_groq_key_cycle)


# ============================================================
# RATE LIMITER
# ============================================================


class RateLimiter:
    """Sliding-window rate limiter with minimum spacing."""

    def __init__(
        self,
        max_requests: int,
        window_seconds: float = 60.0,
        min_interval: float = 0.0,
    ):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.min_interval = min_interval
        self.timestamps: deque[float] = deque()
        self.last_request = 0.0
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                while (
                    self.timestamps and now - self.timestamps[0] >= self.window_seconds
                ):
                    self.timestamps.popleft()

                elapsed = now - self.last_request
                if elapsed < self.min_interval:
                    wait = self.min_interval - elapsed
                    print(f"\n⏳ Spacing wait {wait:.1f}s…\n")
                    await asyncio.sleep(wait)
                    continue

                if len(self.timestamps) >= self.max_requests:
                    wait = self.window_seconds - (now - self.timestamps[0]) + 0.1
                    print(f"\n⏳ Rate limit. Waiting {wait:.1f}s…\n")
                    await asyncio.sleep(wait)
                    continue

                self.timestamps.append(time.monotonic())
                self.last_request = time.monotonic()
                return


# ============================================================
# CONTEXT COMPRESSION
# ============================================================


def _role(message: Any) -> str:
    return str(
        getattr(message, "type", None) or getattr(message, "role", None) or "unknown"
    )


def _content(message: Any) -> str:
    c = getattr(message, "content", "")
    if c is None:
        return ""
    return c if isinstance(c, str) else str(c)


def _estimate_tokens(chars: int) -> int:
    return chars // 4


def _compact_tool_result(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text

    try:
        data = json.loads(text)
        compact: dict[str, Any] = {}
        for k, v in data.items():
            if k in {"raw_website", "raw_linkedin", "raw_facebook"}:
                compact[k] = "[omitted — use page_summaries]"
                continue
            if isinstance(v, list):
                if len(v) == 0:
                    compact[k] = []
                elif len(v) > 5:
                    compact[k] = v[:3] + [f"… +{len(v) - 3} more omitted"]
                else:
                    trimmed = []
                    for item in v:
                        if isinstance(item, dict):
                            trimmed.append(
                                {
                                    ik: (
                                        iv[:200] + "…"
                                        if isinstance(iv, str) and len(iv) > 200
                                        else iv
                                    )
                                    for ik, iv in item.items()
                                }
                            )
                        else:
                            trimmed.append(item)
                    compact[k] = trimmed
            elif isinstance(v, dict):
                compact[k] = {
                    ik: (
                        iv[:200] + "…" if isinstance(iv, str) and len(iv) > 200 else iv
                    )
                    for ik, iv in list(v.items())[:6]
                }
            elif isinstance(v, str) and len(v) > 300:
                compact[k] = v[:250] + "…"
            else:
                compact[k] = v

        result = json.dumps(compact, ensure_ascii=False)
        if len(result) <= max_chars:
            return result

        for k, v in compact.items():
            if isinstance(v, list) and len(v) > 2:
                compact[k] = v[:2] + ["… +more omitted"]
        result = json.dumps(compact, ensure_ascii=False)
        if len(result) <= max_chars:
            return result

    except Exception:
        pass

    head = int(max_chars * 0.65)
    tail = max_chars - head
    return (
        text[:head]
        + f"\n\n[… TRUNCATED {len(text) - max_chars} chars …]\n\n"
        + text[-tail:]
    )


def _compact_message(message: Any) -> Any:
    if _role(message) in {"tool", "function"}:
        c = _content(message)
        if len(c) > MAX_TOOL_RESULT_CHARS:
            new_c = _compact_tool_result(c, MAX_TOOL_RESULT_CHARS)
            try:
                return message.model_copy(update={"content": new_c})
            except Exception:
                try:
                    return message.copy(update={"content": new_c})
                except Exception:
                    pass
    return message


def compact_history(history: list[Any]) -> list[Any]:
    if not history:
        return []
    history = history[-MAX_HISTORY_MESSAGES:]
    history = [_compact_message(m) for m in history]

    kept: list[Any] = []
    total = 0
    for msg in reversed(history):
        chars = len(_content(msg))
        if _estimate_tokens(total + chars) > MAX_HISTORY_CHARS // 4 and kept:
            break
        kept.append(msg)
        total += chars
    kept.reverse()
    return kept


def print_context_stats(history: list[Any], turn: int = 0) -> None:
    total = sum(len(_content(m)) for m in history)
    tool = sum(len(_content(m)) for m in history if _role(m) in {"tool", "function"})
    print(
        f"[📊 T{turn}] {len(history)} msgs | "
        f"~{_estimate_tokens(total):,} tokens | "
        f"tool: {tool:,} chars"
    )


def _final_text_response(messages: list[Any]) -> Any | None:
    """Return the latest actual assistant answer, skipping tool-call messages.

    Tool-calling models commonly emit an AI message with empty content while
    requesting a tool.  It is an intermediate protocol message, not something
    useful to show as the CLI answer.
    """
    for message in reversed(messages):
        if _role(message) != "ai":
            continue
        if getattr(message, "tool_calls", None):
            continue
        if _content(message).strip():
            return message
    return None


def _current_turn_messages(messages: list[Any]) -> list[Any]:
    """Return messages produced after the latest user request."""
    for index in range(len(messages) - 1, -1, -1):
        if _role(messages[index]) in {"human", "user"}:
            return messages[index + 1 :]
    return messages


def _last_tool_summary(messages: list[Any]) -> str | None:
    """Give the user a bounded, useful status if the model omitted its finale."""
    for message in reversed(messages):
        if _role(message) not in {"tool", "function"}:
            continue
        name = getattr(message, "name", None) or "a workspace/research tool"
        result = _content(message).strip()
        if result:
            return f"Last completed tool: {name}. Result: {_compact_tool_result(result, 500)}"
        return f"Last completed tool: {name}."
    return None


# ============================================================
# MAIN
# ============================================================


async def main() -> None:

    mcp_server_path = os.getenv("MCP_SERVER_PATH")
    if not mcp_server_path:
        raise RuntimeError("MCP_SERVER_PATH is missing from agent/.env")

    groq_keys = _load_groq_keys()
    if not groq_keys:
        raise RuntimeError(
            "No Groq API key found. Set GROQ_API_KEY_1 (and optionally _2 / _3) "
            "in agent/.env."
        )

    print(f"🔑 Loaded {len(groq_keys)} Groq API key(s) for rotation.")

    mcp = MultiServerMCPClient(
        {
            "enrichment": {
                "transport": "stdio",
                "command": mcp_server_path,
                "args": [],
            }
        }
    )

    tools = await mcp.get_tools()
    print(f"\n✅ Connected to enrichment MCP. Loaded {len(tools)} tools:\n")
    for t in tools:
        print(f"    • {t.name}")

    limiter = RateLimiter(
        max_requests=MAX_REQUESTS_PER_MINUTE,
        window_seconds=60.0,
        min_interval=MIN_REQUEST_INTERVAL,
    )

    history: list[Any] = []
    turn = 0

    print("\n🚀 Agent ready.")
    print(
        f"⏱️  Rate limit: {MAX_REQUESTS_PER_MINUTE} req/min | "
        f"min spacing: {MIN_REQUEST_INTERVAL}s | "
        f"recursion limit: {RECURSION_LIMIT}"
    )
    print("Type 'exit' to quit.\n")

    system_prompt = """You are a concise, evidence-led business researcher.

Use the smallest tool sequence that answers the request. Never repeat an
identical tool call or research every result.

For a named business, call enrich_business. Call find_decision_makers only if
the user asks for a person/contact. Call qualify_lead only if they ask for a
score, service recommendation, or outreach. Pass page_summaries to the next
tool when used. For a general search, call find_leads and return candidates;
deep-enrich at most one selected candidate unless a comparison is requested.

For workspace files, first list or inspect, then read only the relevant data.
Use exact, bounded edits. File paths are relative to the dedicated workspace
folder; never claim a write succeeded without its tool result.

For an existing CSV, TSV, or Excel sheet, inspect its header then use
add_spreadsheet_column, update_spreadsheet_rows, or append_spreadsheet_rows
instead of rewriting the whole file. Use expected_matches for a targeted row
update whenever the requested row count is known.

For website research, assess first and choose one listed website candidate.
Crawl only one page per crawl_business_pages call; choose a single returned
recommended page only when the evidence requires it. Never request a broad or
automatic multi-page crawl.

For outreach email, create_email_draft and show its full recipient, subject,
and body to the user. Only call send_approved_email after the user has supplied
the exact approval phrase returned by the draft tool; never infer approval.

All MCP operations are recorded in the workspace database. Before repeating
contact research, use search_stored_contacts; use list_tool_runs only when an
audit or recovery view is requested.

Return only the requested facts and strongest evidence. Tools do matching, so
keep their evidence even when the final response is short.""".strip()

    while True:
        try:
            user_input = input("You > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            print("Goodbye!")
            break

        history.append({"role": "user", "content": user_input})
        history = compact_history(history)
        turn += 1
        print_context_stats(history, turn)

        max_retries = len(groq_keys)

        for attempt in range(max_retries):
            api_key = _next_groq_key()
            masked_key = f"...{api_key[-4:]}" if len(api_key) > 4 else "valid_key"

            model = ChatGroq(
                model=GROQ_MODEL,
                temperature=0,
                api_key=api_key,
                max_tokens=AGENT_MAX_TOKENS,
            )

            agent = create_agent(
                model=model,
                tools=tools,
                system_prompt=system_prompt,
            )

            await limiter.acquire()
            print(
                f"🔄 Processing with key ({masked_key}) [Attempt {attempt + 1}/{max_retries}]…\n"
            )

            try:
                result = await agent.ainvoke(
                    {"messages": history},
                    config={"recursion_limit": RECURSION_LIMIT},
                )

                new_history = result.get("messages", [])
                new_history = [_compact_message(m) for m in new_history]
                history = compact_history(new_history)

                turn_messages = _current_turn_messages(new_history)
                response = _final_text_response(turn_messages)

                if response is None:
                    print("⚠️ The model completed but returned no textual final answer.")
                    summary = _last_tool_summary(turn_messages)
                    if summary:
                        print(f"{summary}\n")
                    else:
                        print("No tool result was produced; please retry the request.\n")
                    break

                print(f"Agent > {_content(response)}\n")
                break

            except GraphRecursionError:
                print(
                    f"🛑 Agent reached the internal step limit ({RECURSION_LIMIT} nodes)."
                )
                print(
                    "The agent got stuck in a loop or took too long to complete the task."
                )
                if history and _role(history[-1]) in {"human", "user"}:
                    history.pop()
                break

            except Exception as exc:
                err_msg = str(exc)
                print(f"⚠️ Key ({masked_key}) failed: {err_msg}")

                if attempt < max_retries - 1:
                    print("🔀 Rotating to next API key...\n")
                    await asyncio.sleep(1.0)
                else:
                    print(f"\n❌ All {max_retries} API key(s) failed.\n")
                    if history and _role(history[-1]) in {"human", "user"}:
                        history.pop()


if __name__ == "__main__":
    asyncio.run(main())
