"""SummarizePipeline — orchestrates /api/summarize/stream as a sequence of phases.

Replaces the 230-line generator that lived inline in the route. Each phase is
a small generator method that yields SSE events; phases can short-circuit on
error via `_halt(msg)`. State (chat metadata, profile, prompt, summary, etc.)
lives on `self` so phases can hand off without long arg lists.

Why a class:
  - Each phase is now individually testable
  - Reading the flow top-to-bottom in `run()` is the whole pipeline
  - Adding a new post-hook means one new method, not editing a 200-line block

Public surface:
    pipeline = SummarizePipeline(messages, chat_id, chat_name, hours,
                                 model_key, save_to_memory)
    for evt in pipeline.run():
        yield sse_event(evt)
"""

import json
from datetime import date

from config import (
    AUTO_SUMMARIZE_IDLE_TIMEOUT_SECS,
    AUTO_SUMMARIZE_WALLET_MAX_BUY_ITEMS,
    AUTO_SUMMARIZE_WALLET_MAX_MULTI_ITEMS,
    AUTO_SUMMARIZE_WALLET_MAX_SELL_ITEMS,
    AUTO_SUMMARIZE_WALLET_MAX_TRANSFER_ITEMS,
    AUTO_SUMMARIZE_WALLET_MAX_UNPARSED_ITEMS,
    DIRECT_LIMIT,
    MODEL_OPUS,
    MODEL_SHORT_NAMES,
    MODEL_SONNET,
    logger,
)
from db import get_db_ctx
from routes._prefilter import apply_noise_prefilter
import ai
import ai_backend
import wallet_aggregator
from routes._ai_stream import stream_ai_events


class _PipelineHalt(Exception):
    """Raised to short-circuit the pipeline; the error event is on self."""


class SummarizePipeline:
    """Streamed summarization pipeline.

    Drives messages → profile resolution → msg_text prep (LLM compress
    or wallet aggregate) → prompt build → AI stream → save → post-hooks.
    """

    def __init__(self, messages, chat_id, chat_name, hours, model_key, save_to_memory):
        self.messages = messages
        self.chat_id = chat_id
        self.chat_name = chat_name
        self.hours = hours
        self.save_to_memory = save_to_memory
        self.summarize_model = MODEL_OPUS if model_key == "opus" else MODEL_SONNET
        self.model_label = MODEL_SHORT_NAMES.get(self.summarize_model, "Sonnet")
        self.backend_label = ai_backend.backend_name()

        # Filled progressively by phases. None until set.
        self.profile_name = ai.DEFAULT_PROFILE
        self.profile = None
        self.msg_text = None
        self.prompt = None
        self.cache_prefix = None
        self.summary = None
        self.streamed_tokens = []
        self.final_usage = None
        self.summary_id = None
        self.summary_date = date.today().isoformat()
        self.profile_json = None
        self.profile_json_valid = None
        self.profile_json_pending = False
        self.events_written = []
        self.sentiment = None
        self.save_error = None
        self._error_event = None

    # --- helpers -----------------------------------------------------------

    def _halt(self, msg):
        """Record a terminal error event and abort the pipeline."""
        self._error_event = {"type": "error", "error": msg}
        raise _PipelineHalt()

    def _progress(self, msg, percent=None):
        evt = {"type": "progress", "msg": msg}
        if percent is not None:
            evt["progress"] = percent
        return evt

    # --- phases ------------------------------------------------------------

    def _validate(self):
        """Phase 1 — make sure AI backend is up before doing any work."""
        yield self._progress(f"📨 準備訊息 (backend={self.backend_label})...", percent=5)
        if not ai_backend.ai_available():
            self._halt("AI backend 未就緒（檢查 CLAUDE_API_KEY 或 claude CLI）")

    def _resolve_profile(self):
        """Phase 2 — pick prompt profile from chat's category. wallet_log is special.
        Mutates `self.profile_name` and `self.profile`. Doesn't yield."""
        if self.chat_id:
            with get_db_ctx() as conn:
                row = conn.execute("""
                    SELECT cc.prompt_profile
                    FROM chat_category_map m
                    JOIN chat_categories cc ON cc.id = m.category_id
                    WHERE m.chat_id = ?
                """, (str(self.chat_id),)).fetchone()
                if row and row["prompt_profile"] in ai.PROFILES:
                    self.profile_name = row["prompt_profile"]
        self.profile = ai.get_profile(self.profile_name)

    def _prefilter_noise(self):
        """Phase 2.5 — drop low-signal events for noisy broadcast chats."""
        filtered, stats = apply_noise_prefilter(self.messages, self.chat_id)
        if stats is None:
            return
        self.messages = filtered
        if stats["dropped"]:
            yield self._progress(
                f"🧹 訊源過濾:留 {stats['kept']} / 丟 {stats['dropped']} "
                f"(原 {stats['total']})",
                percent=8,
            )

    def _prepare_msg_text(self):
        """Phase 3 — wallet_log uses deterministic aggregator, others LLM-compress
        when the prompt would exceed the per-profile direct_limit."""
        if self.profile_name == "wallet_log":
            yield self._progress(f"🧮 錢包事件聚合 {len(self.messages)} 則(無 LLM)...", percent=30)
            self.msg_text = wallet_aggregator.aggregate_events(
                self.messages,
                hours=self.hours,
                max_buy_items=AUTO_SUMMARIZE_WALLET_MAX_BUY_ITEMS,
                max_sell_items=AUTO_SUMMARIZE_WALLET_MAX_SELL_ITEMS,
                max_multi_items=AUTO_SUMMARIZE_WALLET_MAX_MULTI_ITEMS,
                max_transfer_items=AUTO_SUMMARIZE_WALLET_MAX_TRANSFER_ITEMS,
                max_unparsed_items=AUTO_SUMMARIZE_WALLET_MAX_UNPARSED_ITEMS,
            )
            yield self._progress("✓ 聚合完成,進入深度分析...", percent=55)
            return

        lines = ai.prepare_lines(self.messages)
        if not lines:
            # Special early-exit case: nothing text-y to summarize. Yield a
            # `done` event the runner can treat as terminal — we do NOT halt
            # via _halt because that's reserved for errors.
            self._error_event = {"type": "done", "summary": "沒有文字訊息可供總結。",
                                 "saved": False, "events": [], "sentiment": None}
            raise _PipelineHalt()

        total_chars = sum(len(l) for l in lines)
        direct_limit = self.profile.get("direct_limit", DIRECT_LIMIT)
        if total_chars <= direct_limit:
            self.msg_text = "\n".join(lines)
            yield self._progress("📝 準備直接分析...", percent=20)
        else:
            chunks = ai.split_chunks(lines)
            yield self._progress(f"⚡ 並行壓縮 {len(chunks)} 批...", percent=10)
            compressed = None
            for prog in ai.compress_chunks_parallel(
                chunks, self.chat_name,
                compress_system=self.profile.get("compress_system"),
            ):
                if prog["results"] is not None:
                    compressed = prog["results"]
                else:
                    pct = 10 + int(50 * prog["done"] / max(1, prog["total"]))
                    yield self._progress(
                        f"⚡ Sonnet 壓縮中 {prog['done']}/{prog['total']}...",
                        percent=pct,
                    )
            self.msg_text = "\n\n".join(compressed)

    def _build_prompt(self):
        """Phase 4 — render the profile template + work out the cache prefix."""
        yield self._progress(
            f"🧠 {self.model_label} 深度分析（{self.profile['label']}）...",
            percent=65,
        )
        fmt_kwargs = {
            "chat_name": self.chat_name,
            "hours": self.hours,
            "count": len(self.messages),
            "msg_text": self.msg_text,
            "coin_profile_context": ai.build_coin_profile_context(self.messages),
        }
        if self.profile["needs_history"]:
            fmt_kwargs["history_context"] = ai.build_history_context(self.chat_id, days=7)
            fmt_kwargs["date_str"] = self.summary_date

        self.prompt = self.profile["template"].format(**fmt_kwargs)

        # Cache prefix only valid when the volatile msg_text sits at the very
        # end of the rendered prompt — otherwise the prefix wouldn't match.
        if self.profile.get("cache_static_prefix"):
            tail = fmt_kwargs.get("msg_text") or ""
            if tail and self.prompt.endswith(tail):
                self.cache_prefix = self.prompt[: -len(tail)]

    def _stream_summary(self):
        """Phase 5 — stream the AI summary, yielding tokens + heartbeats."""
        def heartbeat(has_text, elapsed, idle):
            if has_text:
                return {
                    "type": "progress",
                    "msg": (f"🧠 {self.model_label} 生成中…(已 {elapsed}s,"
                            f"上次 token {idle}s 前)"),
                }
            return self._progress(
                f"🧠 {self.model_label} 等待第一個 token…(已等 {elapsed}s)",
                percent=65,
            )

        def capture(event):
            if event.get("type") == "token":
                self.streamed_tokens.append(event.get("token", ""))
            return event

        self.summary, stream_error, self.final_usage = yield from stream_ai_events(
            self.prompt, self.profile["system"], self.summarize_model,
            max_tokens=self.profile.get("max_tokens", 8192),
            idle_timeout=AUTO_SUMMARIZE_IDLE_TIMEOUT_SECS,
            heartbeat_every=10,
            cache_prefix=self.cache_prefix,
            heartbeat_message=heartbeat,
            event_wrapper=capture,
            include_usage=True,
        )

        if stream_error or not self.summary:
            logger.warning(
                "AI summarize failed: model=%s backend=%s stream_error=%s streamed_chars=%s",
                self.model_label, self.backend_label, stream_error,
                len("".join(self.streamed_tokens)),
            )
            self._halt(f"{self.model_label} 總結失敗: {stream_error or 'no output'}")

        ai._log_cost(f"最終總結({self.model_label})", self.final_usage, self.summarize_model)
        # Bump hit_count on any trading rules the AI cited (`法則 #N` / `rule #N`).
        # Silent on errors — telemetry only, never blocks the summary.
        ai.update_rule_hits(self.summary)
        self._extract_legacy_json_block()

    def _extract_legacy_json_block(self):
        """Strip the legacy `===JSON===` tail (no profiles use it now, but kept
        for safety in case external profiles are loaded)."""
        json_cfg = self.profile.get("json_extract")
        if json_cfg:
            self.profile_json_pending = bool(self.save_to_memory)
            return
        if not self.profile["has_json_block"]:
            return
        markdown_part, json_text = ai.split_profile_output(self.summary)
        self.summary = markdown_part
        self.profile_json = json_text
        if json_text:
            try:
                json.loads(json_text)
                self.profile_json_valid = True
            except Exception as je:
                self.profile_json_valid = False
                logger.warning(
                    "profile=%s JSON tail malformed (%d chars): %s",
                    self.profile_name, len(json_text), je,
                )

    def _save_summary(self):
        """Phase 6 — persist the summary row, capture summary_id."""
        yield self._progress("💾 存入記憶庫...", percent=88)
        try:
            self.summary_id, self.summary_date = ai.save_daily_summary(
                self.chat_id, self.chat_name, self.hours, self.messages,
                self.summary, self.summary_date,
                summary_json=self.profile_json,
            )
        except Exception as e:
            self.save_error = str(e)

    def _submit_json_extract(self):
        """Phase 7a — kick off non-critical JSON sidecar extract in the background."""
        json_cfg = self.profile.get("json_extract")
        if not (self.summary_id and json_cfg):
            if json_cfg and not self.summary_id:
                self.profile_json_pending = False
            return
        yield self._progress("📦 結構化 JSON 已排入背景抽取...", percent=90)
        try:
            ai.submit_summary_json_extract(
                self.summary_id, self.profile_name, json_cfg, self.summary,
                self.chat_name, self.hours, len(self.messages), self.summary_date,
                self.summarize_model,
            )
            self.profile_json_pending = True
        except Exception:
            logger.exception("profile=%s JSON 背景排程失敗", self.profile_name)
            self.profile_json_pending = False

    def _extract_events(self):
        """Phase 7b — pull events out of the markdown summary."""
        post_hooks = set(self.profile.get("post_hooks", ("events", "sentiment", "embedding")))
        if "events" not in post_hooks:
            return
        yield self._progress("📌 提取關鍵事件...", percent=92)
        events = ai.ai_extract_events(self.summary, self.chat_name)
        if self.summary_id:
            try:
                self.events_written = ai.replace_summary_events(
                    self.summary_id, self.summary_date, self.chat_name, events
                )
            except Exception:
                logger.exception("寫入事件失敗 (summary=%s)", self.summary_id)
                self.events_written = []

    def _post_hooks(self):
        """Phase 7c — sentiment + embedding (fire-and-forget for embedding)."""
        post_hooks = set(self.profile.get("post_hooks", ("events", "sentiment", "embedding")))
        downstream = tuple(h for h in ("sentiment", "embedding") if h in post_hooks)
        if not (self.summary_id and downstream):
            return
        if "sentiment" in downstream:
            yield self._progress("🎭 分析情緒指標...", percent=96)
        self.sentiment = ai.post_summarize(
            self.summary, self.summary_id, self.chat_id, self.chat_name,
            hooks=downstream,
        )

    def _final_event(self):
        """Build the terminal `done` event. Called once at end of run()."""
        result = {
            "type": "done",
            "summary": self.summary,
            "saved": self.summary_id is not None,
            "events": self.events_written,
            "sentiment": self.sentiment,
            "profile": self.profile_name,
            "profile_json": self.profile_json,
            "profile_json_valid": self.profile_json_valid,
            "profile_json_pending": self.profile_json_pending,
        }
        if self.summary_id is not None:
            result["summary_id"] = self.summary_id
        if self.save_error:
            result["save_error"] = self.save_error
        return result

    # --- driver ------------------------------------------------------------

    def run(self):
        """Yields SSE-shaped event dicts. Drives every phase in order."""
        try:
            yield from self._validate()
            self._resolve_profile()  # plain method, mutates self
            yield from self._prefilter_noise()
            yield from self._prepare_msg_text()
            yield from self._build_prompt()
            yield from self._stream_summary()
            if self.save_to_memory:
                yield from self._save_summary()
                yield from self._submit_json_extract()
                yield from self._extract_events()
                yield from self._post_hooks()
            yield self._final_event()
        except _PipelineHalt:
            # _error_event is set on self before _halt() is called. May
            # actually be a `done` event for the no-text early-exit case.
            if self._error_event is not None:
                yield self._error_event
        except Exception as e:
            logger.exception("SummarizePipeline crashed")
            yield {"type": "error", "error": str(e)}
