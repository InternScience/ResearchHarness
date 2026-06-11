import argparse
import os
import re
import sys
import time
from typing import Optional, Union

import requests

from agent_base.tools.tooling import ToolBase
from agent_base.utils import PROJECT_ROOT, env_flag, load_dotenv

DEFAULT_WEBFETCH_TIMEOUT_SECONDS = 300.0
DEFAULT_WEBFETCH_MAX_CHARS = 8192


def webfetch_timeout_seconds() -> float:
    timeout = float(os.getenv("WEBFETCH_TIMEOUT_SECONDS", str(DEFAULT_WEBFETCH_TIMEOUT_SECONDS)))
    if timeout <= 0:
        raise ValueError("WEBFETCH_TIMEOUT_SECONDS must be > 0.")
    return timeout


def webfetch_default_max_chars() -> int:
    max_chars = int(os.getenv("WEBFETCH_MAX_CHARS", str(DEFAULT_WEBFETCH_MAX_CHARS)))
    if max_chars <= 0:
        raise ValueError("WEBFETCH_MAX_CHARS must be > 0.")
    return max_chars


def search_debug_enabled() -> bool:
    return env_flag("DEBUG_SEARCH")


def scholar_debug_enabled() -> bool:
    return env_flag("DEBUG_SCHOLAR")


def visit_debug_enabled() -> bool:
    return env_flag("DEBUG_VISIT")


def _request_error_text(exc: requests.RequestException) -> str:
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    body = response.text.strip()
    if len(body) > 1000:
        body = body[:1000] + "...(truncated)"
    return f"{exc}; response_body={body}" if body else str(exc)


def _clean_webpage_text(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


class WebSearch(ToolBase):
    name = "WebSearch"
    description = "Perform one Google web search and return the top results. Call WebSearch multiple times for multiple queries."
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query.",
            },
        },
        "required": ["query"],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)

    def google_search_with_serp(self, query: str):
        def contains_chinese_basic(text: str) -> bool:
            return any("\u4E00" <= char <= "\u9FFF" for char in text)

        if contains_chinese_basic(query):
            payload = {
                "q": query,
                "location": "China",
                "gl": "cn",
                "hl": "zh-cn",
            }
        else:
            payload = {
                "q": query,
                "location": "United States",
                "gl": "us",
                "hl": "en",
            }
        serper_key = os.getenv("SERPER_KEY", "").strip()
        if not serper_key:
            return "[WebSearch] SERPER_KEY is not set."
        headers = {
            "X-API-KEY": serper_key,
            "Content-Type": "application/json",
        }

        last_error = ""
        res = None
        for i in range(5):
            try:
                res = requests.post(
                    "https://google.serper.dev/search",
                    json=payload,
                    headers=headers,
                    timeout=20,
                )
                res.raise_for_status()
                break
            except requests.RequestException as exc:
                last_error = _request_error_text(exc)
                if search_debug_enabled():
                    print(exc)
                if i == 4:
                    return f"[WebSearch] Request failed for '{query}': {last_error}"

        if res is None:
            return f"[WebSearch] Request failed for '{query}': {last_error or 'unknown error'}"

        try:
            results = res.json()
        except ValueError as exc:
            return f"[WebSearch] Invalid JSON response for '{query}': {exc}"

        organic_results = results.get("organic")
        if not isinstance(organic_results, list) or not organic_results:
            return f"No results found for '{query}'. Try with a more general query."

        web_snippets = []
        for idx, page in enumerate(organic_results, start=1):
            if not isinstance(page, dict):
                continue
            title = str(page.get("title", "Untitled result"))
            link = str(page.get("link", ""))
            date_published = f"\nDate published: {page['date']}" if "date" in page else ""
            source = f"\nSource: {page['source']}" if "source" in page else ""
            snippet = f"\n{page['snippet']}" if "snippet" in page else ""
            redacted_version = f"{idx}. [{title}]({link}){date_published}{source}\n{snippet}"
            redacted_version = redacted_version.replace("Your browser can't play this video.", "")
            web_snippets.append(redacted_version)

        if not web_snippets:
            return f"No results found for '{query}'. Try with a more general query."

        content = f"A Google search for '{query}' found {len(web_snippets)} results:\n\n## Web Results\n" + "\n\n".join(web_snippets)
        return content

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self.parse_json_args(params)
            query = params["query"]
        except ValueError as exc:
            return f"[WebSearch] {exc}"

        if not isinstance(query, str) or not query.strip():
            return "[WebSearch] 'query' must be a non-empty string."

        return self.google_search_with_serp(query.strip())


class ScholarSearch(ToolBase):
    name = "ScholarSearch"
    description = "Run one academic search through Google Scholar and return relevant publication results. Call ScholarSearch multiple times for multiple queries."
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query for Google Scholar.",
            },
        },
        "required": ["query"],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)

    def google_scholar_with_serp(self, query: str):
        payload = {"q": query}
        serper_key = os.getenv("SERPER_KEY", "").strip()
        if not serper_key:
            return "[ScholarSearch] SERPER_KEY is not set."
        headers = {
            "X-API-KEY": serper_key,
            "Content-Type": "application/json",
        }
        last_error = ""
        res = None
        for i in range(5):
            try:
                res = requests.post(
                    "https://google.serper.dev/scholar",
                    json=payload,
                    headers=headers,
                    timeout=20,
                )
                res.raise_for_status()
                break
            except requests.RequestException as exc:
                last_error = _request_error_text(exc)
                if scholar_debug_enabled():
                    print(exc)
                if i == 4:
                    return f"[ScholarSearch] Request failed for '{query}': {last_error}"

        if res is None:
            return f"[ScholarSearch] Request failed for '{query}': {last_error or 'unknown error'}"

        try:
            results = res.json()
        except ValueError as exc:
            return f"[ScholarSearch] Invalid JSON response for '{query}': {exc}"

        organic_results = results.get("organic")
        if not isinstance(organic_results, list) or not organic_results:
            return f"No results found for '{query}'. Try with a more general query."

        web_snippets = []
        for idx, page in enumerate(organic_results, start=1):
            if not isinstance(page, dict):
                continue
            title = str(page.get("title", "Untitled result"))
            date_published = f"\nDate published: {page['year']}" if "year" in page else ""
            publication_info = f"\npublicationInfo: {page['publicationInfo']}" if "publicationInfo" in page else ""
            snippet = f"\n{page['snippet']}" if "snippet" in page else ""
            link_info = "no available link"
            if "pdfUrl" in page:
                link_info = "pdfUrl: " + str(page["pdfUrl"])
            cited_by = f"\ncitedBy: {page['citedBy']}" if "citedBy" in page else ""
            redacted_version = f"{idx}. [{title}]({link_info}){publication_info}{date_published}{cited_by}\n{snippet}"
            redacted_version = redacted_version.replace("Your browser can't play this video.", "")
            web_snippets.append(redacted_version)

        if not web_snippets:
            return f"No results found for '{query}'. Try with a more general query."

        content = f"A Google scholar for '{query}' found {len(web_snippets)} results:\n\n## Scholar Results\n" + "\n\n".join(web_snippets)
        return content

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self.parse_json_args(params)
            query = params["query"]
        except ValueError as exc:
            return f"[ScholarSearch] {exc}"

        if not isinstance(query, str) or not query.strip():
            return "[ScholarSearch] 'query' must be a non-empty string."
        return self.google_scholar_with_serp(query.strip())


class WebFetch(ToolBase):
    name = "WebFetch"
    description = "Fetch webpage content and return cleaned, range-bounded page text for the agent to inspect."
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL of the webpage to visit. Call WebFetch multiple times for multiple URLs.",
            },
            "start_line": {
                "type": "integer",
                "description": "Optional 1-based start line for partial reading. Default is 1.",
            },
            "end_line": {
                "type": "integer",
                "description": "Optional 1-based end line for partial reading. If omitted, read to the end.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum number of characters to return. Default is 8192 and the value must not exceed WEBFETCH_MAX_CHARS.",
            },
        },
        "required": ["url"],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)

    @staticmethod
    def _remaining_budget_seconds(runtime_deadline: Optional[float]) -> Optional[float]:
        if runtime_deadline is None:
            return None
        return runtime_deadline - time.time()

    @staticmethod
    def _webfetch_deadline(runtime_deadline: Optional[float]) -> float:
        tool_deadline = time.time() + webfetch_timeout_seconds()
        if runtime_deadline is None:
            return tool_deadline
        return min(float(runtime_deadline), tool_deadline)

    @staticmethod
    def _format_page_content(
        *,
        url: str,
        content: str,
        start_line: int,
        end_line: Optional[int],
        max_chars: int,
    ) -> str:
        clean_content = _clean_webpage_text(content)
        lines = clean_content.splitlines()
        selected_lines = lines[start_line - 1:end_line]
        selected_content = "\n".join(selected_lines)
        truncated = len(selected_content) > max_chars
        returned_content = selected_content[:max_chars] if truncated else selected_content
        effective_end_line = end_line if end_line is not None else len(lines)
        meta = [
            f"url: {url}",
            "source_type: web",
            f"start_line: {start_line}",
            f"end_line: {effective_end_line}",
            f"total_lines: {len(lines)}",
            f"total_chars: {len(clean_content)}",
            f"max_chars: {max_chars}",
            f"returned_chars: {len(returned_content)}",
            f"truncated: {str(truncated).lower()}",
        ]
        if truncated:
            meta.append("note: content was truncated by max_chars; use a narrower line range, or raise max_chars only up to WEBFETCH_MAX_CHARS if this call used a smaller value.")
        return "\n".join(meta) + "\ncontent:\n" + returned_content

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self.parse_json_args(params)
            url = params["url"]
        except ValueError as exc:
            return f"[WebFetch] {exc}"
        try:
            start_line = int(params.get("start_line", 1))
            end_line_raw = params.get("end_line")
            end_line = int(end_line_raw) if end_line_raw is not None else None
            max_chars_limit = webfetch_default_max_chars()
            max_chars_raw = params.get("max_chars")
            max_chars = int(max_chars_raw) if max_chars_raw is not None else max_chars_limit
        except (TypeError, ValueError):
            return "[WebFetch] start_line, end_line, and max_chars must be integers when provided."
        if start_line < 1:
            return "[WebFetch] start_line must be >= 1."
        if end_line is not None and end_line < start_line:
            return "[WebFetch] end_line must be >= start_line."
        if max_chars <= 0:
            return "[WebFetch] max_chars must be > 0."
        if max_chars > max_chars_limit:
            return f"[WebFetch] max_chars must be <= WEBFETCH_MAX_CHARS ({max_chars_limit}). Use a narrower line range to read more of the page."
        try:
            runtime_deadline = self._webfetch_deadline(kwargs.get("runtime_deadline"))
        except ValueError as exc:
            return f"[WebFetch] {exc}"

        response = self.readpage_jina(
            url,
            start_line=start_line,
            end_line=end_line,
            max_chars=max_chars,
            runtime_deadline=runtime_deadline,
        )

        if visit_debug_enabled():
            print(f"WebFetch Length {len(response)}")
        return response.strip()

    def jina_readpage(self, url: str, runtime_deadline: Optional[float] = None) -> str:
        max_retries = 3
        timeout = 50
        jina_api_key = os.getenv("JINA_KEY", "").strip()
        if not jina_api_key:
            return "[WebFetch] JINA_KEY is not set."

        last_error = "unknown page-fetch error"
        for attempt in range(max_retries):
            headers = {
                "Authorization": f"Bearer {jina_api_key}",
            }
            try:
                remaining = self._remaining_budget_seconds(runtime_deadline)
                if remaining is not None and remaining <= 0:
                    return "[WebFetch] Failed to read page: agent runtime limit reached."
                response = requests.get(
                    f"https://r.jina.ai/{url}",
                    headers=headers,
                    timeout=min(timeout, max(remaining, 0.001)) if remaining is not None else timeout,
                )
                if response.status_code == 200:
                    return response.text
                if visit_debug_enabled():
                    print(response.text)
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            except requests.RequestException as exc:
                last_error = str(exc)
                remaining = self._remaining_budget_seconds(runtime_deadline)
                if remaining is not None and remaining <= 0:
                    return "[WebFetch] Failed to read page: agent runtime limit reached."
                time.sleep(min(0.5, remaining) if remaining is not None else 0.5)
                if attempt == max_retries - 1:
                    return f"[WebFetch] Failed to read page: {last_error}"

        return f"[WebFetch] Failed to read page: {last_error}"

    def html_readpage_jina(self, url: str, runtime_deadline: Optional[float] = None) -> str:
        max_attempts = 8
        for _ in range(max_attempts):
            remaining = self._remaining_budget_seconds(runtime_deadline)
            if remaining is not None and remaining <= 0:
                return "[WebFetch] Failed to read page: agent runtime limit reached."
            content = self.jina_readpage(url, runtime_deadline=runtime_deadline)
            if content and not content.startswith("[WebFetch] Failed to read page:") and content != "[WebFetch] Empty content." and not content.startswith("[document_parser]"):
                return content
        return "[WebFetch] Failed to read page: exhausted retries"

    def readpage_jina(
        self,
        url: str,
        *,
        start_line: int = 1,
        end_line: Optional[int] = None,
        max_chars: int = DEFAULT_WEBFETCH_MAX_CHARS,
        runtime_deadline: Optional[float] = None,
    ) -> str:
        content = self.html_readpage_jina(url, runtime_deadline=runtime_deadline)
        if not content or content.startswith("[WebFetch] Failed to read page:") or content == "[WebFetch] Empty content." or content.startswith("[document_parser]"):
            return "[WebFetch] Failed to read page: the provided webpage content could not be accessed. Please check the URL or file format."
        return self._format_page_content(
            url=url,
            content=content,
            start_line=start_line,
            end_line=end_line,
            max_chars=max_chars,
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run web tools directly.")
    subparsers = parser.add_subparsers(dest="tool", required=True)

    search_parser = subparsers.add_parser("search", help="Run WebSearch.")
    search_parser.add_argument("query", nargs="+")

    scholar_parser = subparsers.add_parser("scholar", help="Run ScholarSearch.")
    scholar_parser.add_argument("query", nargs="+")

    fetch_parser = subparsers.add_parser("fetch", help="Run WebFetch.")
    fetch_parser.add_argument("url")
    fetch_parser.add_argument("--start-line", type=int, default=1)
    fetch_parser.add_argument("--end-line", type=int)
    fetch_parser.add_argument("--max-chars", type=int)

    args = parser.parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env")

    if args.tool == "search":
        result = WebSearch().call({"query": " ".join(args.query)})
    elif args.tool == "scholar":
        result = ScholarSearch().call({"query": " ".join(args.query)})
    else:
        result = WebFetch().call(
            {
                "url": args.url,
                "start_line": args.start_line,
                "end_line": args.end_line,
                "max_chars": args.max_chars if args.max_chars is not None else webfetch_default_max_chars(),
            }
        )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
