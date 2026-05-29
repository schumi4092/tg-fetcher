"""Cross-blueprint helpers.

Tiny utilities used by 3+ blueprints. Anything single-blueprint-specific
stays in that blueprint's module.
"""

import json

from flask import request


def get_json_body():
    """Decode JSON body silently — returns {} on missing/invalid Content-Type."""
    return request.get_json(silent=True) or {}


def try_parse_json_object(text):
    """Parse `text` as a JSON object, tolerating preamble / trailing notes.

    The streaming AI sometimes prepends a Chinese sentence (e.g.
    『Twitter 搜尋權限未開放,以已取得資料建檔。』) before the JSON despite
    the prompt asking for bare JSON. Strict json.loads then fails. As a
    fallback we slice from the first `{` to the matching last `}` and retry.
    Returns the dict on success, or None if no valid JSON object is found.
    """
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def sse_event(obj):
    """Format an object as a single Server-Sent-Events `data: ...\\n\\n` frame."""
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def sse_response(generator_fn):
    """Wrap an SSE generator in a Flask Response with the required headers."""
    from flask import Response, stream_with_context
    return Response(
        stream_with_context(generator_fn()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
