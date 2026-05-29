"""Tests for ai.py pure functions (no AI backend / no DB needed)."""

import ai


def test_parse_coin_draft_basic():
    text = """===NARRATIVE===
This is the narrative.

===TIMELINE===
- 10:00 — first mention

===KOL_CONSENSUS===
Strong bullish.

===ARCHETYPE===
Pump-without-relay."""
    out = ai.parse_coin_draft(text)
    assert out["NARRATIVE"] == "This is the narrative."
    assert "first mention" in out["TIMELINE"]
    assert out["KOL_CONSENSUS"] == "Strong bullish."
    assert out["ARCHETYPE"] == "Pump-without-relay."


def test_parse_coin_draft_skips_data_insufficient():
    text = """===NARRATIVE===
Real content.

===TIMELINE===
資料不足"""
    out = ai.parse_coin_draft(text)
    assert "NARRATIVE" in out
    assert "TIMELINE" not in out, "資料不足 sentinel should be filtered"


def test_parse_coin_draft_empty():
    assert ai.parse_coin_draft("") == {}
    assert ai.parse_coin_draft(None) == {}


def test_split_profile_output_with_marker():
    raw = "markdown body\n\n===JSON===\n{\"a\": 1}"
    md, js = ai.split_profile_output(raw)
    assert md == "markdown body"
    assert js == '{"a": 1}'


def test_split_profile_output_no_marker():
    md, js = ai.split_profile_output("just markdown")
    assert md == "just markdown"
    assert js is None


def test_split_profile_output_empty():
    md, js = ai.split_profile_output("")
    assert md == ""
    assert js is None


def test_get_profile_known_and_unknown():
    p1 = ai.get_profile("group_chat")
    assert "system" in p1 and "template" in p1
    # Unknown profile falls back to default
    p2 = ai.get_profile("nonexistent_profile")
    assert p2 is p1


def test_wallet_auto_fallback_summary_truncates():
    text = "x" * (ai.AUTO_SUMMARIZE_WALLET_HARD_CAP + 100)
    out = ai._wallet_auto_fallback_summary(text, "too large")
    assert "AI auto-summary skipped: too large" in out
    assert "Truncated deterministic wallet rollup" in out
    assert out.count("x") == ai.AUTO_SUMMARIZE_WALLET_HARD_CAP


class _Row(dict):
    def keys(self):
        return super().keys()


def test_format_structured_summary_lines_reads_radar_schema():
    row = _Row(
        date="2026-05-10",
        chat_name="看推關注群",
        summary_slot="08:00",
        summary_json="""{
          "checklist": [
            {"priority": "立刻查", "target": "@lienfiapp", "action": "找 CA", "why_now": "第 5 天升級"}
          ],
          "radar": [
            {"target": "$MYTHOS", "status": "NEW", "strength": "B", "why_now": "CA 同框", "next_step": "查合約"}
          ],
          "needs_context": [
            {"clue": "0xaf1e", "missing": "項目名"}
          ],
          "expired": [
            {"target": "@FerrymanTCG", "reason": "已 x3"}
          ]
        }""",
    )
    out = ai._format_structured_summary_lines([row])
    text = "\n".join(out)
    assert "待查:立刻查 @lienfiapp" in text
    assert "雷達:$MYTHOS [NEW/B]" in text
    assert "缺口:0xaf1e" in text
    assert "過期:@FerrymanTCG" in text
