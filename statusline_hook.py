#!/usr/bin/env python3
"""token-mon statusline hook for Claude Code.

Claude Code pipes a JSON status payload to the configured statusLine command on
every refresh. That payload's rate_limits carries FRACTIONAL used_percentage
values (e.g. 42.5) — finer than the integer percent the OAuth usage endpoint
returns — so token-mon uses it as its preferred precision source.

This hook does two things:
  1. saves rate_limits to ~/.claude/token-mon-ratelimits.json for token-mon
  2. prints a compact usage line so the statusline itself is useful

Install (or let token-mon's README guide you):
  "statusLine": {"type": "command", "command": "python3 /path/to/statusline_hook.py"}
"""

import json
import os
import sys
import time


def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        d = {}
    rl = d.get("rate_limits") or {}

    out_path = os.path.expanduser("~/.claude/token-mon-ratelimits.json")
    try:
        payload = {
            "ts": time.time(),
            "five_hour": rl.get("five_hour"),
            "seven_day": rl.get("seven_day"),
        }
        tmp = out_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, out_path)
    except OSError:
        pass

    def pct(win):
        v = (rl.get(win) or {}).get("used_percentage")
        return f"{v:.1f}%" if isinstance(v, (int, float)) else "–"

    model = (d.get("model") or {}).get("display_name") or ""
    parts = [p for p in (model, f"5h {pct('five_hour')}", f"wk {pct('seven_day')}") if p]
    print(" · ".join(parts))


if __name__ == "__main__":
    main()
