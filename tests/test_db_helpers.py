"""Pure-function tests for db.py helpers (no SQLite needed)."""

from datetime import datetime, timezone, timedelta

import db


def test_encode_decode_roundtrip_small():
    msgs = [
        {"id": 1, "date": "2026-04-25T10:00:00+00:00", "from": "Alice",
         "username": "alice", "text": "hi", "media": ""},
        {"id": 2, "date": "2026-04-25T10:01:00+00:00", "from": "Bob",
         "username": "bob", "text": "$PEPE pumping", "media": ""},
    ]
    payload = db.encode_raw_messages(msgs)
    assert isinstance(payload, str)
    decoded = db.decode_raw_messages(payload)
    assert len(decoded) == 2
    assert decoded[0]["from"] == "Alice"
    assert decoded[1]["text"] == "$PEPE pumping"


def test_encode_decode_roundtrip_large_uses_gzip():
    big = [{"id": i, "date": "2026-04-25T10:00:00+00:00",
            "from": f"user{i}", "text": "x" * 200, "media": ""}
           for i in range(50)]
    payload = db.encode_raw_messages(big)
    assert payload.startswith("gz:"), "large payloads should be gzip-encoded"
    decoded = db.decode_raw_messages(payload)
    assert len(decoded) == 50
    assert decoded[10]["from"] == "user10"


def test_encode_caps_at_500():
    msgs = [{"id": i, "text": "t", "from": "x"} for i in range(700)]
    payload = db.encode_raw_messages(msgs)
    decoded = db.decode_raw_messages(payload)
    assert len(decoded) == 500


def test_decode_handles_empty_and_garbage():
    assert db.decode_raw_messages("") == []
    assert db.decode_raw_messages(None) == []
    assert db.decode_raw_messages("not-json") == []


def test_decode_handles_legacy_plain_json():
    legacy = '[{"id":1,"text":"old"}]'
    assert db.decode_raw_messages(legacy) == [{"id": 1, "text": "old"}]


def test_detect_ca_type_evm():
    assert db.detect_ca_type("0x" + "a" * 40) == "evm"
    assert db.detect_ca_type("0x" + "A" * 40) == "evm"


def test_detect_ca_type_solana():
    # 44 chars base58 (no 0/O/I/l)
    sol = "DwK4nZ8vASnfsm5JaXmFLZcgGT3DKYzCNCvAqW3vAsRr"
    assert db.detect_ca_type(sol) == "solana"


def test_detect_ca_type_none():
    assert db.detect_ca_type("PEPE") is None
    assert db.detect_ca_type("") is None
    assert db.detect_ca_type(None) is None


def test_extract_cas_evm():
    text = "see CA 0xabcdefABCDEF1234567890123456789012345678 plus other"
    cas = db.extract_cas(text)
    assert len(cas) == 1
    assert cas[0].startswith("0x")


def test_extract_cas_dedup():
    addr = "0x" + "1" * 40
    text = f"first {addr} again {addr.upper()}"
    cas = db.extract_cas(text)
    # Same EVM addr (case-insensitive) should dedupe
    assert len(cas) == 1


def test_extract_cas_skips_pure_alpha_solana_lookalike():
    # All lowercase, no digits — should be filtered out as noise
    fake = "abcdefghjkmnpqrstuvwxyzabcdefghjkm"
    assert db.extract_cas(f"see {fake}") == []


def test_extract_cas_lowercases_evm():
    # EIP-55 checksum + raw lowercase forms must collapse to one entry,
    # and the output is the lowercase form (so downstream dict-keying works).
    checksum = "0x3722264AB15A1DfCe5A5af89E6547f7949A8ABa3"
    lower = checksum.lower()
    assert db.extract_cas(f"first {checksum} then {lower}") == [lower]


def test_is_system_contract():
    # Wrapped natives + sinks are blacklisted; real token CAs are not.
    assert db.is_system_contract("0x4200000000000000000000000000000000000006")  # Base WETH9
    assert db.is_system_contract("0x4200000000000000000000000000000000000006".upper())
    assert db.is_system_contract("0x000000000000000000000000000000000000dead")
    assert not db.is_system_contract("0x3722264ab15a1dfce5a5af89e6547f7949a8aba3")
    assert not db.is_system_contract("")
    assert not db.is_system_contract(None)


def test_cas_for_ticker_single_ca_kept():
    # Unambiguous: one CA in the message, ticker present → kept regardless
    # of distance.
    ca = "0x" + "a" * 40
    text = f"$LFI is the play. " + ("filler " * 30) + f"CA: {ca}"
    assert db.cas_for_ticker(text, "$LFI") == [ca]


def test_cas_for_ticker_drops_distant_co_occurrence():
    # Multi-CA message: only the CA near the ticker should survive.
    near = "0x" + "a" * 40
    far = "0x" + "b" * 40
    text = (
        f"$LFI {near} buy now. "
        + ("padding text padding text " * 8)  # ~210 chars > 60 threshold
        + f"unrelated mention {far}"
    )
    out = db.cas_for_ticker(text, "$LFI")
    assert near in out
    assert far not in out


def test_cas_for_ticker_keeps_nearby_ca_in_multi():
    # Wallet-log style: WETH right before ticker, real token right after.
    weth = "0x4200000000000000000000000000000000000006"
    real = "0x" + "c" * 40
    router = "0x" + "d" * 40
    text = f"swap {weth} -> $LFI ({real}) via {router}"
    # Without blacklist, all three would survive proximity; the integration
    # in _search_coin_by_ticker layers blacklist on top to drop weth/router.
    near = db.cas_for_ticker(text, "$LFI")
    assert real in near
    # WETH and router are both within 60 chars of `$LFI` in this short line —
    # proximity alone keeps them; the blacklist is what excludes them later.
    assert weth in near
    assert router in near


def test_cas_for_ticker_word_boundary():
    # `LFI` inside a longer alphanumeric token should NOT count as a ticker
    # mention — otherwise `INFLATION` would treat any nearby CA as an LFI hit.
    ca = "0x" + "e" * 40
    text = f"INFLATION concerns aside, see {ca}"
    # Single-CA path returns the CA regardless; force multi-CA to test
    # word-boundary matters.
    other = "0x" + "f" * 40
    text2 = f"INFLATION concerns aside, see {ca}. Separately {other}."
    # No real LFI ticker in the text → ticker_positions empty → fallback
    # returns all CAs (permissive).
    assert set(db.cas_for_ticker(text2, "$LFI")) == {ca, other}


def test_build_fts_query_basic():
    assert db.build_fts_query("PEPE") == "PEPE*"
    assert db.build_fts_query("PEPE coin") == "PEPE* coin*"


def test_build_fts_query_strips_special_chars():
    # Special chars should be stripped, leaving the safe tokens
    out = db.build_fts_query("PEPE: $1.5M (great!)")
    # Tokens should be present, in some order
    assert "PEPE*" in out
    assert "1*" in out or "1.5M*" not in out  # period is stripped


def test_build_fts_query_empty():
    assert db.build_fts_query("") == ""
    assert db.build_fts_query(None) == ""
    assert db.build_fts_query("...") == ""


def test_build_fts_query_custom_joiner():
    assert db.build_fts_query("a b", joiner=" OR ") == "a* OR b*"


def test_to_taipei_str_aware_utc_to_taipei():
    # 10:00 UTC == 18:00 UTC+8
    iso = "2026-04-25T10:00:00+00:00"
    assert db.to_taipei_str(iso) == "2026-04-25 18:00"


def test_to_taipei_str_naive_passthrough():
    # Naive strings are assumed to already be UTC+8 — returned unshifted
    assert db.to_taipei_str("2026-04-25 18:30:00") == "2026-04-25 18:30"


def test_to_taipei_str_datetime_object():
    dt = datetime(2026, 4, 25, 10, 0, tzinfo=timezone.utc)
    assert db.to_taipei_str(dt) == "2026-04-25 18:00"


def test_to_taipei_str_empty():
    assert db.to_taipei_str("") == ""
    assert db.to_taipei_str(None) == ""


def test_to_taipei_str_unparseable_truncates():
    assert db.to_taipei_str("not a date at all") == "not a date at al"


def test_normalize_memory_import_payload_accepts_radar_schema():
    data = {
        "report": {
            "title": "TG 新項目雷達",
            "channel": "看推關注群",
            "platform": "Telegram",
            "time_range": {"duration_hours": 8},
            "message_count": 10,
        },
        "checklist": [
            {"priority": "立刻查", "target": "@lienfiapp", "action": "找 CA", "why_now": "第 5 天升級"}
        ],
        "radar": [
            {"target": "$MYTHOS", "status": "NEW", "strength": "B", "why_now": "CA 同框"}
        ],
        "needs_context": [
            {"clue": "0xaf1e", "missing": "項目名", "next_step": "查鏈上"}
        ],
        "expired": [
            {"target": "@FerrymanTCG", "reason": "已 x3"}
        ],
    }

    payload, kind, err = db.normalize_memory_import_payload(data)

    assert err is None
    assert kind == "analysis_report"
    assert "【新項目雷達】" in payload["summaries"][0]["summary"]
    assert any("$MYTHOS" in e["title"] for e in payload["events"])
    assert any("[待查:立刻查]" in n["content"] for n in payload["notes"])
    assert "$MYTHOS" in payload["watchlist"]
    assert "@lienfiapp" in payload["watchlist"]
