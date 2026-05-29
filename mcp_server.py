"""tg-fetcher MCP server — stdio entry point.

Run standalone:
    python mcp_server.py

Register in ~/.claude.json (or ~/.config/claude/settings.json):
    {
      "mcpServers": {
        "tg-fetcher": {
          "command": "python",
          "args": ["/absolute/path/to/tg-fetcher/mcp_server.py"]
        }
      }
    }

The server reads the same `tg_memory.db` the Flask app uses, but opens it
read-only so MCP tool handlers can never accidentally mutate state.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from mcp_tools.tools_authors import tg_author_resolve, tg_author_profile
from mcp_tools.tools_messages import tg_messages_by_author, tg_messages_search
from mcp_tools.tools_addresses import tg_extract_addresses
from mcp_tools.tools_chats import tg_chat_summary, tg_list_chats
from mcp_tools.tools_coins import tg_coin_mentions
from mcp_tools.tools_wallet_match import tg_match_wallet_for_author


mcp = FastMCP("tg-fetcher")


@mcp.tool()
def author_resolve(query: str, limit: int = 10) -> dict[str, Any]:
    """Fuzzy-match a name / @username against TG senders (incl. historical
    aliases). Returns sender_id candidates ranked by msg_count.

    Use this first when the user asks about a person by name. Pick the
    sender_id from the top hit and pass it to other tools.
    """
    return tg_author_resolve(query=query, limit=limit)


@mcp.tool()
def author_profile(
    sender_id: int | None = None,
    name: str | None = None,
    top_chats: int = 10,
    top_cas: int = 20,
) -> dict[str, Any]:
    """Full profile of one sender: aliases, top active chats, most-mentioned
    CAs, msg count, first/last seen. Pass either sender_id or name.
    """
    return tg_author_profile(
        sender_id=sender_id, name=name,
        top_chats=top_chats, top_cas=top_cas,
    )


@mcp.tool()
def messages_by_author(
    sender_id: int,
    since: str | None = None,
    until: str | None = None,
    chat_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """All messages from one sender, newest first. Dates use ISO format
    (`YYYY-MM-DD` or full datetime; comparisons are lexical).
    """
    return tg_messages_by_author(
        sender_id=sender_id, since=since, until=until,
        chat_id=chat_id, limit=limit,
    )


@mcp.tool()
def messages_search(
    query: str,
    sender_id: int | None = None,
    chat_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Full-text search across all TG messages. Optional filters: sender,
    chat, date range. Uses FTS5.
    """
    return tg_messages_search(
        query=query, sender_id=sender_id, chat_id=chat_id,
        since=since, until=until, limit=limit,
    )


@mcp.tool()
def extract_addresses(
    sender_id: int | None = None,
    name: str | None = None,
    since: str | None = None,
    until: str | None = None,
    chat_id: str | None = None,
    scan_limit: int = 2000,
    top: int = 50,
) -> dict[str, Any]:
    """Pull every CA / wallet-address-shaped string out of a sender's recent
    messages, dedup, and rank by frequency. Returns one-line context snippets.

    Mixes contract addresses and EOA wallet addresses — they're
    indistinguishable from text alone.
    """
    return tg_extract_addresses(
        sender_id=sender_id, name=name, since=since, until=until,
        chat_id=chat_id, scan_limit=scan_limit, top=top,
    )


@mcp.tool()
def chat_summary(
    chat_id: str,
    date: str | None = None,
    slot: str | None = None,
    limit: int = 7,
) -> dict[str, Any]:
    """Read pre-computed daily summaries for a chat. If `date` omitted,
    returns the latest `limit` summaries newest-first.
    """
    return tg_chat_summary(chat_id=chat_id, date=date, slot=slot, limit=limit)


@mcp.tool()
def list_chats(limit: int = 50) -> dict[str, Any]:
    """List all TG chats by msg volume — use to discover chat_id values."""
    return tg_list_chats(limit=limit)


@mcp.tool()
def coin_mentions(
    query: str,
    limit_per_chat: int = 3,
    max_chats: int = 30,
    days: int | None = None,
) -> dict[str, Any]:
    """Find TG mentions for a ticker / name / CA. Auto-detects query shape
    (EVM CA, Solana CA, free text) and returns per-chat groupings plus
    related events.
    """
    return tg_coin_mentions(
        query=query, limit_per_chat=limit_per_chat,
        max_chats=max_chats, days=days,
    )


@mcp.tool()
def match_wallet_for_author(
    sender_id: int | None = None,
    name: str | None = None,
    cas: list[str] | None = None,
    default_chain: str = "base",
    max_cas: int = 5,
    window_before_secs: int = 3600,
    window_after_secs: int = 300,
    top_n_buyers: int = 50,
    min_call_matches: int = 2,
) -> dict[str, Any]:
    """Infer the wallet(s) belonging to a TG sender by cross-referencing
    their stated trades against on-chain early-buyer data from chain-flow.

    Requires chain-flow service running (default http://127.0.0.1:8787 — set
    CHAIN_FLOW_BASE_URL env var to override).

    Picks CAs the sender mentioned alongside a buy verb (aped / filled /
    sniped / etc.), and looks for wallets that appear as early buyers across
    multiple of those calls.
    """
    return tg_match_wallet_for_author(
        sender_id=sender_id, name=name, cas=cas,
        default_chain=default_chain, max_cas=max_cas,
        window_before_secs=window_before_secs,
        window_after_secs=window_after_secs,
        top_n_buyers=top_n_buyers,
        min_call_matches=min_call_matches,
    )


if __name__ == "__main__":
    mcp.run()
