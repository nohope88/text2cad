#!/usr/bin/env python3
"""Anthropic-shaped proxy in front of a self-hosted relay.

    ./qwen_proxy.py [--port 8099] [--upstream URL] [--key-file P]
                    [--max-tokens N] [--think|--no-think] [--pin MODEL]

Two jobs the `claude` CLI cannot do itself:

  1. AUTH. It sends whatever is in ANTHROPIC_API_KEY as x-api-key. The grid
     relay wants a long-lived JWT; keeping that in text2cad's environment would
     copy a credential into every phase's process table. The proxy holds it
     instead and rewrites the header, so SELFHOST_KEY can stay a dummy.
  2. BODY FIELDS. The CLI builds its own JSON and has no flag to add one.
     - max_tokens is clamped so a phase cannot ask for more than the relay
       allows.
     - --no-think injects chat_template_kwargs {"enable_thinking": false},
       which is the ONLY spelling the eternalai gateway honours (measured
       2026-08-18: `thinking:{type:disabled}`, top-level `enable_thinking`, and
       `extra_body` are all silently ignored). The grid relay emits no thinking
       blocks at all, so this is OFF by default now and only needed if you
       point --upstream back at eternalai.
     - --pin overrides the model on every request. `auto` is a ROUTER: four
       consecutive probes landed on qwen3.8-27b, qwen3.6-27b and
       qwen3.6-35b-a3b-uncensored. Fine for a chat, wrong for a pipeline whose
       phases are supposed to be comparable.

THREADED on purpose: text2cad runs three proposers and four lenses in parallel,
and a single-threaded proxy would quietly serialise the panel and make every
wall-clock number a lie.
"""
import itertools
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

DEFAULTS = "/root/panda-secrets/grid-relay.env"
# Measured 2026-08-18 against the grid relay: only these four accept an image
# block. gemma-4-31B-it, DeepSeek-V4-Flash-0731, Laguna-S-2.1 and Qwen3.5-0.8B
# are refused outright ("No active provider for this model supports images").
# Half of text2cad — the 4-lens panel and build-likeness-check — sends renders,
# so a request routed to a blind model is a hard failure six hours into a run.
# Any request carrying an image is forced onto VISION_FALLBACK.
VISION_OK = {"qwen/qwen3.8-27b", "qwen/qwen3.8-27b-uncensored",
             "qwen/qwen3.6-27b", "qwen/qwen3.6-35b-a3b-uncensored"}
VISION_FALLBACK = "qwen/qwen3.8-27b"
PORT = 8099
UPSTREAM = ""
KEY = ""
MAX_TOKENS = 16000
# Read timeout. Measured 2026-08-18: a PROPOSE turn — the smallest context in
# the pipeline — already took 534s on this relay. BUILD carries many times that
# context, so 900s would guillotine it hours into a run. Connect stays short;
# it is the response that is slow, not the handshake.
#
# MAX_TOKENS is 16000, not 64000, for the same reason. phase_env sets
# CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000, so every request arrived asking for more
# than 64000 (clamped 3/3) — a single turn allowed to emit 64k tokens at this
# relay's speed is a connection held open for many minutes, and the relay cuts
# those: propose-family died at 901.9s and brief at 963.4s, both with
# ChunkedEncodingError "Response ended prematurely" in the proxy log. 16000 is
# still far more than a CadQuery turn needs.
# 600, not 2400. Measured 2026-08-19 02:06: across 185 turns of one brief the
# median gap was 4s, but ONE request hung 2018s (33.6 min). A generous read
# timeout does not rescue that request — it just makes the proxy wait out the
# hang instead of failing fast and retrying, turning a few seconds of loss into
# half an hour. Legitimate time-to-first-token on the biggest contexts measured
# ~530s, so 600s clears real work and cuts hangs loose.
READ_TIMEOUT = 600
NO_THINK = False
PIN = ""
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
# 6, not 3. Measured 2026-08-19 01:00: across one brief phase the relay dropped
# a stream 6 times (all recovered) and then dropped the SAME request 3 times in
# a row — retries ran out, the proxy returned 502, and the agent died with
# error_during_execution after 81 minutes of good work. The drops are transient
# and independent, so more attempts with a longer backoff is the cheap fix.
RETRIES = 6
# Cap the TOTAL time spent retrying one request, not just each attempt.
# 2026-08-19 03:29: READ_TIMEOUT=600 x RETRIES=6 meant a single hung request
# could burn 60 minutes of silent retrying — longer than before either patch —
# while the agent saw nothing, its transcript froze, and the phase's own 7200s
# clock kept running. Lowering the per-attempt timeout without bounding the sum
# made the worst case worse. Past this budget, fail loudly and let the phase
# (and autoresume) react instead of hanging.
RETRY_BUDGET_S = 900
# A read timeout only fires when the socket goes SILENT. This relay keeps
# trickling bytes (SSE keepalives) on a response that never completes, so
# requests never raises and the proxy waits forever: 2026-08-19 05:12 an agent
# sat 12 minutes on its FIRST call with retried=0 and not one assistant message
# on disk. Bound the whole response, not just the gaps inside it.
RESPONSE_DEADLINE_S = 900
# TRACE=1 writes one JSONL row per request: timing, status, byte counts, retry
# count and the first 400 bytes of any non-2xx body. Off by default — a full
# body log of an agentic run is gigabytes. On, it is what a gateway team needs
# to trace a dropped stream; counters alone cannot give them that.
TRACE = False
TRACE_PATH = "/root/text2cad/logs/proxy-trace.jsonl"
_tlock = threading.Lock()


def trace(row):
    if not TRACE:
        return
    try:
        with _tlock, open(TRACE_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
# vLLM rejects when input_tokens + max_tokens exceeds the model window, so the
# flat max_tokens this proxy injects becomes part of the overflow: 2026-08-19
# BUILD and BUILD2 both died with "maximum context length is 262144" after 13
# turns, and 16000 of that ceiling was ours. When the relay says so, shrink our
# own ask and try again rather than failing the phase.
CTX_ERR = b"maximum context length"
SHRUNK_MAX_TOKENS = 2048
_stats = {"n": 0, "clamped": 0, "pinned": 0, "errors": 0, "vision_rerouted": 0,
          "retried": 0, "deadline_cut": 0, "ctx_shrunk": 0, "fallback_used": 0, "fallback_failed": 0, "models": {}}


def _has_image(d) -> bool:
    """True if any message carries an image block (Anthropic content array)."""
    for m in d.get("messages") or []:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "image":
                    return True
    return False
_lock = threading.Lock()


def env_file(p):
    d = {}
    p = Path(p)
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                d[k.strip()] = v.strip().strip('"')
    return d


# ---------------------------------------------------------------- fallback --
# qwen serves everything; Anthropic is borrowed ONLY where qwen comes back with
# nothing. Measured 2026-08-19: every long agentic phase died at
# "automatic compaction failed: summarization produced empty response" — the CLI
# asks the model to summarise the conversation so it can compact, and qwen
# returns an empty body. Ordinary generation and tool use on the same model are
# fine, so this borrows the login for one request shape and nothing else.
#
# Detection is by SYMPTOM, not by prompt signature: a summarisation-shaped
# request (one user message, no tools) that comes back with no text. Guessing at
# the CLI's exact compaction wording would break the day it changes.
FALLBACK = False
CLAUDE_BIN = "claude"


# A compaction prompt is the whole conversation pasted into one message, so it
# is always huge. Without the size floor "one user message, no tools" also
# catches every trivial prompt and needlessly forces them non-streaming.
SUMM_MIN_CHARS = 20000


def looks_like_summarisation(d) -> bool:
    msgs = d.get("messages") or []
    if len(msgs) != 1 or msgs[0].get("role") != "user" or d.get("tools"):
        return False
    return len(prompt_text_of(d)) >= SUMM_MIN_CHARS


def text_of(j) -> str:
    return "".join(b.get("text", "") for b in (j.get("content") or [])
                   if isinstance(b, dict) and b.get("type") == "text").strip()


def prompt_text_of(d) -> str:
    c = (d.get("messages") or [{}])[0].get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c
                          if isinstance(b, dict) and b.get("type") == "text")
    return ""


def claude_fallback(prompt: str):
    """Run the summarisation on this machine's claude login. No API key: the
    CLI holds an OAuth session, so the proxy shells out to it rather than
    trying to mint a credential it does not have."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY",
                        "ANTHROPIC_AUTH_TOKEN")}
    try:
        r = subprocess.run([CLAUDE_BIN, "-p", "--output-format", "json",
                            "--max-turns", "1"],
                           input=prompt, capture_output=True, text=True,
                           timeout=600, env=env)
        line = (r.stdout or "").strip().splitlines()
        j = json.loads(line[-1]) if line else {}
        out = (j.get("result") or "").strip()
        return out or None
    except Exception as e:                                       # noqa: BLE001
        print(f"[fallback] claude -p failed: {e}", flush=True)
        return None


def sse_bytes(text: str, model: str) -> bytes:
    """Rebuild an Anthropic SSE stream around a finished answer."""
    mid = "msg_fallback"
    ev = [
        ("message_start", {"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": max(1, len(text) // 4)}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(f"event: {n}\ndata: {json.dumps(o)}\n\n".encode() for n, o in ev)


def json_bytes(text: str, model: str) -> bytes:
    return json.dumps({
        "id": "msg_fallback", "type": "message", "role": "assistant",
        "model": model, "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": max(1, len(text) // 4)},
    }).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _relay(self, method):
        summ = None
        want_stream = False
        body = b""
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            body = self.rfile.read(n)

        if body:
            try:
                d = json.loads(body)
                if isinstance(d, dict) and "model" in d:
                    if PIN:
                        d["model"] = PIN
                        with _lock:
                            _stats["pinned"] += 1
                    mt = d.get("max_tokens")
                    if isinstance(mt, int) and mt > MAX_TOKENS:
                        d["max_tokens"] = MAX_TOKENS
                        with _lock:
                            _stats["clamped"] += 1
                    elif mt is None:
                        d["max_tokens"] = MAX_TOKENS
                    if NO_THINK:
                        d["chat_template_kwargs"] = {"enable_thinking": False}
                    # Force any image-bearing request onto a model that can see.
                    if d.get("model") not in VISION_OK and _has_image(d):
                        d["model"] = VISION_FALLBACK
                        with _lock:
                            _stats["vision_rerouted"] += 1
                    # A summarisation-shaped call is the one qwen answers with
                    # nothing. Force it non-streaming upstream so the whole reply
                    # can be inspected before a single byte reaches the CLI —
                    # once the client owns a 200 there is no substituting it.
                    if FALLBACK and looks_like_summarisation(d):
                        summ = d
                        want_stream = bool(d.get("stream"))
                        d["stream"] = False
                    body = json.dumps(d).encode()
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass  # not JSON we understand — forward untouched

        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        hdrs.pop("x-api-key", None)          # the CLI's dummy key, not ours
        hdrs.pop("X-Api-Key", None)
        hdrs["Authorization"] = f"Bearer {KEY}"
        hdrs["Content-Length"] = str(len(body))
        url = UPSTREAM.rstrip("/") + self.path

        with _lock:
            _stats["n"] += 1
        # Retry ONLY before the first byte reaches the client. Once headers and
        # a chunk are out the CLI owns a 200 and a partial stream, and there is
        # no honest way to take it back — so pull the first chunk here, while a
        # retry is still invisible, and commit the response only after it lands.
        r = first = None
        t_start = time.monotonic()
        for attempt in range(RETRIES):
            if time.monotonic() - t_start > RETRY_BUDGET_S:
                err = Exception(f"retry budget {RETRY_BUDGET_S}s exhausted "
                                f"after {attempt} attempt(s)")
                break
            try:
                r = requests.request(method, url, data=body, headers=hdrs,
                                     stream=True, timeout=(30, READ_TIMEOUT))
                stream = r.iter_content(chunk_size=None)
                first, err = None, None
                try:
                    for c in stream:
                        if c:
                            first = c
                            break
                except Exception as e:                           # noqa: BLE001
                    err = e
                if err is None and r.status_code == 400 and first and CTX_ERR in first:
                    # our max_tokens is part of the sum that overflowed — shrink it
                    try:
                        d2 = json.loads(body)
                        if d2.get("max_tokens", 0) > SHRUNK_MAX_TOKENS:
                            d2["max_tokens"] = SHRUNK_MAX_TOKENS
                            body = json.dumps(d2).encode()
                            hdrs["Content-Length"] = str(len(body))
                            with _lock:
                                _stats["ctx_shrunk"] += 1
                            r.close()
                            first, err = None, Exception("ctx overflow, retrying smaller")
                            continue
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
                if err is None:
                    break
            except Exception as e:                               # noqa: BLE001
                err, r = e, None
            if attempt < RETRIES - 1 and time.monotonic() - t_start < RETRY_BUDGET_S:
                with _lock:
                    _stats["retried"] += 1
                if r is not None:
                    r.close()
                time.sleep(min(3 * (attempt + 1), 20))
                continue
            break
        if first is None and (r is None or err is not None):
            with _lock:
                _stats["errors"] += 1
            msg = json.dumps({"type": "error", "error": {
                "type": "api_error", "message": f"proxy upstream: {err}"}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        if summ is not None and r.status_code < 300:
            whole = bytes(first or b"") + b"".join(
                c for c in stream if c)                     # small by nature
            text = ""
            try:
                text = text_of(json.loads(whole))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            if not text:
                repl = claude_fallback(prompt_text_of(summ))
                with _lock:
                    _stats["fallback_used" if repl else "fallback_failed"] += 1
                if repl:
                    print(f"[fallback] qwen returned empty on a summarisation; "
                          f"answered via claude login ({len(repl)} chars)", flush=True)
                    model = summ.get("model", "")
                    whole = sse_bytes(repl, model) if want_stream else json_bytes(repl, model)
            elif want_stream:
                whole = sse_bytes(text, summ.get("model", ""))
            self.send_response(200)
            self.send_header("Content-Type",
                             "text/event-stream" if want_stream else "application/json")
            self.send_header("Content-Length", str(len(whole)))
            self.end_headers()
            self.wfile.write(whole)
            trace({"t": round(time.time(), 2), "path": self.path, "status": 200,
                   "req_bytes": len(body), "resp_head_bytes": len(whole),
                   "attempts": attempt + 1, "route": "summarisation",
                   "fallback": not text, "prompt_chars": len(prompt_text_of(summ))})
            return

        self.send_response(r.status_code)
        clen = r.headers.get("Content-Length")
        for k, v in r.headers.items():
            if k.lower() not in HOP:
                self.send_header(k, v)
        if clen is None:
            self.send_header("Connection", "close")   # SSE: close is the EOF
        self.end_headers()
        buf = bytearray()
        t_resp = time.monotonic()
        try:
            for chunk in itertools.chain([first] if first else [], stream):
                if time.monotonic() - t_resp > RESPONSE_DEADLINE_S:
                    with _lock:
                        _stats["deadline_cut"] += 1
                    break          # client sees a truncated stream and errors
                if chunk:
                    if len(buf) < 4096:
                        buf.extend(chunk[:4096])
                    self.wfile.write(chunk)
                    self.wfile.flush()        # never buffer a token stream
        except (BrokenPipeError, ConnectionResetError):
            pass
        # record which model the router actually picked, so `auto` drift is visible
        try:
            head = bytes(buf).decode("utf-8", "replace")
            i = head.find('"model"')
            if i >= 0:
                m = head[i:i + 80].split(":", 1)[1].strip().strip('"').split('"')[0]
                with _lock:
                    _stats["models"][m] = _stats["models"].get(m, 0) + 1
        except Exception:                                        # noqa: BLE001
            pass
        trace({"t": round(time.time(), 2), "path": self.path,
               "status": r.status_code, "req_bytes": len(body),
               "resp_head_bytes": len(buf), "attempts": attempt + 1,
               "ttfb_s": round(t_resp - t_start, 2),
               "stream_s": round(time.monotonic() - t_resp, 2),
               "reqid": r.headers.get("x-request-id") or r.headers.get("cf-ray"),
               "err": None if r.status_code < 300
                      else bytes(buf)[:400].decode("utf-8", "replace")})
        if clen is None:
            self.close_connection = True

    def do_POST(self):
        self._relay("POST")

    def do_GET(self):
        if self.path == "/_proxy_stats":
            b = json.dumps({**_stats, "upstream": UPSTREAM, "pin": PIN or "(none)",
                            "max_tokens": MAX_TOKENS, "no_think": NO_THINK}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        self._relay("GET")


if __name__ == "__main__":
    cfg = env_file(DEFAULTS)
    UPSTREAM = cfg.get("GRID_RELAY_URL", "")
    KEY = cfg.get("GRID_RELAY_KEY", "")
    a = sys.argv[1:]
    if "--port" in a:       PORT = int(a[a.index("--port") + 1])
    if "--upstream" in a:   UPSTREAM = a[a.index("--upstream") + 1]
    if "--key-file" in a:
        c2 = env_file(a[a.index("--key-file") + 1])
        UPSTREAM = c2.get("GRID_RELAY_URL", UPSTREAM)
        KEY = c2.get("GRID_RELAY_KEY", KEY)
    if "--max-tokens" in a: MAX_TOKENS = int(a[a.index("--max-tokens") + 1])
    if "--read-timeout" in a: READ_TIMEOUT = int(a[a.index("--read-timeout") + 1])
    if "--pin" in a:        PIN = a[a.index("--pin") + 1]
    if "--no-think" in a:   NO_THINK = True
    if "--trace" in a:      TRACE = True
    if "--fallback-claude" in a: FALLBACK = True
    if not (UPSTREAM and KEY):
        print("missing upstream/key — refusing to start"); raise SystemExit(1)
    print(f"qwen_proxy: 127.0.0.1:{PORT} -> {UPSTREAM}\n"
          f"  pin={PIN or '(none, router decides)'} max_tokens={MAX_TOKENS} "
          f"no_think={NO_THINK} read_timeout={READ_TIMEOUT}s retries={RETRIES}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
