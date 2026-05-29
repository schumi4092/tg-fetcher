import group_aggregator as ga


def _msg(i, text, sender="alice", sender_id=1):
    return {
        "id": i,
        "date": f"2026-05-20T00:{i % 60:02d}:00+00:00",
        "from": sender,
        "sender_id": sender_id,
        "text": text,
        "media": "",
    }


def test_group_rollup_keeps_entities_and_timeline():
    ca = "0x" + "a" * 40
    msgs = [
        _msg(1, "gm"),
        _msg(2, f"$ABC deploy CA {ca} FDV 500K", sender="trusted", sender_id=42),
        _msg(3, "$ABC now 3x, liquidity looks thin"),
        _msg(4, "random chatter"),
    ]
    out = ga.build_group_chat_rollup(
        msgs,
        trust_map={42: "trusted"},
        target_chars=20000,
        max_entity_samples=3,
        max_timeline_samples=4,
    )
    assert "GROUP_CHAT_SIGNAL_ROLLUP" in out
    assert "## TOP_TICKERS" in out
    assert "### $ABC" in out
    assert ca in out
    assert "⭐trusted" in out
    assert "## TIMELINE_SAMPLE" in out


def test_group_rollup_respects_target_chars():
    msgs = [
        _msg(i, f"$TOKEN{i % 9} CA 0x{'b' * 40} FDV {i}K launch alpha " + ("x" * 200))
        for i in range(120)
    ]
    out = ga.build_group_chat_rollup(
        msgs,
        target_chars=12000,
        max_high_signal_lines=80,
    )
    assert len(out) <= 12100
    assert "GROUP_CHAT_SIGNAL_ROLLUP" in out
