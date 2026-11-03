"""Shared manual tool-use loop for the project's Claude agents.

Both `discovery.py` (read+write) and `reporting.py` (read-only) drive the
Anthropic Messages API through the same hand-written agentic loop defined here,
`run_loop`. The loop owns the request -> execute-tools -> resend cycle: it calls
the Messages API, executes any tool-use blocks the model emits via
`tools.dispatch`, feeds the results back, and repeats until the model finishes.

The two agents differ only in which tools they are handed (`allowed_tools`) and
whether they stream (`stream_cb`); the loop logic is identical, so it lives in
one place.
"""

import json

import anthropic

import tools as tools_module

MODEL = "claude-opus-5-5"
MAX_TOKENS_STREAM = 64000
MAX_TOKENS = 16000
MAX_ITERATIONS = 12


def run_loop(
    system: str,
    messages: list[dict],
    allowed_tools: list[str],
    stream_cb=None,
) -> str:
    """Run the manual Anthropic tool-use loop to completion and return final text.

    This is a stub. The implementer must follow exactly the contract below.

    Parameters
    ----------
    system:
        The system prompt string passed as the Messages API `system` field.
    messages:
        The running conversation as a list of Anthropic message dicts. The loop
        mutates this list in place (appending each assistant turn and each
        user turn carrying tool results), so callers may inspect the full
        transcript afterward. The first entry must be a `user` message.
    allowed_tools:
        The names of the tools this agent may use. The loop filters
        `tools.TOOL_SCHEMAS` down to exactly these names and passes the filtered
        list as the request `tools`. The reporting agent passes
        `READ_TOOL_NAMES`; the discovery agent passes
        `READ_TOOL_NAMES + WRITE_TOOL_NAMES`.
    stream_cb:
        Optional callable invoked with each incremental assistant text delta as
        it arrives. When provided, the loop streams every request; when None,
        the loop uses non-streaming create calls.

    Contract the implementer must follow
    ------------------------------------
    1. Construct a client with `anthropic.Anthropic()` (zero-arg; it resolves
       credentials from the environment or an `ant auth login` profile).
    2. Filter `tools.TOOL_SCHEMAS` to the entries whose `name` is in
       `allowed_tools`; this filtered list is sent as `tools` on every request.
    3. Loop, each iteration issuing one Messages API request with
       `model=MODEL`, the system prompt, the current `messages`, the filtered
       tools, and adaptive thinking defaults: pass
       `thinking={"type": "adaptive"}` and do NOT pass `budget_tokens`
       (it is rejected on this model). Use `output_config={"effort": ...}` if
       depth control is needed rather than any thinking budget.
         - When `stream_cb` is None: call `client.messages.create(...)` with
           `max_tokens=MAX_TOKENS`.
         - When `stream_cb` is provided: call `client.messages.stream(...)` with
           `max_tokens=MAX_TOKENS_STREAM`, forward each text delta to
           `stream_cb`, and obtain the completed message via
           `stream.get_final_message()`.
    4. Inspect `response.stop_reason` before acting on content:
         - `max_tokens`: raise a RuntimeError (or equivalent) reporting that the
           response was truncated; do not treat partial output as final.
         - `refusal`: raise a RuntimeError reporting the refusal, including
           `response.stop_details.category` / `.explanation` when present.
         - `end_turn`: the model is done. Append the assistant content to
           `messages`, join its `text` blocks, and return that final text.
         - `tool_use`: proceed to step 5.
    5. On `tool_use`:
         - Append the full assistant `response.content` to `messages` (preserve
           every block, including thinking and tool_use blocks, unchanged).
         - For EVERY `tool_use` block in `response.content` (there may be more
           than one in a single turn), execute it via
           `tools.dispatch(block.name, block.input)`. `block.input` is already
           parsed by the SDK into a Python dict; use it directly (do not re-parse
           a JSON string). Wrap each call in try/except: on success build a
           `tool_result` block with the JSON-serialized dict as content; on
           exception build a `tool_result` block with `is_error: True` and the
           error text as content. Each `tool_result` must carry the matching
           `tool_use_id`.
         - Return ALL `tool_result` blocks in a SINGLE `user` message appended
           to `messages` (never split them across multiple user messages, which
           suppresses parallel tool use).
         - Continue the loop to send the updated `messages` back to the model.

    Returns
    -------
    str
        The final assistant text once the model stops with `end_turn`.
    """
    client = anthropic.Anthropic()

    # Do not mutate the caller's list; work on a shallow copy.
    working = list(messages)

    tools = [t for t in tools_module.TOOL_SCHEMAS if t["name"] in allowed_tools]

    response = None

    for _ in range(MAX_ITERATIONS):
        if stream_cb is not None:
            with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS_STREAM,
                system=system,
                tools=tools,
                thinking={"type": "adaptive"},
                messages=working,
            ) as stream:
                for event in stream:
                    if event.type == "text":
                        stream_cb(event.text)
            response = stream.get_final_message()
        else:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=tools,
                thinking={"type": "adaptive"},
                messages=working,
            )

        # Preserve the full assistant turn (thinking + tool_use blocks) so the
        # tool round-trip stays valid.
        working.append({"role": "assistant", "content": response.content})

        stop_reason = response.stop_reason

        if stop_reason == "refusal":
            category = None
            stop_details = getattr(response, "stop_details", None)
            if stop_details is not None:
                category = getattr(stop_details, "category", None)
            if category is not None:
                return f"Model refused to respond (category: {category})."
            return "Model refused to respond."

        if stop_reason == "max_tokens":
            break

        if stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                result_block = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                }
                try:
                    result = tools_module.dispatch(block.name, block.input)
                    result_block["content"] = json.dumps(result)
                except Exception as exc:  # noqa: BLE001
                    result_block["is_error"] = True
                    result_block["content"] = str(exc)
                tool_results.append(result_block)
            # All tool_result blocks go back in a SINGLE user message.
            working.append({"role": "user", "content": tool_results})
            continue

        # end_turn or any other terminal stop reason.
        break

    if response is None:
        return ""

    return "".join(
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text"
    )
