"""Anonymous Parallel Search MCP caller for the synchronous WebSearch tool."""

import asyncio
import math
import os
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation


PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
USER_AGENT = "ResearchHarness (https://github.com/InternScience/ResearchHarness)"


async def _search(query: str) -> str:
    # This dedicated client never inherits service keys or authentication from
    # another provider. The project identity applies to every MCP request.
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        async with streamable_http_client(PARALLEL_MCP_URL, http_client=client) as (reader, writer, _):
            async with ClientSession(
                reader, writer, client_info=Implementation(name="ResearchHarness-ParallelSearch", version="1")
            ) as session:
                await session.initialize()
                tools = await session.list_tools()
                if not any(tool.name == "web_search" for tool in tools.tools):
                    raise ValueError("Parallel MCP did not advertise web_search.")
                result = await session.call_tool(
                    "web_search", {"objective": query, "search_queries": [query]}
                )
                text = "\n\n".join(
                    block.text for block in result.content if block.type == "text"
                ).strip()
                if result.isError:
                    raise ValueError(text or "Parallel MCP returned a tool error.")
                if not text:
                    raise ValueError("Parallel MCP returned no text results.")
                return f"A Parallel search for '{query}':\n\n## Web Results\n{text}"


async def _bounded_search(query: str, timeout: float, interrupt_event) -> str:
    task = asyncio.create_task(_search(query))
    deadline = time.monotonic() + timeout
    try:
        while True:
            if interrupt_event is not None and interrupt_event.is_set():
                return "[WebSearch] Parallel search cancelled."
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "[WebSearch] Parallel search timed out."
            done, _ = await asyncio.wait({task}, timeout=min(remaining, 0.05))
            if done:
                return task.result()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _error_message(exc: Exception) -> str:
    # MCP transport task groups can wrap the useful server error.
    children = getattr(exc, "exceptions", None)
    if children:
        return "; ".join(_error_message(child) for child in children)
    return str(exc)


def search_parallel(query: str, *, runtime_deadline=None, interrupt_event=None) -> str:
    try:
        timeout = float(os.getenv("WEBSEARCH_TIMEOUT_SECONDS", "30"))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("WEBSEARCH_TIMEOUT_SECONDS must be a finite number > 0.")
        if runtime_deadline is not None:
            timeout = min(timeout, float(runtime_deadline) - time.time())
        if timeout <= 0:
            return "[WebSearch] Agent runtime limit reached."
        return asyncio.run(_bounded_search(query, timeout, interrupt_event))
    except Exception as exc:
        return f"[WebSearch] Parallel search failed: {_error_message(exc)}"
