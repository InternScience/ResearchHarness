import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Union

import requests
import tiktoken
from openai import APIConnectionError, APIError, APITimeoutError, OpenAI

from agent_base.provider_compat import apply_sampling_params
from agent_base.prompt import EXTRACTOR_PROMPT
from agent_base.tools.tooling import ToolBase
from agent_base.utils import PROJECT_ROOT, env_flag, load_dotenv

DEFAULT_LLM_TIMEOUT_SECONDS = 600.0
DEFAULT_LLM_MAX_RETRIES = 10
DEFAULT_TEMPERATURE = 0.6
DEFAULT_TOP_P = 0.95
DEFAULT_PRESENCE_PENALTY = 1.1


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


def truncate_to_tokens(text: str, max_tokens: int = 95000) -> str:
    encoding = tiktoken.get_encoding("cl100k_base")
    tokens = encoding.encode(text)
    if len(tokens) <= max_tokens:
        return text
    truncated_tokens = tokens[:max_tokens]
    return encoding.decode(truncated_tokens)


def _stringify_field(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value).strip()
    return str(value).strip()


def _webfetch_failure(url: str, goal: str, reason: str) -> str:
    useful_information = "The useful information in {url} for user goal {goal} as follows: \n\n".format(url=url, goal=goal)
    useful_information += "Evidence in page: \n" + reason + "\n\n"
    useful_information += "Summary: \n" + "The webpage content could not be processed into the required structured summary." + "\n\n"
    return useful_information


def _parse_extractor_payload(raw) -> tuple[str, str] | None:
    if isinstance(raw, str):
        raw = raw.replace("```json", "").replace("```", "").strip()
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None

    if not isinstance(raw, dict):
        return None

    evidence = _stringify_field(raw.get("evidence"))
    summary = _stringify_field(raw.get("summary"))
    if not evidence or not summary:
        return None
    return evidence, summary


class WebSearch(ToolBase):
    name = "WebSearch"
    description = "Perform Google web searches and return the top results. Accepts multiple complementary queries."
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "array",
                "items": {
                    "type": "string",
                },
                "description": "Array of query strings. Include multiple complementary search queries in a single call.",
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
        serper_key = os.getenv("SERPER_KEY_ID", "").strip()
        if not serper_key:
            return "[WebSearch] SERPER_KEY_ID is not set."
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

    def search_with_serp(self, query: str):
        return self.google_search_with_serp(query)

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self.parse_json_args(params)
            query = params["query"]
        except ValueError as exc:
            return f"[WebSearch] {exc}"

        if isinstance(query, list):
            with ThreadPoolExecutor(max_workers=3) as executor:
                responses = list(executor.map(self.search_with_serp, query))
            response = "\n=======\n".join(responses)
        else:
            return "[WebSearch] 'query' must be a list of strings."

        return response


class ScholarSearch(ToolBase):
    name = "ScholarSearch"
    description = "Search academic sources through Google Scholar and return relevant publication results."
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "array",
                "items": {"type": "string", "description": "The search query."},
                "minItems": 1,
                "description": "The list of search queries for Google Scholar.",
            },
        },
        "required": ["query"],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)

    def google_scholar_with_serp(self, query: str):
        payload = {"q": query}
        serper_key = os.getenv("SERPER_KEY_ID", "").strip()
        if not serper_key:
            return "[ScholarSearch] SERPER_KEY_ID is not set."
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

        if isinstance(query, list):
            with ThreadPoolExecutor(max_workers=3) as executor:
                response = list(executor.map(self.google_scholar_with_serp, query))
            response = "\n=======\n".join(response)
        else:
            return "[ScholarSearch] 'query' must be a list of strings."
        return response


class WebFetch(ToolBase):
    name = "WebFetch"
    description = "Fetch webpage content and return evidence plus a goal-focused summary."
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": ["string", "array"],
                "items": {
                    "type": "string",
                },
                "minItems": 1,
                "description": "The URL(s) of the webpage(s) to visit. Can be a single URL or an array of URLs.",
            },
            "goal": {
                "type": "string",
                "description": "The goal of the visit for webpage(s).",
            },
        },
        "required": ["url", "goal"],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)
        self._summary_client: Optional[OpenAI] = None
        self._summary_api_base: Optional[str] = None
        self._summary_model_name = os.environ.get("MODEL_NAME", "").strip()
        self._summary_timeout_seconds = float(
            os.getenv("LLM_TIMEOUT_SECONDS", str(DEFAULT_LLM_TIMEOUT_SECONDS))
        )
        self._summary_temperature = float(os.getenv("TEMPERATURE", str(DEFAULT_TEMPERATURE)))
        self._summary_top_p = float(os.getenv("TOP_P", str(DEFAULT_TOP_P)))
        self._summary_presence_penalty = float(os.getenv("PRESENCE_PENALTY", str(DEFAULT_PRESENCE_PENALTY)))

    def _ensure_summary_client(self) -> Optional[OpenAI]:
        if self._summary_client is not None:
            return self._summary_client
        self._summary_api_base = os.environ.get("API_BASE")
        self._summary_model_name = os.environ.get("MODEL_NAME", "").strip()
        self._summary_timeout_seconds = float(
            os.getenv("LLM_TIMEOUT_SECONDS", str(DEFAULT_LLM_TIMEOUT_SECONDS))
        )
        self._summary_temperature = float(os.getenv("TEMPERATURE", str(DEFAULT_TEMPERATURE)))
        self._summary_top_p = float(os.getenv("TOP_P", str(DEFAULT_TOP_P)))
        self._summary_presence_penalty = float(os.getenv("PRESENCE_PENALTY", str(DEFAULT_PRESENCE_PENALTY)))
        if not self._summary_api_base:
            return None
        self._summary_client = OpenAI(
            api_key=os.environ.get("API_KEY", "EMPTY"),
            base_url=self._summary_api_base,
            timeout=self._summary_timeout_seconds,
        )
        return self._summary_client

    @staticmethod
    def _remaining_budget_seconds(runtime_deadline: Optional[float]) -> Optional[float]:
        if runtime_deadline is None:
            return None
        return runtime_deadline - time.time()

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self.parse_json_args(params)
            url = params["url"]
            goal = params["goal"]
        except ValueError as exc:
            return f"[WebFetch] {exc}"
        runtime_deadline = kwargs.get("runtime_deadline")

        start_time = time.time()

        if isinstance(url, str):
            response = self.readpage_jina(url, goal, runtime_deadline=runtime_deadline)
        elif isinstance(url, list):
            response = []
            start_time = time.time()
            for one_url in url:
                remaining = self._remaining_budget_seconds(runtime_deadline)
                if remaining is not None and remaining <= 0:
                    cur_response = _webfetch_failure(
                        url=one_url,
                        goal=goal,
                        reason="Agent runtime limit reached before WebFetch could complete.",
                    )
                    response.append(cur_response)
                    continue
                if time.time() - start_time > 900:
                    cur_response = "The useful information in {url} for user goal {goal} as follows: \n\n".format(url=one_url, goal=goal)
                    cur_response += "Evidence in page: \n" + "The provided webpage content could not be accessed. Please check the URL or file format." + "\n\n"
                    cur_response += "Summary: \n" + "The webpage content could not be processed, and therefore, no information is available." + "\n\n"
                else:
                    cur_response = self.readpage_jina(one_url, goal, runtime_deadline=runtime_deadline)
                response.append(cur_response)
            response = "\n=======\n".join(response)
        else:
            return "[WebFetch] 'url' must be a string or a list of strings."

        if visit_debug_enabled():
            print(f"Summary Length {len(response)}")
        return response.strip()

    def call_server(self, msgs, max_retries=2, runtime_deadline: Optional[float] = None):
        client = self._ensure_summary_client()
        if client is None or not self._summary_api_base:
            return "[WebFetch] Summary model error: API_BASE is not set."
        if not self._summary_model_name:
            return "[WebFetch] Summary model error: MODEL_NAME is not set."
        last_error = "unknown summary-model error"
        for attempt in range(max_retries):
            remaining = self._remaining_budget_seconds(runtime_deadline)
            if remaining is not None and remaining <= 0:
                return "[WebFetch] Summary model error: agent runtime limit reached."
            try:
                request_client = (
                    client.with_options(timeout=min(self._summary_timeout_seconds, max(remaining, 0.001)))
                    if remaining is not None
                    else client
                )
                request_kwargs = {
                    "model": self._summary_model_name,
                    "messages": msgs,
                }
                apply_sampling_params(
                    request_kwargs,
                    model_name=self._summary_model_name,
                    temperature=self._summary_temperature,
                    top_p=self._summary_top_p,
                    presence_penalty=self._summary_presence_penalty,
                )
                chat_response = request_client.chat.completions.create(**request_kwargs)
                content = chat_response.choices[0].message.content
                if content:
                    return content
                last_error = "empty response from summary model"
            except (APIError, APIConnectionError, APITimeoutError) as exc:
                last_error = str(exc)
                if attempt == (max_retries - 1):
                    return f"[WebFetch] Summary model error: {last_error}"

        return f"[WebFetch] Summary model error: {last_error}"

    def jina_readpage(self, url: str, runtime_deadline: Optional[float] = None) -> str:
        max_retries = 3
        timeout = 50
        jina_api_key = os.getenv("JINA_API_KEYS", "").strip()
        if not jina_api_key:
            return "[WebFetch] JINA_API_KEYS is not set."

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

    def readpage_jina(self, url: str, goal: str, runtime_deadline: Optional[float] = None) -> str:
        summary_page_func = self.call_server
        max_retries = int(os.getenv("LLM_MAX_RETRIES", str(DEFAULT_LLM_MAX_RETRIES)))

        content = self.html_readpage_jina(url, runtime_deadline=runtime_deadline)

        if content and not content.startswith("[WebFetch] Failed to read page:") and content != "[WebFetch] Empty content." and not content.startswith("[document_parser]"):
            content = truncate_to_tokens(content, max_tokens=95000)
            messages = [{"role": "user", "content": EXTRACTOR_PROMPT.format(webpage_content=content, goal=goal)}]
            raw = summary_page_func(messages, max_retries=max_retries, runtime_deadline=runtime_deadline)
            summary_retries = 3
            while len(raw) < 10 and summary_retries >= 0:
                remaining = self._remaining_budget_seconds(runtime_deadline)
                if remaining is not None and remaining <= 0:
                    return _webfetch_failure(
                        url=url,
                        goal=goal,
                        reason="Agent runtime limit reached before WebFetch could complete.",
                    )
                truncate_length = int(0.7 * len(content)) if summary_retries > 0 else 25000
                status_msg = (
                    f"[WebFetch] Summary url[{url}] "
                    f"attempt {3 - summary_retries + 1}/3, "
                    f"content length: {len(content)}, "
                    f"truncating to {truncate_length} chars"
                ) if summary_retries > 0 else (
                    f"[WebFetch] Summary url[{url}] failed after 3 attempts, "
                    f"final truncation to 25000 chars"
                )
                if visit_debug_enabled():
                    print(status_msg)
                content = content[:truncate_length]
                extraction_prompt = EXTRACTOR_PROMPT.format(
                    webpage_content=content,
                    goal=goal,
                )
                messages = [{"role": "user", "content": extraction_prompt}]
                raw = summary_page_func(messages, max_retries=max_retries, runtime_deadline=runtime_deadline)
                summary_retries -= 1

            parse_retry_times = 0
            parsed = _parse_extractor_payload(raw)
            while parse_retry_times < 3:
                if parsed is not None:
                    break
                remaining = self._remaining_budget_seconds(runtime_deadline)
                if remaining is not None and remaining <= 0:
                    return _webfetch_failure(
                        url=url,
                        goal=goal,
                        reason="Agent runtime limit reached before WebFetch could complete.",
                    )
                raw = summary_page_func(messages, max_retries=max_retries, runtime_deadline=runtime_deadline)
                parsed = _parse_extractor_payload(raw)
                parse_retry_times += 1

            if parsed is None:
                reason = "The webpage content was fetched, but the summary model did not return the required evidence and summary fields."
                if isinstance(raw, str) and raw.startswith("[WebFetch] Summary model error:"):
                    reason = raw
                useful_information = _webfetch_failure(
                    url=url,
                    goal=goal,
                    reason=reason,
                )
            else:
                evidence, summary = parsed
                useful_information = "The useful information in {url} for user goal {goal} as follows: \n\n".format(url=url, goal=goal)
                useful_information += "Evidence in page: \n" + evidence + "\n\n"
                useful_information += "Summary: \n" + summary + "\n\n"

            if len(useful_information) < 10 and summary_retries < 0:
                if visit_debug_enabled():
                    print("[WebFetch] Could not generate valid summary after maximum retries")
                useful_information = "[WebFetch] Failed to read page."

            return useful_information

        return _webfetch_failure(
            url=url,
            goal=goal,
            reason="The provided webpage content could not be accessed. Please check the URL or file format.",
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
    fetch_parser.add_argument("goal")

    args = parser.parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env")

    if args.tool == "search":
        result = WebSearch().call({"query": [" ".join(args.query)]})
    elif args.tool == "scholar":
        result = ScholarSearch().call({"query": [" ".join(args.query)]})
    else:
        result = WebFetch().call({"url": args.url, "goal": args.goal})
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
