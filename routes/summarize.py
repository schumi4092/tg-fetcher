"""SSE summarize endpoint — thin wrapper around SummarizePipeline."""

from flask import Blueprint, jsonify

from routes._shared import get_json_body, sse_event, sse_response
from routes._summarize_pipeline import SummarizePipeline


bp = Blueprint("summarize", __name__)


@bp.route("/api/summarize/stream", methods=["POST"])
def api_summarize_stream():
    data = get_json_body()
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"error": "沒有訊息"}), 400

    pipeline = SummarizePipeline(
        messages=messages,
        chat_id=data.get("chat_id", ""),
        chat_name=data.get("chat_name", "未知"),
        hours=data.get("hours", 8),
        model_key=data.get("model", "sonnet"),
        save_to_memory=data.get("save_to_memory", True),
    )

    def generate():
        for evt in pipeline.run():
            yield sse_event(evt)

    return sse_response(generate)
