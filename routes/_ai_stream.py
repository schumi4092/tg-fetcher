"""Shared AI streaming helpers for SSE-style routes and pipelines."""

import ai_backend


def stream_ai_events(prompt, system, model, *, max_tokens=8192,
                     idle_timeout=300, heartbeat_every=10,
                     cache_prefix=None, heartbeat_message=None,
                     event_wrapper=None, include_usage=False):
    """Yield token/progress dicts while streaming, then return (text, error).

    `heartbeat_message` may be a callable:
        fn(has_text: bool, elapsed: int, idle: int) -> dict | None
    """
    text_acc = []
    stream_error = None
    final_usage = None

    def emit(event):
        return event_wrapper(event) if event_wrapper else event

    try:
        source = ai_backend.ai_stream(
            prompt, system, model,
            max_tokens=max_tokens,
            cache_prefix=cache_prefix,
        )
        for evt in ai_backend.with_watchdog(
            source,
            idle_timeout=idle_timeout,
            heartbeat_every=heartbeat_every,
        ):
            etype = evt.get("type")
            if etype == "token":
                token = evt.get("token", "")
                if token:
                    text_acc.append(token)
                    yield emit({"type": "token", "token": token})
            elif etype == "heartbeat":
                if heartbeat_message:
                    progress = heartbeat_message(
                        bool(text_acc),
                        evt.get("elapsed_secs", 0),
                        evt.get("idle_secs", 0),
                    )
                    if progress:
                        yield emit(progress)
            elif etype == "error":
                stream_error = evt.get("error", "unknown")
                break
            elif etype == "done":
                final_usage = evt.get("usage")
    except Exception as e:
        stream_error = str(e)
    result = ("".join(text_acc).strip(), stream_error)
    if include_usage:
        return result + (final_usage,)
    return result
