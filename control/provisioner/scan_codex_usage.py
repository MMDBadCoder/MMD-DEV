#!/usr/bin/env python3
"""Count Codex token usage inside a workspace.

The sibling of scan_ai_usage.py, and deliberately the same contract: it runs
INSIDE the container, prints a compact JSON summary, and reports CUMULATIVE
totals per session rather than a delta. The caller keeps the high-water mark and
charges the difference, which makes this stateless, safe to re-run, and correct
when a session file is appended to between passes.

    {"sessions": {"<session-id>": {"<model>": {"input": N, "cache_read": N,
                                               "cache_write_5m": N,
                                               "cache_write_1h": N,
                                               "output": N}}},
     "files": N, "messages": N}

The category names are Claude's, not Codex's, on purpose: the billing side
prices a dict of those five keys and knows nothing about which agent produced
it. Codex's field names are mapped onto them here, once, rather than teaching
the pricing code a second vocabulary.

The one thing that must not be got wrong
----------------------------------------
Codex writes TWO usage figures in every `token_count` record:

    "total_token_usage": {...}   - cumulative for the whole session
    "last_token_usage":  {...}   - the most recent turn alone

and it writes one such record per turn. Summing `last_token_usage` across
records would be correct only if no record were ever missed or re-read; summing
`total_token_usage` would multiply the session by its number of turns. Neither
is what we want. This takes the LAST `total_token_usage` in the file - the
session's own running total, by definition already de-duplicated - which is the
same shape the Claude scanner reports and the same thing the mark arithmetic
expects.

`reasoning_output_tokens` is deliberately NOT added to output: it is a subset of
`output_tokens`, not an addition to it, and adding it would bill the reasoning
twice.
"""
from __future__ import annotations

import json
import os
import sys

CATEGORIES = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")

# Codex writes the sessions under ~/.codex/sessions/<year>/<month>/<day>/.
DEFAULT_ROOT = os.path.expanduser("~/.codex/sessions")


def _blank() -> dict[str, int]:
    return dict.fromkeys(CATEGORIES, 0)


def _map_usage(u: dict) -> dict[str, int]:
    """Codex's field names onto the five categories billing prices.

    `input_tokens` already INCLUDES `cached_input_tokens` in Codex's accounting,
    so the uncached remainder is the difference. Getting this wrong would bill
    cache reads at the full input rate, which is the expensive direction.
    """
    def n(key: str) -> int:
        v = u.get(key)
        return int(v) if isinstance(v, (int, float)) and v > 0 else 0

    cached = n("cached_input_tokens")
    total_in = n("input_tokens")
    return {
        "input": max(total_in - cached, 0),
        "cache_read": cached,
        "cache_write_5m": n("cache_write_input_tokens"),
        "cache_write_1h": 0,
        # NOT plus reasoning_output_tokens: that is a subset of output_tokens,
        # and adding it bills the same reasoning twice.
        "output": n("output_tokens"),
    }


def scan_file(path: str) -> tuple[dict[str, dict[str, int]], int]:
    """The session's own cumulative total, keyed by the model that produced it.

    Returns ({model: categories}, turns). A file with no `token_count` record
    has produced nothing billable yet, which is not an error.
    """
    model = ""
    latest: dict | None = None
    turns = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                payload = rec.get("payload")
                if not isinstance(payload, dict):
                    continue
                # The model is carried on other records, so the last one seen
                # before the totals is the one that produced them.
                m = payload.get("model")
                if isinstance(m, str) and m:
                    model = m
                if payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                total = info.get("total_token_usage")
                if isinstance(total, dict):
                    latest = total
                    turns += 1
    except OSError:
        return {}, 0

    if latest is None or not model:
        return {}, turns
    return {model: _map_usage(latest)}, turns


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ROOT
    root = os.path.expanduser(root)
    sessions: dict[str, dict[str, dict[str, int]]] = {}
    files = 0
    messages = 0

    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, name)
            per_model, turns = scan_file(path)
            files += 1
            messages += turns
            if not per_model:
                continue
            # The session id is in the filename after the timestamp; the file
            # path is unique per session either way, so it is a safe key even
            # if that naming ever changes.
            key = os.path.splitext(name)[0]
            bucket = sessions.setdefault(key, {})
            for model, cats in per_model.items():
                acc = bucket.setdefault(model, _blank())
                for c in CATEGORIES:
                    acc[c] += cats.get(c, 0)

    json.dump({"sessions": sessions, "files": files, "messages": messages},
              sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
