"""Unit tests for the holdings reconcile logic (pure, no DB / no network)."""

import wallet_aggregator as wa


def _msg(ts, text):
    return {"date": "2026-05-29 00:00:00", "timestamp": ts, "text": text}


WALLET_A = "0xaaaa000000000000000000000000000000000001"
CA = "0x1111111111111111111111111111111111111111"


def _buy(ts, usd, holds, first=False):
    emoji = "🆕🟢" if first else "🟢"
    return _msg(ts, f"""{emoji} [BUY PEPE](https://etherscan.io/tx/0x{ts}) (ETHEREUM)
[**whaleA**](https://etherscan.io/address/{WALLET_A}) swapped **1.0** (${usd}) [**ETH**] for **1.0** (${usd}) [**PEPE**]
✊Holds: {holds}
🔗 **#PEPE** | **MC**: $10M
`{CA}`""")


def _sell(ts, usd, sold_pct, holds, pnl="+1,000", pnl_pct="+20"):
    sign = pnl[0] if pnl and pnl[0] in "+-" else "+"
    emoji = "📈" if sign == "+" else "📉"
    return _msg(ts, f"""🔴 [SELL PEPE](https://etherscan.io/tx/0x{ts}) (ETHEREUM)
[**whaleA**](https://etherscan.io/address/{WALLET_A}) swapped **1.0** (${usd}) [**PEPE**] for **1.0** (${usd}) [**ETH**]
➖Sold: {sold_pct}%
{emoji}PnL: **${pnl}** ({pnl_pct}%)
✊Holds: {holds}
🔗 **#PEPE** | **MC**: $12M
`{CA}`""")


def test_amount_value_parses_k_m_b():
    assert wa.holder_amount_value("0.00") == 0.0
    assert wa.holder_amount_value("500K") == 500_000
    assert wa.holder_amount_value("1.2M") == 1_200_000
    assert wa.holder_amount_value("1,200") == 1200
    assert wa.holder_amount_value("garbage") == 0.0


def test_status_buy_is_holding():
    ev = wa.parse_message(_buy(100, "5,000", "1.2M (3.45%)", first=True))
    assert wa.wallet_holding_status(ev) == (wa.HOLDING, "latest_buy")


def test_status_full_sell_is_exited():
    ev = wa.parse_message(_sell(100, "5,000", 100, "0.00 (0.00%)", pnl="-100", pnl_pct="-10"))
    status, _ = wa.wallet_holding_status(ev)
    assert status == wa.EXITED


def test_status_partial_sell_with_holds_is_holding():
    ev = wa.parse_message(_sell(100, "3,000", 40, "700K (2.0%)"))
    status, reason = wa.wallet_holding_status(ev)
    assert status == wa.HOLDING
    assert reason in ("sell_reports_holds", "partial_sell")


def test_reentry_round_trip_flagged_as_holding():
    """BUY -> SELL(partial) -> BUY: sold then re-bought, still holding."""
    msgs = [
        _buy(100, "5,000", "1.2M (3.45%)", first=True),
        _sell(200, "3,000", 40, "700K (2.0%)"),
        _buy(300, "2,000", "1.0M (3.0%)"),
    ]
    events, _ = wa._parse_messages(msgs)
    recs = wa.derive_holdings(events)
    assert len(recs) == 1
    r = recs[0]
    assert r.status == wa.HOLDING
    assert r.round_trip is True
    assert r.reentry is True
    assert r.n_buys == 2 and r.n_sells == 1
    assert wa.holding_status_label(r) == "賣後回補仍持有"
    assert r.last_action == "BUY"


def test_full_exit_after_round_trip_is_exited():
    """BUY -> BUY -> SELL 100%: ends flat regardless of earlier buys."""
    msgs = [
        _buy(100, "5,000", "1.2M (3.45%)", first=True),
        _buy(150, "1,000", "1.4M (3.6%)"),
        _sell(200, "9,000", 100, "0.00 (0.00%)", pnl="-100", pnl_pct="-10"),
    ]
    events, _ = wa._parse_messages(msgs)
    recs = wa.derive_holdings(events)
    assert len(recs) == 1
    assert recs[0].status == wa.EXITED
    assert wa.holding_status_label(recs[0]) == "已清倉"


def test_render_current_holdings_section_present():
    msgs = [
        _buy(100, "5,000", "1.2M (3.45%)", first=True),
        _sell(200, "3,000", 40, "700K (2.0%)"),
        _buy(300, "2,000", "1.0M (3.0%)"),
    ]
    out = wa.aggregate_token_flows(msgs, hours=8)
    assert "## CURRENT_HOLDINGS" in out
    assert "賣後回補仍持有" in out
    assert "holding=1" in out  # one active wallet still holding


def test_no_holdings_section_when_no_buy_sell():
    # APPROVE-only message produces no BUY/SELL events → no holdings section.
    approve = _msg(100, """🆗 [APPROVE PEPE](https://etherscan.io/tx/0x1) (ETHEREUM)
[**whaleA**](https://etherscan.io/address/%s) approved
`%s`""" % (WALLET_A, CA))
    events, _ = wa._parse_messages([approve])
    assert wa.derive_holdings(events) == []
