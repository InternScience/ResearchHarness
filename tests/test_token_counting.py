#!/usr/bin/env python3

from __future__ import annotations

import copy
import json

from agent_base.react_agent import MultiTurnReactAgent


def test_count_tokens_treats_special_token_literals_as_plain_text() -> None:
    agent = MultiTurnReactAgent(
        function_list=[],
        llm={
            "model": "fake-model",
            "generate_cfg": {
                "max_input_tokens": 10000,
                "max_retries": 1,
                "temperature": 0.0,
                "top_p": 1.0,
                "presence_penalty": 0.0,
            },
        },
    )
    messages = [
        {"role": "tool", "content": "example text <|endoftext|> from a PDF"},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "hello <|endoftext|>"}],
            "tool_calls": [
                {
                    "type": "function",
                    "function": {
                        "name": "Echo",
                        "arguments": json.dumps({"x": "<|endoftext|>"}),
                    },
                }
            ],
            "reasoning_content": {"note": "<|endoftext|>"},
        },
    ]
    original_messages = copy.deepcopy(messages)

    assert agent.count_tokens(messages, include_tool_schema=False) > 0
    assert messages == original_messages
