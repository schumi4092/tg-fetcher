"""AI backend adapter: unified one-shot + streaming over API or Claude Code CLI.

Public API:
    ai_available() -> bool
    ai_call(prompt, system, model, max_tokens=1024, cache_prefix=None) -> (text, usage_or_None)
    ai_stream(prompt, system, model, max_tokens=8192, cache_prefix=None) -> iterator yielding dict:
        {"type": "token", "token": str}         — partial text
        {"type": "done", "text": str, "usage": usage_or_None}
        {"type": "error", "error": str}
    backend_name() -> "api" | "cli"

Prompt caching (cache_prefix):
    Optional string that MUST be a prefix of `prompt`. When set on the API
    backend, the system prompt and the prefix are sent as cached blocks
    (`cache_control: ephemeral`); the volatile suffix is sent uncached. Repeat
    calls within ~5min that share the same system + prefix hit the server-side
    cache, dropping TTFT and cutting input cost on the cached portion to ~0.1×.
    Ignored on the CLI backend (Claude Code does not expose cache_control).
"""

import json
import queue
import shutil
import subprocess
import threading
import time

from config import (
    AI_BACKEND,
    CLAUDE_API_KEY,
    CLAUDE_CLI_PATH,
    CLAUDE_CLI_TIMEOUT,
    MODEL_CLI_ALIASES,
    logger,
)

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False


_api_client = None
_cli_checked = False
_cli_available = False
_cli_resolved_path = None
_cli_lock = threading.Lock()


def backend_name():
    if AI_BACKEND == "cli":
        return "cli"
    return "api"


def _get_api_client():
    global _api_client
    if _api_client is not None:
        return _api_client
    if not HAS_ANTHROPIC or not CLAUDE_API_KEY:
        return None
    _api_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY, timeout=600.0)
    return _api_client


def _check_cli():
    global _cli_checked, _cli_available, _cli_resolved_path
    with _cli_lock:
        if _cli_checked:
            return _cli_available
        _cli_checked = True
        # Resolve to full path so Windows subprocess can find .cmd/.exe without shell=True.
        # Prefer the inner claude.exe over the claude.CMD wrapper: the .CMD on Windows
        # can mangle stdout forwarding (buffered via cmd.exe) when stdin is large.
        resolved = shutil.which(CLAUDE_CLI_PATH)
        if resolved and resolved.lower().endswith(".cmd"):
            import os as _os
            cmd_dir = _os.path.dirname(resolved)
            candidate = _os.path.join(cmd_dir, "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
            if _os.path.isfile(candidate):
                logger.info("Claude CLI: 繞過 .CMD wrapper → %s", candidate)
                resolved = candidate
        _cli_available = resolved is not None
        _cli_resolved_path = resolved or CLAUDE_CLI_PATH
        if not _cli_available:
            logger.warning("AI_BACKEND=cli 但找不到 %s 執行檔，請設定 CLAUDE_CLI_PATH", CLAUDE_CLI_PATH)
        else:
            logger.info("Claude CLI: %s", resolved)
        return _cli_available


def ai_available():
    if backend_name() == "cli":
        return _check_cli()
    return _get_api_client() is not None


def get_api_client_if_api():
    """For code paths that only run on the API backend (e.g. background streaming)."""
    return _get_api_client() if backend_name() == "api" else None


# ---------------------------------------------------------------------------
# API path
# ---------------------------------------------------------------------------

def _build_cached_payload(prompt, system, cache_prefix):
    """Return (system_param, messages_param) for the API call.

    When cache_prefix is set and is a real prefix of prompt, both the system
    string and the prefix get `cache_control: ephemeral` blocks so subsequent
    calls within the cache window reuse the prefilled KV cache. Uses 2 of
    Anthropic's 4 cache breakpoints; falls back to plain string params when
    caching isn't requested or doesn't apply.
    """
    if (
        cache_prefix
        and isinstance(prompt, str)
        and isinstance(cache_prefix, str)
        and len(cache_prefix) > 0
        and prompt.startswith(cache_prefix)
        and len(prompt) > len(cache_prefix)
    ):
        suffix = prompt[len(cache_prefix):]
        system_param = [{
            "type": "text",
            "text": system or "",
            "cache_control": {"type": "ephemeral"},
        }] if system else None
        user_content = [
            {"type": "text", "text": cache_prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": suffix},
        ]
        return system_param, [{"role": "user", "content": user_content}]
    return system, [{"role": "user", "content": prompt}]


def _api_call(prompt, system, model, max_tokens, cache_prefix=None, _retries=2):
    client = _get_api_client()
    if client is None:
        raise RuntimeError("CLAUDE_API_KEY 未設定或 anthropic 未安裝")
    system_param, messages_param = _build_cached_payload(prompt, system, cache_prefix)
    last_err = None
    for attempt in range(_retries + 1):
        try:
            kwargs = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": messages_param,
            }
            if system_param is not None:
                kwargs["system"] = system_param
            response = client.messages.create(**kwargs)
            return response.content[0].text, response.usage
        except Exception as e:
            last_err = e
            status = getattr(e, "status_code", None)
            # 只對暫時性錯誤重試：429 / 5xx / 連線錯誤
            transient = status in (429, 500, 502, 503, 504) or "Connection" in type(e).__name__
            if not transient or attempt >= _retries:
                raise
            wait = 2 * (attempt + 1)
            logger.warning("Claude API 暫時錯誤（%s），%ds 後重試 (%d/%d)", e, wait, attempt + 1, _retries)
            time.sleep(wait)
    raise last_err  # unreachable


def _api_stream(prompt, system, model, max_tokens, cache_prefix=None):
    client = _get_api_client()
    if client is None:
        yield {"type": "error", "error": "CLAUDE_API_KEY 未設定或 anthropic 未安裝"}
        return
    system_param, messages_param = _build_cached_payload(prompt, system, cache_prefix)
    try:
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages_param,
        }
        if system_param is not None:
            kwargs["system"] = system_param
        with client.messages.stream(**kwargs) as stream:
            parts = []
            for chunk in stream.text_stream:
                parts.append(chunk)
                yield {"type": "token", "token": chunk}
            final = stream.get_final_message()
        # Surface cache hit/miss for visibility in logs.
        try:
            usage = final.usage
            cache_read = getattr(usage, "cache_read_input_tokens", None)
            cache_write = getattr(usage, "cache_creation_input_tokens", None)
            if cache_read or cache_write:
                logger.info(
                    "Prompt cache: read=%s write=%s input=%s output=%s",
                    cache_read, cache_write,
                    getattr(usage, "input_tokens", None),
                    getattr(usage, "output_tokens", None),
                )
        except Exception:
            pass
        yield {"type": "done", "text": "".join(parts) or final.content[0].text, "usage": final.usage}
    except Exception as e:
        yield {"type": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# CLI path (subprocess to Claude Code)
# ---------------------------------------------------------------------------

def _cli_model_flag(model):
    return MODEL_CLI_ALIASES.get(model, "sonnet")


def _build_cli_cmd(system, model, stream_json=False):
    exe = _cli_resolved_path or CLAUDE_CLI_PATH
    # `--tools ""` disables all tools so Opus/Sonnet doesn't enter an agent loop
    # (without this, Claude Code still registers Read/Bash/Task/etc even with --system-prompt,
    # and the model may stall trying to call a tool instead of emitting text).
    cmd = [exe, "-p", "--model", _cli_model_flag(model), "--tools", ""]
    if system:
        # Use a dedicated system prompt so Claude Code's coding-agent prompt
        # does not leak into pure summarization / extraction tasks.
        cmd += ["--system-prompt", system]
    if stream_json:
        cmd += ["--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    else:
        cmd += ["--output-format", "json"]
    return cmd


def _cli_call(prompt, system, model, max_tokens):
    if not _check_cli():
        raise RuntimeError(f"找不到 Claude Code CLI：{CLAUDE_CLI_PATH}")
    cmd = _build_cli_cmd(system, model, stream_json=False)
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLAUDE_CLI_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Claude CLI 逾時（>{CLAUDE_CLI_TIMEOUT}s）")
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"Claude CLI 失敗: {err[:400]}")
    raw = (proc.stdout or "").strip()
    if not raw:
        raise RuntimeError("Claude CLI 無輸出")
    try:
        result = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"Claude CLI JSON 解析失敗: {e}") from e
    if result.get("type") != "result":
        raise RuntimeError(f"Claude CLI 回傳非 result 事件: {result.get('type') or 'unknown'}")
    if result.get("is_error"):
        msg = result.get("result") or result.get("subtype") or "unknown error"
        raise RuntimeError(f"Claude CLI 錯誤: {msg[:400]}")
    return (result.get("result") or "").rstrip("\n"), None


def _iter_cli_stream(proc):
    """Yield text deltas parsed from Claude Code's stream-json output.

    Claude Code stream-json emits JSON-lines; we only care about `assistant` message
    events that carry a text `content_block_delta`-style payload or a full text block.
    """
    emitted = 0  # running count of chars already yielded
    buffer_text = ""
    result_event = None
    event_types = []  # for diagnostics when output ends up empty
    raw_lines_seen = 0
    raw_lines_unparsable = 0
    first_raw_preview = None

    for raw_line in proc.stdout:
        raw_lines_seen += 1
        if first_raw_preview is None:
            first_raw_preview = raw_line[:200]
        line = raw_line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except Exception:
            raw_lines_unparsable += 1
            continue

        etype = evt.get("type")
        event_types.append(etype)
        if etype == "result":
            result_event = evt
            continue
        # Partial streaming: {type:"stream_event", event:{type:"content_block_delta", delta:{type:"text_delta", text:"..."}}}
        if etype == "stream_event":
            inner = evt.get("event", {})
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text") or ""
                    if text:
                        buffer_text += text
                        emitted += len(text)
                        yield {"type": "token", "token": text}
            continue

        # Completed assistant message: reconcile in case partial events were missed.
        if etype == "assistant":
            if evt.get("error"):
                yield {"type": "error", "error": str(evt.get("error"))}
                return
            msg = evt.get("message", {}) or {}
            # Refusal / policy-block comes through as a synthetic assistant message
            # with stop_reason=stop_sequence/refusal and the refusal text in content.
            stop_reason = msg.get("stop_reason")
            if stop_reason in ("refusal", "stop_sequence") and msg.get("model") == "<synthetic>":
                blocks = msg.get("content", []) or []
                refuse_text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                if refuse_text:
                    yield {"type": "error", "error": f"Claude Code 拒答: {refuse_text[:400]}"}
                    return
            blocks = msg.get("content", []) or []
            full = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            if full and len(full) > emitted:
                tail = full[emitted:]
                buffer_text += tail
                emitted += len(tail)
                yield {"type": "token", "token": tail}

    if result_event is not None and result_event.get("is_error"):
        msg = result_event.get("result") or result_event.get("subtype") or "unknown error"
        yield {"type": "error", "error": f"Claude CLI 錯誤: {msg[:400]}"}
        return

    result_text = ""
    if result_event is not None:
        result_text = result_event.get("result") or ""
    final_text = buffer_text or result_text
    if not final_text:
        logger.warning(
            "Claude CLI stream 乾淨結束但無文字；raw_lines=%d unparsable=%d event_types=%s first_raw=%r",
            raw_lines_seen, raw_lines_unparsable, event_types[:30], first_raw_preview,
        )
    yield {"type": "done", "text": final_text, "usage": None}


def _cli_stream(prompt, system, model, max_tokens):
    if not _check_cli():
        yield {"type": "error", "error": f"找不到 Claude Code CLI：{CLAUDE_CLI_PATH}"}
        return

    cmd = _build_cli_cmd(system, model, stream_json=True)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except Exception as e:
        yield {"type": "error", "error": f"無法啟動 Claude CLI: {e}"}
        return

    # Drain stderr in a background thread so a full pipe buffer can't deadlock the
    # subprocess (Opus runs can emit warnings/telemetry to stderr continuously).
    stderr_chunks = []
    def _drain_stderr():
        try:
            for line in proc.stderr:
                stderr_chunks.append(line)
        except Exception:
            pass
    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    # Write stdin in a background thread. Windows pipe buffer is ~64KB; a large
    # prompt (>100KB) blocks the main thread on write, but the child is already
    # waiting on us to drain stdout → silent deadlock (CLI exits 0 with no output).
    stdin_err = []
    def _write_stdin():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except Exception as e:
            stdin_err.append(e)
            try:
                proc.stdin.close()
            except Exception:
                pass
    stdin_thread = threading.Thread(target=_write_stdin, daemon=True)
    stdin_thread.start()

    # Intercept `done` so we can gate it on the process's exit code.
    last_done = None
    saw_error = False
    killed_by_consumer = False
    token_count = 0
    stream_start = time.time()
    stderr_tail = ""
    try:
        try:
            for evt in _iter_cli_stream(proc):
                if evt.get("type") == "done":
                    last_done = evt
                    continue
                if evt.get("type") == "error":
                    saw_error = True
                if evt.get("type") == "token":
                    token_count += 1
                yield evt
        except GeneratorExit:
            killed_by_consumer = True
            proc.kill()
            raise
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        stderr_thread.join(timeout=2)
        stdin_thread.join(timeout=2)
        stderr_tail = "".join(stderr_chunks).strip()
        elapsed = time.time() - stream_start
        if killed_by_consumer:
            # Watchdog or downstream cancelled us — always log so callers can
            # see what the CLI was doing right before the kill (otherwise we
            # silently lose the only diagnostic signal we have).
            logger.warning(
                "Claude CLI 被外部中斷(watchdog/consumer): model=%s exit=%s "
                "prompt_len=%d elapsed=%.1fs tokens=%d stdin_err=%s "
                "stderr_len=%d stderr_tail=%r",
                model, proc.returncode, len(prompt), elapsed, token_count,
                stdin_err, len(stderr_tail), stderr_tail[-1500:],
            )
        else:
            empty_output = last_done is not None and not last_done.get("text")
            if empty_output or proc.returncode not in (0, None):
                logger.warning(
                    "Claude CLI 診斷: model=%s exit=%s prompt_len=%d "
                    "elapsed=%.1fs tokens=%d stdin_err=%s stderr_len=%d stderr=%r",
                    model, proc.returncode, len(prompt), elapsed, token_count,
                    stdin_err, len(stderr_tail), stderr_tail[:1500],
                )

    if proc.returncode not in (0, None):
        yield {"type": "error", "error": f"Claude CLI 異常結束 (exit={proc.returncode}): {stderr_tail[:200] or '無輸出'}"}
        return
    if saw_error:
        return
    if last_done is not None:
        # 乾淨退出但沒半個 token — 補上 stderr 讓上層知道是哪裡卡住
        if not last_done.get("text") and stderr_tail:
            yield {"type": "error", "error": f"Claude CLI 無輸出: {stderr_tail[:200]}"}
            return
        yield last_done
    else:
        yield {"type": "error", "error": "Claude CLI 無輸出"}


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------

def ai_call(prompt, system, model, max_tokens=1024, cache_prefix=None):
    if backend_name() == "cli":
        # CLI does not support cache_control; ignore the hint.
        return _cli_call(prompt, system, model, max_tokens)
    return _api_call(prompt, system, model, max_tokens, cache_prefix=cache_prefix)


def ai_stream(prompt, system, model, max_tokens=8192, cache_prefix=None):
    if backend_name() == "cli":
        yield from _cli_stream(prompt, system, model, max_tokens)
    else:
        yield from _api_stream(prompt, system, model, max_tokens, cache_prefix=cache_prefix)


# ---------------------------------------------------------------------------
# Watchdog + heartbeat wrapper
# ---------------------------------------------------------------------------

def _kill_generator_subprocess(gen, seen=None):
    """Best-effort kill for subprocesses hidden inside nested generators."""
    if gen is None:
        return False
    if seen is None:
        seen = set()
    obj_id = id(gen)
    if obj_id in seen:
        return False
    seen.add(obj_id)

    killed = False
    frame = getattr(gen, "gi_frame", None)
    if frame is not None:
        proc = frame.f_locals.get("proc")
        if proc is not None and hasattr(proc, "poll") and hasattr(proc, "kill"):
            try:
                if proc.poll() is None:
                    proc.kill()
                    killed = True
            except Exception:
                pass

    child = getattr(gen, "gi_yieldfrom", None)
    if child is not None:
        killed = _kill_generator_subprocess(child, seen) or killed
    return killed


def with_watchdog(source, idle_timeout=90, heartbeat_every=10):
    """Wrap an ai_stream() generator with idle watchdog + periodic heartbeats.

    Yields:
        - all events from `source` unchanged (token / done / error)
        - {"type": "heartbeat", "idle_secs": int, "elapsed_secs": int} every
          `heartbeat_every` seconds while no token has arrived
        - {"type": "error", "error": "..."} when no token arrives for
          `idle_timeout` seconds; upstream generator is closed (which kills
          the CLI subprocess via its GeneratorExit handler).

    Works for both API and CLI streams. The producer runs on a daemon thread
    so the consumer can abandon it cleanly on watchdog trip.
    """
    q = queue.Queue()
    sentinel = object()

    def producer():
        try:
            for evt in source:
                q.put(evt)
        except Exception as e:
            q.put({"type": "error", "error": f"stream producer crashed: {e}"})
        finally:
            q.put(sentinel)

    t = threading.Thread(target=producer, daemon=True)
    t.start()

    start = time.time()
    last_token = start
    last_heartbeat = start

    while True:
        try:
            evt = q.get(timeout=1.0)
        except queue.Empty:
            now = time.time()
            idle = now - last_token
            if idle >= idle_timeout:
                # Try to close the source — for _cli_stream this triggers
                # GeneratorExit → proc.kill(); for _api_stream it exits the
                # SDK stream context. The producer thread will then drain.
                killed = _kill_generator_subprocess(source)
                try:
                    source.close()
                except Exception:
                    pass
                logger.warning(
                    "ai_stream watchdog tripped: no token for %ds (elapsed=%ds, killed_proc=%s)",
                    int(idle), int(now - start), killed,
                )
                yield {
                    "type": "error",
                    "error": f"AI 串流空閒 {int(idle)}s 仍無回應,已中斷(可能是 CLI subprocess 卡住或模型 queue 排隊過久)",
                }
                return
            if now - last_heartbeat >= heartbeat_every:
                yield {
                    "type": "heartbeat",
                    "idle_secs": int(idle),
                    "elapsed_secs": int(now - start),
                }
                last_heartbeat = now
            continue

        if evt is sentinel:
            return

        if evt.get("type") == "token":
            last_token = time.time()

        yield evt
