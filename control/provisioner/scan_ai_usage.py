#!/usr/bin/env python3
"""Count Claude Code token usage inside a workspace.

Runs INSIDE the container and prints a compact JSON summary. The session logs
are tens of megabytes; copying them to the host to be parsed there would be the
same mistake the zip download made, so only totals cross the boundary.

Reports CUMULATIVE totals per session, not a delta. The caller keeps the
high-water mark and charges the difference - which makes this stateless, safe to
re-run, and correct when a session file is appended to between passes.

    {"sessions": {"<session-uuid>": {"<model>": {"input": N, "cache_write_5m": N,
                                                 "cache_write_1h": N,
                                                 "cache_read": N, "output": N}}},
     "files": N, "messages": N}

The one thing that must not be got wrong
----------------------------------------
Claude Code writes an assistant response to the log ONCE PER CONTENT BLOCK -
text, thinking, each tool_use - and every one of those lines repeats the same
`message.usage` totals. Summing the lines overcounts: measured on a real
session, 2,317 assistant records for 1,122 actual messages, inflating output
tokens by 2.37x. Usage is therefore attributed once per `message.id`.
"""
from __future__ import annotations

import json
import os
import sys

CATEGORIES = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")

# Records Claude Code writes for locally-generated content. They carry no usage
# that Anthropic charged for, and must not be billed.
NON_BILLABLE_MODELS = {"<synthetic>", "", None}


def _blank() -> dict[str, int]:
    return dict.fromkeys(CATEGORIES, 0)


def _usage_of(rec: dict) -> tuple[str, dict[str, int]] | None:
    """The billable usage of one assistant record, or None."""
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    model = msg.get("model")
    if model in NON_BILLABLE_MODELS:
        return None
    u = msg.get("usage")
    if not isinstance(u, dict):
        return None

    # The two cache-write durations are priced differently (1.25x vs 2x base
    # input), so they are kept apart. Older records carry only the combined
    # `cache_creation_input_tokens`; those are treated as 5-minute writes, which
    # is the cheaper of the two and therefore the one that cannot overcharge.
    detail = u.get("cache_creation")
    if isinstance(detail, dict):
        w5 = int(detail.get("ephemeral_5m_input_tokens") or 0)
        w1h = int(detail.get("ephemeral_1h_input_tokens") or 0)
    else:
        w5, w1h = int(u.get("cache_creation_input_tokens") or 0), 0

    return model, {
        "input": int(u.get("input_tokens") or 0),
        "cache_write_5m": w5,
        "cache_write_1h": w1h,
        "cache_read": int(u.get("cache_read_input_tokens") or 0),
        "output": int(u.get("output_tokens") or 0),
    }


def scan_file(path: str) -> tuple[dict[str, dict[str, int]], int]:
    """Per-model totals for one session file, and how many messages were counted."""
    per_model: dict[str, dict[str, int]] = {}
    seen: set[str] = set()

    with open(path, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or '"assistant"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                # A partially-written final line while Claude Code is running.
                # Skipping it is right: the next pass sees it complete.
                continue
            if rec.get("type") != "assistant":
                continue

            got = _usage_of(rec)
            if got is None:
                continue
            model, usage = got

            # One charge per API response, however many lines it occupies.
            mid = (rec.get("message") or {}).get("id")
            if not mid or mid in seen:
                continue
            seen.add(mid)

            bucket = per_model.setdefault(model, _blank())
            for k in CATEGORIES:
                bucket[k] += usage[k]

    return per_model, len(seen)


def main() -> int:
    root = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/.claude/projects")
    sessions: dict[str, dict[str, dict[str, int]]] = {}
    files = messages = 0

    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            # The file stem is the session UUID; it is the identity the caller
            # keys its high-water mark on.
            session = name[: -len(".jsonl")]
            try:
                per_model, n = scan_file(os.path.join(dirpath, name))
            except OSError:
                continue
            files += 1
            messages += n
            if not per_model:
                continue
            into = sessions.setdefault(session, {})
            for model, usage in per_model.items():
                acc = into.setdefault(model, _blank())
                for k in CATEGORIES:
                    acc[k] += usage[k]

    json.dump({"sessions": sessions, "files": files, "messages": messages},
              sys.stdout, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
