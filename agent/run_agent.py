#!/usr/bin/env python3
"""A support agent that wakes when a customer writes.

The loop is the interesting part. Rather than polling on a timer - which makes
every customer wait for the timer - `wait_for_new_ticket` blocks on the server
until a message arrives, so the agent starts within about two seconds of
someone pressing send and is idle the rest of the time.

This is a reference implementation with no dependencies beyond `httpx`, so it
can be read as documentation of the protocol. A real deployment would drive an
LLM between steps 3 and 4; where that goes is marked.

    MMD_MCP_URL=https://mmd-ai.ir/mcp \\
    MMD_MCP_TOKEN="$(sudo cat /etc/mmd/mcp.key)" \\
        python3 agent/run_agent.py
"""
import json
import os
import sys
import time

import httpx

URL = os.environ.get("MMD_MCP_URL", "https://mmd-ai.ir/mcp")
TOKEN = os.environ.get("MMD_MCP_TOKEN", "")
_id = 0


def rpc(method: str, params: dict | None = None, timeout: float = 90.0):
    global _id
    _id += 1
    r = httpx.post(URL, timeout=timeout,
                   headers={"Authorization": f"Bearer {TOKEN}",
                            "Content-Type": "application/json"},
                   json={"jsonrpc": "2.0", "id": _id, "method": method,
                         "params": params or {}})
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(body["error"])
    return body["result"]


def tool(name: str, args: dict | None = None, timeout: float = 90.0):
    """Tool results arrive as text; the payload is JSON inside it."""
    result = rpc("tools/call", {"name": name, "arguments": args or {}}, timeout)
    text = result["content"][0]["text"]
    if result.get("isError"):
        raise RuntimeError(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def main() -> int:
    if not TOKEN:
        print("set MMD_MCP_TOKEN", file=sys.stderr)
        return 2

    rpc("initialize")
    guide = tool("platform_guide")
    print(f"connected; product guide is {len(guide['guide'])} characters")

    # Baseline, so the first wait does not fire on a message from last week.
    since = tool("wait_for_new_ticket")["latest_message_id"]
    print(f"watching from message {since}")

    handled: set[int] = set()
    while True:
        try:
            woke = tool("wait_for_new_ticket",
                        {"since_id": since, "timeout_seconds": 60},
                        timeout=120)
        except (httpx.HTTPError, RuntimeError) as exc:
            # A dropped connection is not a reason to stop working; back off
            # briefly and pick the watch back up.
            print(f"wait failed ({exc}); retrying", file=sys.stderr)
            time.sleep(5)
            continue

        since = woke.get("latest_message_id") or since
        if not woke.get("new_activity"):
            continue

        for tk in tool("list_open_tickets")["tickets"]:
            if tk["ticket_id"] in handled:
                continue
            if tk["messages"] and tk["messages"][-1]["from"] != "customer":
                continue                       # the last word is already ours

            print(f"#{tk['ticket_id']} from {tk['customer']['username']}: "
                  f"{tk['subject']}")

            # ---- where an LLM goes -------------------------------------
            # Give it: agent/system-prompt.md as the system prompt, `guide`,
            # this ticket, and optionally
            #   tool("export_customer_data", {"username": ...})
            # Then take its reply and chosen status and post them:
            #
            #   tool("reply_to_ticket", {"ticket_id": tk["ticket_id"],
            #                            "body": reply,
            #                            "status": status})
            #
            # This reference build stops here rather than answering, so it can
            # be run safely against production to watch the loop work.
            # ------------------------------------------------------------
            handled.add(tk["ticket_id"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
