"""Offline MCP transport and scripted-agent regression tests (no model calls)."""

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from agent_base.tools import parallel_search
from agent_base.tools.tool_web import WebSearch
from agent_base.utils import load_dotenv, require_required_env
from researchharness import create_agent


class MCPFixture:
    """Use the real SDK and wire format, replacing only the HTTP network."""

    def __init__(self, *, error=False, delay=0, malformed=False, missing_tool=False, http_error=False, empty=False):
        self.requests = []
        self.error = error
        self.delay = delay
        self.malformed = malformed
        self.missing_tool = missing_tool
        self.cancelled = False
        self.http_error = http_error
        self.empty = empty

    async def handle(self, request):
        payload = json.loads(request.content)
        self.requests.append((request, payload))
        method = payload["method"]
        if method.startswith("notifications/"):
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [] if self.missing_tool else [{
                "name": "web_search",
                "inputSchema": {"type": "object", "properties": {}},
            }]}
        elif method == "tools/call":
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            if self.malformed:
                return httpx.Response(200, content=b"not-json", headers={"Content-Type": "application/json"})
            if self.http_error:
                return httpx.Response(503, text="Service unavailable")
            result = {"content": [{"type": "text", "text": (
                "Free usage quota exceeded" if self.error else
                "1. [Research paper](https://example.org/paper)\nEvidence excerpt."
            )}], "isError": self.error}
            if self.empty:
                result["content"] = []
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})


class ParallelSearchTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def agent(self, tools=None, require_env=False):
        class Encoding:
            def encode(self, text, **kwargs):
                return list(str(text))

        with patch("tiktoken.get_encoding", return_value=Encoding()):
            return create_agent(
                model_name="scripted-model", api_key="model-only", api_base="http://127.0.0.1:9/v1",
                tools=["WebSearch"] if tools is None else tools,
                workspace_root=self.tmp.name, require_env=require_env, max_rounds=3,
            )

    def transport(self, fixture):
        original = httpx.AsyncClient
        def client(**kwargs):
            return original(transport=httpx.MockTransport(fixture.handle), **kwargs)
        return patch.object(parallel_search.httpx, "AsyncClient", side_effect=client)

    def assert_wire(self, fixture):
        methods = [payload["method"] for _, payload in fixture.requests]
        self.assertIn("initialize", methods)
        self.assertIn("notifications/initialized", methods)
        self.assertIn("tools/list", methods)
        self.assertIn("tools/call", methods)
        for request, payload in fixture.requests:
            self.assertEqual(str(request.url), "https://search.parallel.ai/mcp")
            self.assertEqual(request.headers["User-Agent"], parallel_search.USER_AGENT)
            self.assertNotIn("Authorization", request.headers)
            self.assertNotIn("X-API-KEY", request.headers)
            if payload["method"] == "tools/call":
                self.assertEqual(payload["params"]["name"], "web_search")
                self.assertEqual(payload["params"]["arguments"], {
                    "objective": "research evidence", "search_queries": ["research evidence"],
                })

    def test_fresh_no_key_agent_consumes_search_evidence(self):
        os.environ.update(API_KEY="model-only", API_BASE="http://127.0.0.1:9/v1", MODEL_NAME="scripted-model")
        require_required_env()
        agent = self.agent(require_env=True)
        fixture = MCPFixture()
        def scripted_model(messages, **kwargs):
            results = [m for m in messages if m.get("role") == "tool"]
            if not results:
                return {"status": "ok", "finish_reason": "tool_calls", "content": "", "tool_calls": [{
                    "id": "search-1", "type": "function", "function": {
                        "name": "WebSearch", "arguments": json.dumps({"query": "research evidence"}),
                    },
                }]}
            self.assertIn("https://example.org/paper", results[-1]["content"])
            self.assertIn("Evidence excerpt.", results[-1]["content"])
            return {"status": "ok", "finish_reason": "stop", "content": "Evidence: https://example.org/paper", "tool_calls": []}
        with self.transport(fixture), patch.object(agent, "call_llm_api", side_effect=scripted_model):
            result = agent._run_session("Research evidence")
        self.assertEqual(result["result_text"], "Evidence: https://example.org/paper")
        self.assert_wire(fixture)

    def test_configured_serper_remains_selected(self):
        os.environ["SERPER_KEY"] = "existing-key"
        with patch.object(WebSearch, "google_search_with_serp", return_value="existing result") as serper:
            self.assertEqual(self.agent().custom_call_tool("WebSearch", {"query": "query"}), "existing result")
            serper.assert_called_once_with("query")

    def test_explicit_parallel_overrides_serper_without_forwarding_keys(self):
        os.environ.update(SERPER_KEY="existing-key", PARALLEL_API_KEY="ambient-key", WEBSEARCH_PROVIDER="parallel")
        fixture = MCPFixture()
        with self.transport(fixture):
            result = self.agent().custom_call_tool("WebSearch", {"query": "research evidence"})
        self.assertIn("## Web Results", result)
        self.assert_wire(fixture)

    def test_explicit_serper_without_key_does_not_fallback(self):
        os.environ["WEBSEARCH_PROVIDER"] = "serper"
        with patch.object(parallel_search.httpx, "AsyncClient") as client:
            result = self.agent().custom_call_tool("WebSearch", {"query": "query"})
        self.assertIn("SERPER_KEY is not set", result)
        client.assert_not_called()

    def test_saved_choice_and_environment_override(self):
        settings = Path(self.tmp.name) / "settings.env"
        settings.write_text("WEBSEARCH_PROVIDER=disabled\nSERPER_KEY=saved-key\n")
        load_dotenv(settings)
        self.assertIn("disabled", self.agent().custom_call_tool("WebSearch", {"query": "query"}))
        os.environ["WEBSEARCH_PROVIDER"] = "serper"
        load_dotenv(settings)
        with patch.object(WebSearch, "google_search_with_serp", return_value="saved result"):
            self.assertEqual(self.agent().custom_call_tool("WebSearch", {"query": "query"}), "saved result")

    def test_tool_allowlist_prevents_search(self):
        with patch.object(parallel_search.httpx, "AsyncClient") as client:
            result = self.agent(tools=[]).custom_call_tool("WebSearch", {"query": "query"})
        self.assertIn("not found", result)
        client.assert_not_called()

    def test_invalid_provider_does_not_fallback(self):
        os.environ["WEBSEARCH_PROVIDER"] = "typo"
        with patch.object(parallel_search.httpx, "AsyncClient") as client:
            self.assertIn("must be", WebSearch().call({"query": "query"}))
        client.assert_not_called()

    def test_tool_error_surfaces_without_provider_fallback(self):
        fixture = MCPFixture(error=True)
        with self.transport(fixture), patch.object(WebSearch, "google_search_with_serp") as serper:
            result = self.agent().custom_call_tool("WebSearch", {"query": "research evidence"})
        self.assertIn("Free usage quota exceeded", result)
        self.assertNotIn("## Web Results", result)
        serper.assert_not_called()

    def test_malformed_response_surfaces(self):
        # The SDK logs malformed JSON and waits for a valid response. Our total
        # budget must still bound that wait and return no successful evidence.
        os.environ["WEBSEARCH_TIMEOUT_SECONDS"] = "0.1"
        fixture = MCPFixture(malformed=True)
        with self.transport(fixture):
            self.assertIn("timed out", self.agent().custom_call_tool("WebSearch", {"query": "query"}))

    def test_missing_tool_surfaces(self):
        fixture = MCPFixture(missing_tool=True)
        with self.transport(fixture):
            self.assertIn("did not advertise", self.agent().custom_call_tool("WebSearch", {"query": "query"}))

    def test_http_error_surfaces(self):
        fixture = MCPFixture(http_error=True)
        with self.transport(fixture):
            self.assertIn("503", self.agent().custom_call_tool("WebSearch", {"query": "query"}))

    def test_empty_results_are_not_success(self):
        fixture = MCPFixture(empty=True)
        with self.transport(fixture):
            self.assertIn("no text results", self.agent().custom_call_tool("WebSearch", {"query": "query"}))

    def test_timeout_cancels_pending_mcp_call(self):
        os.environ["WEBSEARCH_TIMEOUT_SECONDS"] = "0.1"
        fixture = MCPFixture(delay=10)
        start = time.monotonic()
        with self.transport(fixture):
            result = self.agent().custom_call_tool("WebSearch", {"query": "query"})
        self.assertIn("timed out", result)
        self.assertTrue(fixture.cancelled)
        self.assertLess(time.monotonic() - start, 2)

    def test_runtime_deadline_clips_timeout(self):
        fixture = MCPFixture(delay=10)
        with self.transport(fixture):
            result = self.agent().custom_call_tool("WebSearch", {"query": "query"}, runtime_deadline=time.time() + 0.1)
        self.assertIn("timed out", result)
        self.assertTrue(fixture.cancelled)

    def test_interrupt_cancels_pending_mcp_call(self):
        fixture = MCPFixture(delay=10)
        event = threading.Event()
        timer = threading.Timer(0.1, event.set)
        timer.start()
        try:
            with self.transport(fixture):
                result = self.agent().custom_call_tool("WebSearch", {"query": "query"}, interrupt_event=event)
        finally:
            timer.join()
        self.assertIn("cancelled", result)
        self.assertTrue(fixture.cancelled)

    def test_agent_interrupt_reaches_mcp_request(self):
        fixture = MCPFixture(delay=10)
        event = threading.Event()
        agent = self.agent()
        reply = {"status": "ok", "finish_reason": "tool_calls", "content": "", "tool_calls": [{
            "id": "search-1", "type": "function", "function": {
                "name": "WebSearch", "arguments": json.dumps({"query": "query"}),
            },
        }]}
        timer = threading.Timer(0.1, event.set)
        timer.start()
        try:
            with self.transport(fixture), patch.object(agent, "call_llm_api", return_value=reply):
                result = agent._run_session("Research evidence", interrupt_event=event)
        finally:
            timer.join()
        self.assertEqual(result["termination"], "interrupted")
        self.assertTrue(fixture.cancelled)

    def test_expired_runtime_makes_no_request(self):
        with patch.object(parallel_search.httpx, "AsyncClient") as client:
            result = self.agent().custom_call_tool("WebSearch", {"query": "query"}, runtime_deadline=time.time() - 1)
        self.assertIn("runtime limit", result)
        client.assert_not_called()

    def test_invalid_timeout_makes_no_request(self):
        for value in ["0", "-1", "nan", "inf", "invalid"]:
            with self.subTest(value=value), patch.dict(os.environ, {"WEBSEARCH_TIMEOUT_SECONDS": value}):
                with patch.object(parallel_search.httpx, "AsyncClient") as client:
                    self.assertIn("failed", WebSearch().call({"query": "query"}))
                client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
