"""Tool schema exposed to the LLM during discovery. Deliberately small and closed:
the agent can only click/type/select on an element it was just shown (by numeric
index from the last perception snapshot), extract a labeled output, declare the goal
finished, or ask for human help. It cannot navigate to an arbitrary URL, run
JavaScript, or otherwise step outside src/policy/allowlist.yaml's action types --
this is what "the agent must not act outside [the allowlist]" (3.4) looks like in
practice for the discovery side."""
from __future__ import annotations

TOOLS = [
    {
        "name": "click_element",
        "description": "Click the interactive element with the given index from the most recent page snapshot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "The element's index, e.g. from '[4] button \"Search\"'."},
                "reasoning": {"type": "string", "description": "Why this click moves toward the goal."},
            },
            "required": ["index", "reasoning"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text into a textbox element with the given index (clears any existing value first).",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "integer"},
                "text": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["index", "text", "reasoning"],
        },
    },
    {
        "name": "select_option",
        "description": "Choose one of the listed 'options' values for a combobox/select element with the given index.",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "integer"},
                "value": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["index", "value", "reasoning"],
        },
    },
    {
        "name": "extract_output",
        "description": "Record a piece of data read from the current page as a named output of this capability.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "value": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["name", "value", "reasoning"],
        },
    },
    {
        "name": "finish_success",
        "description": "Declare the goal achieved. Only call this once the current page's state genuinely satisfies the goal.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "checkpoint_text": {
                    "type": "string",
                    "description": "A short, distinctive piece of text visible on this final page that proves success "
                    "(used as the replay checkpoint assertion).",
                },
            },
            "required": ["summary", "checkpoint_text"],
        },
    },
    {
        "name": "escalate",
        "description": "Call this if you are stuck: the goal cannot be safely or confidently continued "
        "(e.g. an unexpected state, ambiguous UI, or an action that looks irreversible/risky and was "
        "not clearly part of the goal). Explain why.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]
