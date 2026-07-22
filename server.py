#!/usr/bin/env python3
"""token-mon: live local dashboard for AI-coding subscription usage
(Claude Code + OpenAI Codex).

Two local data sources, no third-party services:

  1. Transcripts (~/.claude/projects/**/*.jsonl) — exact, live, per-model token
     counts, updated as Claude Code writes them.
  2. Claude Code's own usage endpoint (api.anthropic.com/api/oauth/usage),
     called with the OAuth token already on this machine and the claude-code
     User-Agent — the same call `/usage` makes. Returns the OFFICIAL session /
     weekly / per-model utilization percentages and reset times.

The official percentages are authoritative but coarse (whole numbers). We
calibrate the exact token count against the official session % so the headline
can tick with many decimals while staying anchored to the real figure.

Plan and limits are auto-detected from ~/.claude/.credentials.json — no flags
needed. Use --no-usage to disable the endpoint (transcripts only), or --plan /
--limit to override the fallback estimate used when the endpoint is unreachable.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BLOCK_HOURS = 5
RETAIN_DAYS = 8
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CRED_PATH = Path.home() / ".claude" / ".credentials.json"
# Written by statusline_hook.py — carries FRACTIONAL used_percentage from
# Claude Code's statusline payload, finer than the integer endpoint.
STATUSLINE_DUMP = Path.home() / ".claude" / "token-mon-ratelimits.json"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_HOME = Path.home() / ".codex"
# Resolve the codex binary even under systemd, whose PATH lacks ~/.local/bin.
CODEX_BIN = shutil.which("codex")
if not CODEX_BIN:
    _codex_candidate = Path.home() / ".local" / "bin" / "codex"
    CODEX_BIN = str(_codex_candidate) if _codex_candidate.exists() else None

# Cost-equivalent weights relative to input=1, from published pricing. The
# providers meter subscription limits by cost, not raw tokens — cache reads are
# ~10% of input — so estimator math must run in these units for local usage to
# be comparable with the official percentage.
#   Anthropic (claude.com/pricing): output 5x, cache-write 1.25x, cache-read 0.1x
#   OpenAI Codex credit rates (learn.chatgpt.com/docs/pricing): output 6x, cached 0.1x
CLAUDE_W = {"in": 1.0, "out": 5.0, "cw": 1.25, "cr": 0.1}
CODEX_W = {"in": 1.0, "out": 6.0, "cw": 1.0, "cr": 0.1}

# Rough weekly-limit priors (raw tokens) for Codex, used only until local
# measurement provides a better figure. Order-of-magnitude anchors from
# openai/codex discussions #2251/#26512 and 2026 community pricing analyses
# (Plus weekly ~ single-digit millions of tokens; Pro = 5-20x Plus). These are
# deliberately conservative and clearly imprecise — measurement replaces them.
CODEX_WEEKLY_PRIOR = {"plus": 10_000_000, "pro": 50_000_000, "business": 10_000_000,
                      "team": 10_000_000, "enterprise": 50_000_000}

# Fallback estimates of tokens per 5h block (only used if the official endpoint
# is unavailable). Anthropic does not publish real token limits.
PLAN_PRESETS = {"pro": 19_000, "max5": 88_000, "max20": 220_000}
TIER_TO_PLAN = {
    "default_claude_pro": ("pro", "Pro"),
    "default_claude_max_5x": ("max5", "Max 5x"),
    "default_claude_max_20x": ("max20", "Max 20x"),
}
DEFAULT_LIMIT = 88_000

def label_model(mid):
    """Generic, whitelist-free label derived straight from whatever model id the
    tool wrote — so new/renamed models are followed automatically.
    "claude-opus-4-8" -> "Opus 4.8", "gpt-5.6-sol" -> "GPT 5.6 Sol"."""
    if not mid:
        return "unknown"
    s = re.sub(r"^claude-", "", mid)
    s = re.sub(r"-\d{8}$", "", s)          # strip a trailing YYYYMMDD build id
    parts = s.split("-")
    if len(parts) >= 2:
        head = parts[0].upper() if len(parts[0]) <= 3 else parts[0].capitalize()
        nums, tail = [], []
        for p in parts[1:]:
            if p.replace(".", "").isdigit() and not tail:
                nums.append(p)
            else:
                tail.append(p.capitalize())
        if nums:
            out = head + " " + ".".join(nums)
            if tail:
                out += " " + " ".join(tail)
            return out
    return s.capitalize()


def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


def detect_version():
    try:
        out = subprocess.run(
            ["claude", "--version"], capture_output=True, timeout=4, text=True
        ).stdout
        m = re.search(r"(\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "2.1.204"


def read_credentials():
    try:
        return json.load(open(CRED_PATH)).get("claudeAiOauth", {})
    except Exception:
        return {}


# --------------------------------------------------------------------------
# Official usage poller
# --------------------------------------------------------------------------
class OfficialUsage:
    """Polls Claude Code's usage endpoint on a slow cadence (it rate-limits
    aggressively) and calibrates a token-equivalent session limit."""

    def __init__(self, scanner, version, interval, enabled):
        self.scanner = scanner
        self.version = version
        self.interval = interval
        self.enabled = enabled
        self.lock = threading.Lock()
        self.data = {"ok": False, "error": "not polled yet"}
        self.last_util = None
        self.session_limit = None
        self.window_start = None  # datetime
        self.last_wutil = None
        self.weekly_limit = None
        self.weekly_window_start = None  # datetime

    def _fetch(self):
        token = read_credentials().get("accessToken")
        if not token:
            raise RuntimeError("no OAuth token in credentials")
        req = urllib.request.Request(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": f"claude-code/{self.version}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())

    @staticmethod
    def _statusline_overlay(session, weekly):
        """Claude Code's statusline payload (saved by statusline_hook.py) has
        FRACTIONAL used_percentage — overlay it when fresh so calibration and
        estimation run at decimal precision instead of whole percents."""
        try:
            d = json.load(open(STATUSLINE_DUMP))
        except Exception:
            return
        if time.time() - (d.get("ts") or 0) > 600:
            return
        for blk, win in ((session, d.get("five_hour")), (weekly, d.get("seven_day"))):
            if not isinstance(win, dict):
                continue
            v = win.get("used_percentage")
            if isinstance(v, (int, float)):
                blk["pct"] = float(v)
                blk["precise"] = True
            r = win.get("resets_at")
            if isinstance(r, (int, float)) and r > 0:
                blk["resets_at"] = datetime.fromtimestamp(r, timezone.utc).isoformat()

    def poll(self):
        try:
            j = self._fetch()
        except urllib.error.HTTPError as e:
            with self.lock:
                self.data = {"ok": False, "error": f"HTTP {e.code}", "status": e.code}
            return
        except Exception as e:
            with self.lock:
                self.data = {"ok": False, "error": str(e)[:120]}
            return

        fh = j.get("five_hour") or {}
        sd = j.get("seven_day") or {}
        sev = {lim.get("kind"): lim.get("severity") for lim in (j.get("limits") or [])}

        session = {
            "pct": fh.get("utilization"),
            "resets_at": fh.get("resets_at"),
            "severity": sev.get("session", "normal"),
        }
        weekly = {
            "pct": sd.get("utilization"),
            "resets_at": sd.get("resets_at"),
            "severity": sev.get("weekly_all", "normal"),
        }
        self._statusline_overlay(session, weekly)
        models = []
        for lim in j.get("limits") or []:
            if lim.get("kind") == "weekly_scoped":
                sc = (lim.get("scope") or {}).get("model") or {}
                models.append(
                    {
                        "model": sc.get("display_name") or "scoped",
                        "pct": lim.get("percent"),
                        "resets_at": lim.get("resets_at"),
                        "severity": lim.get("severity", "normal"),
                    }
                )

        # Calibrate a token-equivalent session limit, anchored to the official %.
        util = session["pct"]
        resets = parse_ts(session["resets_at"])
        win_start = resets - timedelta(hours=BLOCK_HOURS) if resets else None
        if win_start:
            self.window_start = win_start
        if util and util >= 1 and win_start:
            with self.scanner.lock:
                tok_now = sum(
                    ev_total(e) for e in self.scanner.events if e["ts"] >= win_start
                )
            # Re-anchor only when the official integer % changes, so the derived
            # percentage climbs monotonically on each plateau instead of sawtoothing.
            if self.last_util != round(util) or self.session_limit is None:
                self.session_limit = tok_now / (util / 100.0)
                self.last_util = round(util)

        # Same calibration for the 7-day window, aligned to the official reset.
        wutil = weekly["pct"]
        wresets = parse_ts(weekly["resets_at"])
        wwin_start = wresets - timedelta(days=7) if wresets else None
        if wwin_start:
            self.weekly_window_start = wwin_start
        if wutil and wutil >= 1 and wwin_start:
            with self.scanner.lock:
                wtok_now = sum(
                    ev_total(e) for e in self.scanner.events if e["ts"] >= wwin_start
                )
            if self.last_wutil != round(wutil) or self.weekly_limit is None:
                self.weekly_limit = wtok_now / (wutil / 100.0)
                self.last_wutil = round(wutil)

        extra = j.get("extra_usage") or {}
        with self.lock:
            self.data = {
                "ok": True,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "session": session,
                "weekly_all": weekly,
                "weekly_models": models,
                "extra_usage": extra if extra.get("is_enabled") else None,
                "session_limit_calibrated": self.session_limit,
                "session_window_start": win_start.isoformat() if win_start else None,
                "weekly_limit_calibrated": self.weekly_limit,
                "weekly_window_start": wwin_start.isoformat() if wwin_start else None,
            }

    def snapshot(self):
        with self.lock:
            d = dict(self.data)
        d["session_limit_calibrated"] = self.session_limit
        d["weekly_limit_calibrated"] = self.weekly_limit
        return d

    def run(self):
        if not self.enabled:
            with self.lock:
                self.data = {"ok": False, "error": "disabled (--no-usage)"}
            return
        backoff = self.interval
        while True:
            self.poll()
            ok = self.data.get("ok")
            backoff = self.interval if ok else min(backoff * 2, 900)
            time.sleep(backoff)


# --------------------------------------------------------------------------
# Transcript scanner (exact per-model tokens)
# --------------------------------------------------------------------------
class UsageScanner:
    def __init__(self, root: Path):
        self.root = root
        self.offsets = {}
        self.seen = set()
        self.events = []
        self.lock = threading.Lock()
        self.files_tracked = 0

    def scan(self):
        new_events = []
        try:
            paths = list(self.root.glob("*/*.jsonl"))
        except OSError:
            paths = []
        self.files_tracked = len(paths)
        for path in paths:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            offset = self.offsets.get(path, 0)
            if size == offset:
                continue
            if size < offset:
                offset = 0
            try:
                with open(path, "rb") as f:
                    f.seek(offset)
                    chunk = f.read()
            except OSError:
                continue
            last_nl = chunk.rfind(b"\n")
            if last_nl == -1:
                continue
            self.offsets[path] = offset + last_nl + 1
            for raw in chunk[: last_nl + 1].split(b"\n"):
                if not raw.strip():
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ev = self._extract(entry)
                if ev:
                    new_events.append(ev)
        if new_events:
            cutoff = datetime.now(timezone.utc) - timedelta(days=RETAIN_DAYS)
            with self.lock:
                self.events.extend(new_events)
                self.events.sort(key=lambda e: e["ts"])
                self.events = [e for e in self.events if e["ts"] >= cutoff]
        return len(new_events)

    def _extract(self, entry):
        msg = entry.get("message")
        if not isinstance(msg, dict):
            return None
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            return None
        model = msg.get("model") or ""
        if model == "<synthetic>":
            return None
        key = (msg.get("id"), entry.get("requestId"))
        if key != (None, None):
            if key in self.seen:
                return None
            self.seen.add(key)
        ts = parse_ts(entry.get("timestamp"))
        if ts is None:
            return None
        return {
            "ts": ts,
            "model": model,
            "in": usage.get("input_tokens") or 0,
            "out": usage.get("output_tokens") or 0,
            "cw": usage.get("cache_creation_input_tokens") or 0,
            "cr": usage.get("cache_read_input_tokens") or 0,
        }


class CodexScanner:
    """Tails Codex CLI session rollouts (~/.codex/sessions/**/*.jsonl) for exact
    per-turn token usage. Events reuse the Claude field names so the same
    aggregation helpers work: in=non-cached input, cr=cached input, out=output
    (+reasoning), cw=0."""

    def __init__(self, roots):
        self.roots = list(roots)
        self.offsets = {}
        self.models = {}     # path -> current model for subsequent turns
        self.events = []
        self.rate = None     # freshest rate_limits snapshot seen in a rollout
        self.zst_done = set()  # cold .jsonl.zst files already ingested
        self.lock = threading.Lock()
        self.files_tracked = 0

    def scan(self):
        new_events = []
        paths = []
        for root in self.roots:
            try:
                paths.extend(root.glob("**/*.jsonl"))
                # Codex background-compresses cold rollouts; ingest those once.
                for zp in root.glob("**/*.jsonl.zst"):
                    if zp not in self.zst_done:
                        new_events.extend(self._read_zst(zp))
                        self.zst_done.add(zp)
            except OSError:
                pass
        self.files_tracked = len(paths) + len(self.zst_done)
        for path in paths:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            offset = self.offsets.get(path, 0)
            if size == offset:
                continue
            if size < offset:
                offset = 0
            try:
                with open(path, "rb") as f:
                    f.seek(offset)
                    chunk = f.read()
            except OSError:
                continue
            last_nl = chunk.rfind(b"\n")
            if last_nl == -1:
                continue
            self.offsets[path] = offset + last_nl + 1
            for raw in chunk[: last_nl + 1].split(b"\n"):
                if not raw.strip():
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ev = self._extract(path, entry)
                if ev:
                    new_events.append(ev)
        if new_events:
            cutoff = datetime.now(timezone.utc) - timedelta(days=RETAIN_DAYS)
            with self.lock:
                self.events.extend(new_events)
                self.events.sort(key=lambda e: e["ts"])
                self.events = [e for e in self.events if e["ts"] >= cutoff]
        return len(new_events)

    def _read_zst(self, path):
        """Ingest a compressed cold rollout in one pass (they're immutable)."""
        try:
            import zstandard
        except ImportError:
            return []   # optional dependency; cold files are usually outside windows
        events = []
        try:
            with open(path, "rb") as f:
                data = zstandard.ZstdDecompressor().stream_reader(f).read()
            for raw in data.split(b"\n"):
                if not raw.strip():
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ev = self._extract(path, entry)
                if ev:
                    events.append(ev)
        except Exception:
            pass
        return events

    def _extract(self, path, entry):
        payload = entry.get("payload") or {}
        ptype = payload.get("type") or entry.get("type")
        model = payload.get("model") or (payload.get("info") or {}).get("model")
        if model:
            self.models[path] = model
        if ptype != "token_count":
            return None
        ts = parse_ts(entry.get("timestamp")) or datetime.now(timezone.utc)
        # Rollouts embed the freshest OFFICIAL rate-limit snapshot per turn,
        # with FRACTIONAL used_percent — better than the integer endpoint.
        rl = payload.get("rate_limits") or {}
        prim = rl.get("primary") or {}
        if prim.get("used_percent") is not None:
            snap = {"ts": ts, "pct": float(prim["used_percent"]),
                    "window_seconds": (prim.get("window_minutes") or 0) * 60}
            if not self.rate or ts >= self.rate["ts"]:
                self.rate = snap
        info = payload.get("info") or payload
        last = info.get("last_token_usage") or payload.get("last_token_usage")
        if not isinstance(last, dict):
            return None
        inp = last.get("input_tokens") or 0
        cached = last.get("cached_input_tokens") or 0
        out = (last.get("output_tokens") or 0) + (last.get("reasoning_output_tokens") or 0)
        if inp + out == 0:
            return None
        return {
            "ts": ts,
            "model": self.models.get(path) or "codex",
            "in": max(0, inp - cached),
            "out": out,
            "cw": last.get("cache_write_input_tokens") or 0,
            "cr": cached,
        }


def codex_window_name(seconds):
    if not seconds:
        return "usage"
    if seconds >= 6 * 86400:
        return "weekly"
    if seconds >= 86400:
        return f"{seconds // 86400}-day"
    return f"{seconds // 3600}-hour"


class CodexUsage:
    """Official Codex account usage, actively probed.

    Primary source: `codex app-server` JSON-RPC (`account/rateLimits/read`) —
    asks Codex itself, burns no tokens, returns the authoritative multi-bucket
    view, and lets Codex refresh its own OAuth token so this keeps working even
    if the user never runs Codex on this machine. Fallback: OpenAI's usage
    endpoint with the token from ~/.codex/auth.json (same data `/status` shows).
    """

    def __init__(self, interval, enabled):
        self.interval = interval
        self.enabled = enabled
        self.lock = threading.Lock()
        self.data = {"ok": False, "error": "not polled yet"}

    def _fetch_probe(self):
        proc = subprocess.Popen(
            [CODEX_BIN, "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )
        try:
            def send(o):
                proc.stdin.write(json.dumps(o) + "\n")
                proc.stdin.flush()

            def read_id(rid, timeout=25):
                end = time.time() + timeout
                while time.time() < end:
                    line = proc.stdout.readline()
                    if not line:
                        return None
                    try:
                        m = json.loads(line)
                    except ValueError:
                        continue
                    if m.get("id") == rid:
                        return m
                return None

            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"clientInfo": {"name": "token-mon", "title": "token-mon", "version": "1.0"}}})
            if not read_id(1):
                raise RuntimeError("app-server: no initialize response")
            send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
            send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}})
            m = read_id(2)
            if not m or "result" not in m:
                raise RuntimeError("app-server: no rateLimits response")
            return m["result"]
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    @staticmethod
    def _window_probe(w):
        if not isinstance(w, dict) or w.get("usedPercent") is None:
            return None
        mins = w.get("windowDurationMins")
        secs = mins * 60 if mins else None
        reset = w.get("resetsAt")
        return {
            "name": codex_window_name(secs),
            "seconds": secs,
            "pct": w.get("usedPercent"),
            "resets_at": datetime.fromtimestamp(reset, timezone.utc).isoformat() if reset else None,
        }

    def _poll_probe(self):
        res = self._fetch_probe()
        rl = res.get("rateLimits") or {}
        additional = []
        for lid, snap in (res.get("rateLimitsByLimitId") or {}).items():
            if not isinstance(snap, dict) or lid == rl.get("limitId"):
                continue
            win = self._window_probe(snap.get("primary"))
            if win:
                additional.append({**win, "name": snap.get("limitName") or lid})
        with self.lock:
            self.data = {
                "ok": True,
                "source": "codex app-server",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "plan": (rl.get("planType") or "?").capitalize(),
                "primary": self._window_probe(rl.get("primary")),
                "secondary": self._window_probe(rl.get("secondary")),
                "additional": additional,
            }

    def _fetch(self):
        auth = json.load(open(CODEX_HOME / "auth.json"))
        tokens = auth.get("tokens") or {}
        tok, acct = tokens.get("access_token"), tokens.get("account_id")
        if not tok:
            raise RuntimeError("no Codex OAuth token")
        req = urllib.request.Request(
            CODEX_USAGE_URL,
            headers={
                "Authorization": f"Bearer {tok}",
                "chatgpt-account-id": acct or "",
                "User-Agent": "codex-cli",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())

    @staticmethod
    def _window(w):
        if not isinstance(w, dict):
            return None
        secs = w.get("limit_window_seconds")
        reset = w.get("reset_at")
        return {
            "name": codex_window_name(secs),
            "seconds": secs,
            "pct": w.get("used_percent"),
            "resets_at": datetime.fromtimestamp(reset, timezone.utc).isoformat() if reset else None,
        }

    def poll(self):
        probe_err = None
        if CODEX_BIN:
            try:
                self._poll_probe()
                return
            except Exception as e:
                probe_err = str(e)[:80]
        try:
            j = self._fetch()
        except FileNotFoundError:
            with self.lock:
                self.data = {"ok": False, "error": "codex not installed"}
            return
        except urllib.error.HTTPError as e:
            with self.lock:
                self.data = {"ok": False, "error": f"probe: {probe_err} / HTTP {e.code}"}
            return
        except Exception as e:
            with self.lock:
                self.data = {"ok": False, "error": f"probe: {probe_err} / {str(e)[:80]}"}
            return
        rl = j.get("rate_limit") or {}
        additional = []
        for a in j.get("additional_rate_limits") or []:
            win = self._window((a.get("rate_limit") or {}).get("primary_window"))
            if win:
                additional.append({**win, "name": a.get("limit_name") or "limit"})
        with self.lock:
            self.data = {
                "ok": True,
                "plan": (j.get("plan_type") or "?").capitalize(),
                "primary": self._window(rl.get("primary_window")),
                "secondary": self._window(rl.get("secondary_window")),
                "additional": additional,
            }

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def run(self):
        if not self.enabled:
            with self.lock:
                self.data = {"ok": False, "error": "disabled"}
            return
        backoff = self.interval
        while True:
            self.poll()
            backoff = self.interval if self.data.get("ok") else min(backoff * 2, 900)
            time.sleep(backoff)


class AcctEstimator:
    """Best-effort estimate of ACCOUNT-WIDE usage, which no provider exposes.

    All math runs in WEIGHTED (cost-equivalent) units, because the providers
    meter their limits by cost — raw local tokens are NOT comparable with the
    official percentage (a cache-heavy hour can add raw tokens far faster than
    it consumes the metered limit). In weighted units, local usage is a strict
    subset of account usage, so (weighted local)/(official %) is a true lower
    bound of the account's usage-per-percent. We ratchet the best bound seen —
    cumulative window readings plus deltas between official readings —
    padding denominators for the %'s integer rounding so the bound never
    overstates. A per-key raw/weighted mix factor converts the weighted
    estimate back to familiar raw-token magnitudes for display. Confidence is
    earned, not assumed: an absolute number is only reported when local growth
    explains most of the account's observed % growth.  State persists so the
    estimate sharpens over days, not per-run."""

    VERSION = 3

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.best = {}   # key -> weighted units per 1% of the account limit
        self.mix = {}    # key -> raw/weighted ratio for display conversion
        self.last = {}   # key -> (pct, weighted) anchor for >=2%-jump samples
        self.step = {}   # key -> (pct, weighted) anchor advanced every sample
        self.sums = {}   # key -> [dpct_total, dweighted_total] evidence
        try:
            d = json.load(open(path))
            if d.get("v") == self.VERSION:
                self.best = {k: float(v) for k, v in d.get("best", {}).items()}
                self.mix = {k: float(v) for k, v in d.get("mix", {}).items()}
                self.sums = {k: [float(v[0]), float(v[1])] for k, v in d.get("sums", {}).items()}
        except Exception:
            pass

    def _save(self):
        try:
            tmp = str(self.path) + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"v": self.VERSION, "best": self.best,
                           "mix": self.mix, "sums": self.sums}, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def sample(self, key, raw, weighted, pct):
        """Feed (raw local tokens, weighted local tokens, official %) for a
        window. Callers must only invoke this when the local window is aligned
        to the OFFICIAL window boundary — sampling against a fallback window
        poisons the ratios."""
        if pct is None or weighted is None:
            return
        with self.lock:
            updated = False
            if weighted > 0 and raw:
                self.mix[key] = raw / weighted
            if pct >= 2:
                r = weighted / (pct + 0.5)              # rounding-safe floor
                if r > self.best.get(key, 0):
                    self.best[key] = r
                    updated = True
            sp = self.step.get(key)
            if sp is None or pct < sp[0] or weighted < sp[1]:
                self.step[key] = (pct, weighted)        # window reset / first sight
            elif pct > sp[0] or weighted > sp[1]:
                sums = self.sums.setdefault(key, [0.0, 0.0])
                sums[0] += pct - sp[0]
                sums[1] += weighted - sp[1]
                self.step[key] = (pct, weighted)
                updated = True
            lp = self.last.get(key)
            if lp is None or pct < lp[0] or weighted < lp[1]:
                self.last[key] = (pct, weighted)
            elif pct - lp[0] >= 2:
                r = (weighted - lp[1]) / (pct - lp[0] + 1.0)
                if r > self.best.get(key, 0):
                    self.best[key] = r
                self.last[key] = (pct, weighted)
                updated = True
            if updated:
                self._save()

    def estimate(self, key, pct, raw):
        r = self.best.get(key)
        m = self.mix.get(key)
        if not r or not m or pct is None:
            return None
        dpct, dw = self.sums.get(key, (0.0, 0.0))
        explained = min(1.0, dw / (dpct * r)) if dpct > 0 and r > 0 else None
        confident = dpct >= 3 and explained is not None and explained >= 0.5
        return {
            "tokens": max(pct * r * m, raw or 0),   # raw-equivalent, floored at local
            "limit": 100 * r * m,
            "confident": confident,
            "explained": explained,
        }


def ev_total(e):
    return e["in"] + e["out"] + e["cw"] + e["cr"]


def ev_weighted(e, w):
    return e["in"] * w["in"] + e["out"] * w["out"] + e["cw"] * w["cw"] + e["cr"] * w["cr"]


def build_blocks(events):
    blocks, cur = [], None
    span = timedelta(hours=BLOCK_HOURS)
    for e in events:
        if cur is None or e["ts"] >= cur["start"] + span:
            cur = {"start": e["ts"].replace(minute=0, second=0, microsecond=0), "events": []}
            blocks.append(cur)
        cur["events"].append(e)
    for b in blocks:
        b["end"] = b["start"] + span
        b["total"] = sum(ev_total(e) for e in b["events"])
    return blocks


def percentile(values, p):
    if not values:
        return None
    vs = sorted(values)
    return vs[min(len(vs) - 1, max(0, round(p / 100 * (len(vs) - 1))))]


def agg(evs):
    o = {"in": 0, "out": 0, "cw": 0, "cr": 0}
    for e in evs:
        o["in"] += e["in"]; o["out"] += e["out"]; o["cw"] += e["cw"]; o["cr"] += e["cr"]
    o["total"] = o["in"] + o["out"] + o["cw"] + o["cr"]
    return o


def agg_by_model(evs):
    models = {}
    for e in evs:
        m = models.setdefault(e["model"] or "unknown", {"in": 0, "out": 0, "cw": 0, "cr": 0})
        m["in"] += e["in"]; m["out"] += e["out"]; m["cw"] += e["cw"]; m["cr"] += e["cr"]
    rows = [
        {"id": k, "model": label_model(k),
         "in": v["in"], "out": v["out"], "cw": v["cw"], "cr": v["cr"],
         "total": v["in"] + v["out"] + v["cw"] + v["cr"]}
        for k, v in models.items()
    ]
    rows.sort(key=lambda r: -r["total"])
    return rows


class State:
    def __init__(self, scanner, usage, args, plan_label, fallback_limit,
                 codex_scanner=None, codex_usage=None):
        self.scanner = scanner
        self.usage = usage
        self.args = args
        self.plan_label = plan_label
        self.fallback_limit = fallback_limit
        self.codex_scanner = codex_scanner
        self.codex_usage = codex_usage
        self.est = AcctEstimator(Path(__file__).parent / ".state.json")
        self.live = {}   # window-key -> live account-token sync state
        self._cache = (0.0, None)
        self.lock = threading.Lock()

    def _live_est(self, key, pct, local_raw, limit):
        """The user-visible account-wide token counter, per the sync design:
        anchor at official% x limit, advance live with local token deltas
        between official readings, re-sync upward when the official % moves.
        Never ticks backward mid-window (freezes instead if the provider
        recomputes downward); a fresh window key starts a fresh counter."""
        if pct is None or not limit:
            return None
        floor_base = pct / 100.0 * limit
        cap = (pct + 1.5) / 100.0 * limit    # can't be past the next % boundary
        st = self.live.get(key)
        if st is None or (local_raw or 0) < st["local"]:
            st = {"pct": pct, "base": floor_base, "local": local_raw or 0}
        if pct > st["pct"]:
            cur = st["base"] + ((local_raw or 0) - st["local"])
            st = {"pct": pct, "base": min(max(floor_base, cur), cap),
                  "local": local_raw or 0}
        elif pct < st["pct"]:
            st = dict(st, pct=pct)           # provider recomputed down: freeze
        est = st["base"] + ((local_raw or 0) - st["local"])
        self.live[key] = st
        return min(est, cap)

    def _codex_block(self, now):
        if not self.codex_usage:
            return None
        cu = self.codex_usage.snapshot()
        with self.codex_scanner.lock:
            events = list(self.codex_scanner.events)
            files = self.codex_scanner.files_tracked
        win = cu.get("primary") if cu.get("ok") else None
        # Prefer a rollout-embedded snapshot when it's fresher than the endpoint
        # poll and covers the same window — it carries fractional precision.
        snap = self.codex_scanner.rate
        if win and snap and win.get("seconds") and abs(snap["window_seconds"] - win["seconds"]) < 3600:
            fetched = parse_ts(cu.get("fetched_at"))
            if fetched is None or snap["ts"] >= fetched:
                win = dict(win, pct=snap["pct"])
        # Aggregate exact local tokens over the official window (or 7d fallback).
        if win and win.get("resets_at") and win.get("seconds"):
            wstart = parse_ts(win["resets_at"]) - timedelta(seconds=win["seconds"])
        else:
            wstart = now - timedelta(days=7)
        win_events = [e for e in events if e["ts"] >= wstart]
        models = agg_by_model(win_events)
        additional = cu.get("additional") or []
        for m in models:
            sc = next((a for a in additional if a.get("name") and (
                a["name"].lower() in m["id"].lower() or m["id"].lower() in a["name"].lower()
                or a["name"].lower().replace(" ", "-") in m["id"].lower())), None)
            m["pct"] = sc["pct"] if sc else None
            m["pctLabel"] = f"of your {sc['name']} cap" if sc else None
        # Scoped limits with no local tokens still deserve a row — but only if
        # some of that cap is actually used (usage may come from other devices).
        for a in additional:
            if not any(m.get("pctLabel") and a["name"] in m["pctLabel"] for m in models):
                models.append({"id": "cap:" + a["name"], "model": a["name"],
                               "in": 0, "out": 0, "cw": 0, "cr": 0, "total": 0,
                               "pct": a["pct"], "pctLabel": "of its own cap",
                               "resets_at": a.get("resets_at")})
        # Zero rows say nothing: drop models with no usage and no cap consumption.
        models = [m for m in models if m["total"] > 0 or (m.get("pct") or 0) > 0]
        tokens = agg(win_events)
        cx_pct = win.get("pct") if win else None
        est_key = f"codex_{win['name']}" if win else "codex"
        if win and win.get("resets_at") and cx_pct is not None:
            cx_weighted = sum(ev_weighted(e, CODEX_W) for e in win_events)
            self.est.sample(est_key, tokens["total"], cx_weighted, cx_pct)
        cx_meas = self.est.estimate(est_key, cx_pct, tokens["total"]) or {}
        prior = CODEX_WEEKLY_PRIOR.get((cu.get("plan") or "").lower())
        cx_lim = max(filter(None, (cx_meas.get("limit"), prior)), default=None)
        cx_live = self._live_est(("cx", (win or {}).get("resets_at") or ""),
                                 cx_pct, tokens["total"], cx_lim)
        cx_est = ({"tokens": cx_live, "limit": cx_lim, "pct": cx_live / cx_lim * 100}
                  if cx_live and cx_lim else None)
        if cx_est and tokens["total"]:
            k = cx_est["tokens"] / tokens["total"]
            models = [dict(m, estTotal=m["total"] * k) if m["total"] else m for m in models]
        return {
            "ok": bool(cu.get("ok")),
            "error": cu.get("error"),
            "source": cu.get("source", "endpoint"),
            "plan": cu.get("plan"),
            "window": win,
            "tokens": tokens,
            "models": models,
            "est": cx_est,
            "meta": {"files": files, "events": len(events)},
        }

    def snapshot(self):
        with self.lock:
            at, snap = self._cache
            if snap and time.monotonic() - at < 0.5:
                return snap
            snap = self._compute()
            self._cache = (time.monotonic(), snap)
            return snap

    def _compute(self):
        now = datetime.now(timezone.utc)
        with self.scanner.lock:
            events = list(self.scanner.events)
            files_tracked = self.scanner.files_tracked

        official = self.usage.snapshot()
        off_ok = official.get("ok")

        # Session window: prefer the official 5-hour block; else fall back to the
        # greedy block heuristic so the dashboard still works offline.
        win_start = parse_ts(official.get("session_window_start")) if off_ok else None
        session_end = None
        if win_start:
            sess = win_start
            resets = parse_ts(official.get("session", {}).get("resets_at"))
            session_end = resets or (win_start + timedelta(hours=BLOCK_HOURS))
        else:
            blocks = build_blocks(events)
            active = blocks[-1] if blocks and now < blocks[-1]["end"] else None
            sess = active["start"] if active else now
            session_end = active["end"] if active else None

        session_events = [e for e in events if e["ts"] >= sess]
        session_tokens = agg(session_events)
        session_models = agg_by_model(session_events)

        # Calibrated session limit + fallback estimate.
        calibrated = official.get("session_limit_calibrated") if off_ok else None
        if calibrated:
            limit = calibrated
            limit_label = "calibrated to official session %"
            limit_source = "calibrated"
        elif self.args.limit:
            limit, limit_label, limit_source = self.args.limit, "custom limit", "estimate"
        else:
            limit = self.fallback_limit
            limit_label = f"{self.plan_label} estimate"
            limit_source = "estimate"

        # Weekly window: align to the official 7-day reset when available, so the
        # exact token sum matches what the official percentage measures.
        wstart = parse_ts(official.get("weekly_window_start")) if off_ok else None
        if not wstart:
            wstart = now - timedelta(days=7)
        week_events = [e for e in events if e["ts"] >= wstart]
        week_tokens = agg(week_events)
        week_models = agg_by_model(week_events)
        weekly_all = official.get("weekly_all") or {}
        weekly_limit = official.get("weekly_limit_calibrated") if off_ok else None
        week_pct = (week_tokens["total"] / weekly_limit * 100) if weekly_limit else None

        # Feed the account-wide estimator with (raw, weighted, OFFICIAL %) — but
        # ONLY when the local window aligns with the official window boundary;
        # sampling against a fallback window poisons the ratios.
        s_off = (official.get("session") or {}).get("pct") if off_ok else None
        w_off = weekly_all.get("pct") if off_ok else None
        if win_start is not None and s_off is not None:
            s_weighted = sum(ev_weighted(e, CLAUDE_W) for e in session_events)
            self.est.sample("claude_5h", session_tokens["total"], s_weighted, s_off)
        if off_ok and official.get("weekly_window_start") and w_off is not None:
            w_weighted = sum(ev_weighted(e, CLAUDE_W) for e in week_events)
            self.est.sample("claude_weekly", week_tokens["total"], w_weighted, w_off)

        burn_cut = now - timedelta(minutes=10)
        per_sec = sum(ev_total(e) for e in events if e["ts"] >= burn_cut) / 600.0

        spark = [0] * 60
        spark_cut = now - timedelta(minutes=60)
        for e in events:
            if e["ts"] >= spark_cut:
                i = int((e["ts"] - spark_cut).total_seconds() // 60)
                if 0 <= i < 60:
                    spark[i] += ev_total(e)

        # Live account-wide token counters: best available limit floor
        # (measured ratchet vs calibration), anchored to the official %,
        # advanced by local deltas between official readings.
        s_meas = self.est.estimate("claude_5h", s_off, session_tokens["total"]) or {}
        s_lim = max(filter(None, (s_meas.get("limit"), limit)), default=None)
        s_live = self._live_est(("5h", str(session_end)), s_off,
                                session_tokens["total"], s_lim)
        s_est = ({"tokens": s_live, "limit": s_lim, "pct": s_live / s_lim * 100}
                 if s_live and s_lim else None)
        w_meas = self.est.estimate("claude_weekly", w_off, week_tokens["total"]) or {}
        w_lim = max(filter(None, (w_meas.get("limit"), weekly_limit)), default=None)
        w_live = self._live_est(("wk", weekly_all.get("resets_at") or ""), w_off,
                                week_tokens["total"], w_lim)
        w_est = ({"tokens": w_live, "limit": w_lim, "pct": w_live / w_lim * 100}
                 if w_live and w_lim else None)

        def scale_models(models, est_blk, local_total):
            if not est_blk or not local_total:
                return models
            k = est_blk["tokens"] / local_total
            return [dict(m, estTotal=m["total"] * k) for m in models]

        session_models = scale_models(session_models, s_est, session_tokens["total"])
        week_models = scale_models(week_models, w_est, week_tokens["total"])

        return {
            "now": now.isoformat(),
            "plan": {"label": self.plan_label},
            "official": official,
            "session": {
                "start": sess.isoformat(),
                "end": session_end.isoformat() if session_end else None,
                "tokens": session_tokens,
                "models": session_models,
                "limit": limit,
                "limitLabel": limit_label,
                "limitSource": limit_source,
                "pct": (session_tokens["total"] / limit * 100) if limit else None,
                "est": s_est,
                "officialPct": s_off,
            },
            "week": {
                "start": wstart.isoformat(),
                "resets_at": weekly_all.get("resets_at"),
                "tokens": week_tokens,
                "models": week_models,
                "limit": weekly_limit,
                "pct": week_pct,
                "officialPct": weekly_all.get("pct"),
                "severity": weekly_all.get("severity", "normal"),
                "scoped": official.get("weekly_models", []) if off_ok else [],
                "source": "calibrated" if weekly_limit else "estimate",
                "est": w_est,
            },
            "burn": {"perSec": per_sec, "perMin": per_sec * 60},
            "spark": spark,
            "codex": self._codex_block(now),
            "meta": {"files": files_tracked, "events": len(events)},
        }


STATE = None
INDEX_PATH = Path(__file__).parent / "index.html"
ICONS = {
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.png": ("favicon.png", "image/png"),
    "/favicon.ico": ("favicon.png", "image/png"),   # modern browsers accept PNG here
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                self._send(200, INDEX_PATH.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "index.html not found next to server.py", "text/plain")
        elif path == "/api/state":
            self._send(200, json.dumps(STATE.snapshot()), "application/json")
        elif path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(f"data: {json.dumps(STATE.snapshot())}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(1.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        elif path in ICONS:
            name, ctype = ICONS[path]
            try:
                self._send(200, (INDEX_PATH.parent / name).read_bytes(), ctype)
            except OSError:
                self._send(404, "not found", "text/plain")
        else:
            self._send(404, "not found", "text/plain")


def main():
    global STATE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--dir", default=str(Path.home() / ".claude" / "projects"))
    ap.add_argument("--plan", choices=sorted(PLAN_PRESETS))
    ap.add_argument("--limit", type=int, help="fallback tokens per 5h window")
    ap.add_argument("--usage-poll", type=int, default=60, help="seconds between official polls")
    ap.add_argument("--no-usage", action="store_true", help="disable the official endpoint")
    args = ap.parse_args()

    # Auto-detect plan from credentials, unless overridden. The rateLimitTier
    # field is unreliable for the 5x/20x distinction, so show the plain
    # subscription type ("Max") and use the tier only for the offline fallback.
    cred = read_credentials()
    sub = cred.get("subscriptionType") or "unknown"
    plan_label = {"max": "Max", "pro": "Pro", "free": "Free", "team": "Team"}.get(sub, str(sub).capitalize())
    tier = cred.get("rateLimitTier", "")
    plan_key = TIER_TO_PLAN.get(tier, (None, None))[0]
    if args.plan:
        plan_key, plan_label = args.plan, args.plan
    fallback_limit = args.limit or PLAN_PRESETS.get(plan_key, DEFAULT_LIMIT)

    root = Path(args.dir).expanduser()
    scanner = UsageScanner(root)
    version = detect_version()
    usage = OfficialUsage(scanner, version, args.usage_poll, not args.no_usage)
    codex_scanner = CodexScanner([CODEX_HOME / "sessions", CODEX_HOME / "archived_sessions"])
    codex_usage = CodexUsage(args.usage_poll, not args.no_usage and CODEX_HOME.exists())
    STATE = State(scanner, usage, args, plan_label, fallback_limit,
                  codex_scanner, codex_usage)

    print(f"token-mon: plan={plan_label} · claude-code/{version} · scanning {root}")
    scanner.scan()
    print(f"token-mon: {len(scanner.events)} usage events from {scanner.files_tracked} transcripts")
    if not args.no_usage:
        usage.poll()
        d = usage.snapshot()
        if d.get("ok"):
            print(f"token-mon: official session={d['session']['pct']}% "
                  f"weekly={d['weekly_all']['pct']}% "
                  f"(calibrated limit ~{int(d['session_limit_calibrated'] or 0):,} tok)")
        else:
            print(f"token-mon: official usage unavailable ({d.get('error')}); using estimate")

    threading.Thread(target=usage.run, daemon=True).start()
    threading.Thread(target=codex_usage.run, daemon=True).start()
    codex_scanner.scan()

    def scan_loop():
        while True:
            time.sleep(1.0)
            try:
                scanner.scan()
                codex_scanner.scan()
            except Exception as e:
                print(f"scan error: {e}")

    threading.Thread(target=scan_loop, daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"token-mon: dashboard at http://{args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
