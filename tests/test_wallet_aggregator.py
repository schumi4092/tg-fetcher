"""wallet_aggregator regex parser tests — Ray Orange markdown shapes."""

import wallet_aggregator as wa


# Real Ray Orange shape: wallet link uses [**Name**](url/address/0xADDR) form,
# address must be 32-64 alphanumerics for the regex to bite.
WALLET_ADDR = "0x" + "d" * 40
KOL_ADDR = "0x" + "1" * 40

SAMPLE_BUY = f"""🆕🟢 [BUY HAMILTON](https://etherscan.io/tx/0xabc) (ETHEREUM)

🔹[**Whale.eth**](https://etherscan.io/address/{WALLET_ADDR}) **Smart Whale**

swapped **0.5** ($1,200) [**ETH**](https://etherscan.io/token/0xeee) for **1,000,000** ($1,200) [**HAMILTON**](https://etherscan.io/token/0xfff)

🔗 **#HAMILTON** | **MC**: $1.2M | **LQ**: $45K | **Seen**: 2h: [link]

`0x1234567890abcdef1234567890abcdef12345678`"""


SAMPLE_SELL = f"""🔴 [SELL COPE](https://etherscan.io/tx/0xabc) (ETHEREUM)

🔹[**KOL Wallet**](https://etherscan.io/address/{KOL_ADDR}) **CT Whale**

swapped **2,000,000** ($5,000) [**COPE**](...) for **2.0** ($5,000) [**ETH**](...)

➖Sold: 80% | 📈PnL: **$+3,200** (+170.5%)
✊Holds: 500K (20.00%)

🔗 **#COPE** | **MC**: $850K | **Seen**: 6h: [link]

`0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`"""


def test_parse_buy_message():
    msg = {"text": SAMPLE_BUY, "timestamp": 1700000000, "date": "2026-04-25T10:00:00+00:00"}
    ev = wa.parse_message(msg)
    assert ev is not None
    assert ev.action == "BUY"
    assert ev.chain == "ETHEREUM"
    assert ev.token_symbol == "HAMILTON"
    assert ev.is_first_buy is True  # 🆕🟢 prefix
    assert ev.usd_value == 1200.0
    assert ev.mc_usd == 1_200_000.0
    assert ev.lq_usd == 45_000.0
    assert ev.token_ca == "0x1234567890abcdef1234567890abcdef12345678"
    # Two wallet-name regexes coexist; _TRACKED_WALLET_LINE_RE wins on this
    # shape and captures the trailing **bold** text after the link.
    assert ev.wallet_name == "Smart Whale"


def test_parse_sell_message_with_pnl():
    msg = {"text": SAMPLE_SELL, "timestamp": 1700000000, "date": "2026-04-25T10:00:00+00:00"}
    ev = wa.parse_message(msg)
    assert ev is not None
    assert ev.action == "SELL"
    assert ev.token_symbol == "COPE"
    assert ev.sold_pct == 80.0
    assert ev.pnl_usd == 3200.0
    assert ev.pnl_pct == 170.5
    assert ev.has_pnl is True
    assert ev.holds_pct == 20.0
    assert ev.holds_amount == "500K"


def test_parse_unrecognized_returns_none():
    assert wa.parse_message({"text": ""}) is None
    assert wa.parse_message({"text": "Just a normal chat message about something"}) is None
    assert wa.parse_message({}) is None


def test_aggregate_empty():
    out = wa.aggregate_events([])
    assert "no events" in out or "未識別" in out


def test_aggregate_buy_groups_by_wallet_token():
    msgs = [{"text": SAMPLE_BUY, "timestamp": 1700000000, "date": "2026-04-25T10:00:00+00:00"}] * 3
    out = wa.aggregate_events(msgs)
    assert "🟢 買入聚合" in out
    assert "HAMILTON" in out
    # 3 events same wallet+token = 1 group, 3 buys
    assert "3 筆" in out


def test_parse_date_fills_timestamp_for_auto_rows():
    msg = {"text": SAMPLE_BUY, "date": "2026-04-25T10:00:00+00:00"}
    ev = wa.parse_message(msg)
    assert ev is not None
    assert ev.timestamp > 0
    assert wa._hm(ev.timestamp) == "18:00"


def test_aggregate_auto_rows_show_time_without_timestamp():
    msgs = [{"text": SAMPLE_BUY, "date": "2026-04-25T10:00:00+00:00"}]
    out = wa.aggregate_events(msgs)
    assert "首買 ?" not in out
    assert "首買 18:00" in out


def test_aggregate_token_flows_is_compact_and_token_centric():
    msgs = [
        {"text": SAMPLE_BUY, "date": "2026-04-25T10:00:00+00:00"},
        {"text": SAMPLE_SELL, "date": "2026-04-25T10:05:00+00:00"},
    ]
    out = wa.aggregate_token_flows(msgs, hours=12, max_tokens=5)
    assert "WALLET_TOKEN_FLOW_ROLLUP" in out
    assert "TOKEN $HAMILTON" in out
    assert "TOKEN $COPE" in out
    assert "FLOW buy=" in out
    assert "TOP_BUY 1:" in out
    assert "TOP_SELL 1:" in out
    assert "WALLET_REALIZED_PNL" in out
    assert "18:00" in out


def _sell_with_pnl(amount_usd, pnl_usd, pnl_pct, minute):
    return SAMPLE_SELL.replace(
        "($5,000)", f"(${amount_usd:,})", 1
    ).replace(
        "($5,000)", f"(${amount_usd:,})", 1
    ).replace(
        "$+3,200", f"$+{pnl_usd:,}"
    ).replace(
        "+170.5%", f"+{pnl_pct}%"
    ), f"2026-04-25T10:{minute:02d}:00+00:00"


def test_repeated_sell_pnl_uses_latest_snapshot_not_sum():
    first, first_date = _sell_with_pnl(500, 100, 10.0, 0)
    second, second_date = _sell_with_pnl(700, 150, 15.0, 5)
    msgs = [
        {"text": first, "date": first_date},
        {"text": second, "date": second_date},
    ]

    compact = wa.aggregate_token_flows(msgs, hours=12, max_tokens=5)
    assert "realized_pnl=+$150" in compact
    assert "CT Whale: +$150" in compact
    assert "+$250" not in compact

    detailed = wa.aggregate_events(msgs, hours=12)
    assert "PnL +$150 (latest +15.0%)" in detailed
    assert "+$250" not in detailed


TRANSFER_OUT_ADDR = "0x" + "e" * 40

SAMPLE_TRANSFER_OUT_LARGE = f"""💸 [TRANSFER](https://etherscan.io/tx/0xabc) (ETHEREUM)

🔹[**Whale.eth**](https://etherscan.io/address/{TRANSFER_OUT_ADDR}) **Smart Whale**

transferred **0.5** ($1,500) [**ETH**](https://etherscan.io/token/0xeee)"""

SAMPLE_TRANSFER_OUT_SMALL = f"""💸 [TRANSFER](https://etherscan.io/tx/0xdef) (ETHEREUM)

🔹[**Whale.eth**](https://etherscan.io/address/{TRANSFER_OUT_ADDR}) **Smart Whale**

transferred **0.05** ($50) [**ETH**](https://etherscan.io/token/0xeee)"""


def test_transfer_alert_renders_above_threshold():
    msgs = [{"text": SAMPLE_TRANSFER_OUT_LARGE,
             "date": "2026-04-25T10:00:00+00:00"}]
    out = wa.aggregate_token_flows(msgs, hours=12, transfer_alert_usd=200)
    assert "TRANSFER_ALERTS" in out
    assert "$1.5K" in out
    assert "Smart Whale" in out


def test_transfer_alert_filters_below_threshold():
    msgs = [{"text": SAMPLE_TRANSFER_OUT_SMALL,
             "date": "2026-04-25T10:00:00+00:00"}]
    out = wa.aggregate_token_flows(msgs, hours=12, transfer_alert_usd=200)
    # $50 is below the $200 threshold — section should not appear at all.
    assert "TRANSFER_ALERTS" not in out


def test_transfer_alert_disabled_when_threshold_zero():
    msgs = [{"text": SAMPLE_TRANSFER_OUT_LARGE,
             "date": "2026-04-25T10:00:00+00:00"}]
    out = wa.aggregate_token_flows(msgs, hours=12, transfer_alert_usd=0)
    assert "TRANSFER_ALERTS" not in out


def test_transfer_alert_caps_at_max_alerts():
    addrs = [f"0x{i:040x}" for i in range(10)]
    msgs = []
    for i, addr in enumerate(addrs):
        text = (
            f"💸 [TRANSFER](https://etherscan.io/tx/0x{i}) (ETHEREUM)\n\n"
            f"🔹[**w{i}**](https://etherscan.io/address/{addr}) **wallet_{i}**\n\n"
            f"transferred **1** (${1000 + i}) [**ETH**](https://etherscan.io/token/0xeee)"
        )
        msgs.append({"text": text, "date": f"2026-04-25T10:0{i}:00+00:00"})
    out = wa.aggregate_token_flows(
        msgs, hours=12, transfer_alert_usd=200, max_transfer_alerts=3,
    )
    # Only the 3 biggest transfers should be listed; remaining 7 collapsed.
    assert "shown=3 total=10" in out
    assert "omitted_smaller_transfers=7" in out


def test_solana_chain_inferred_from_venue():
    sol_addr = "DwK4nZ8vASnfsm5JaXmFLZcgGT3DKYzCNCvAqW3vAsRr"  # 44 chars
    text = f"""🟢 [BUY MEME](https://solscan.io/tx/abc) on JUPITER

🔹[**Sol Whale**](https://solscan.io/account/{sol_addr}) **Whale**

swapped **1.0** ($150) [**SOL**](...) for **1,000** ($150) [**MEME**](...)

🔗 **#MEME** | **MC**: $500K | **Seen**: 1h: [link]"""
    ev = wa.parse_message({"text": text, "timestamp": 1700000000})
    assert ev is not None
    assert ev.chain == "SOLANA"
