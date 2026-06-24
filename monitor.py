#!/usr/bin/env python3
"""
Sports Market Trend Monitor
Compares trending sports topics on Kalshi & Polymarket vs. Freeplay (ROLR).
Posts gaps to Slack if found.

Required env vars:
  SLACK_WEBHOOK_URL  -- Slack incoming webhook URL

Usage:
  python monitor.py
"""

import json
import os
import sys
from difflib import SequenceMatcher

import requests

# ── Config ────────────────────────────────────────────────────────────────────

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

KALSHI_BASE     = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_BASE = "https://gamma-api.polymarket.com"
FREEPLAY_BASE   = "https://freeplay.rolr.com/api/trpc"

# Freeplay uses generic league names; map both directions for matching
LEAGUE_ALIASES = {
    "nba":                    "pro basketball",
    "nfl":                    "pro football",
    "mlb":                    "pro baseball",
    "nhl":                    "pro hockey",
    "mls":                    "pro soccer",
    "wnba":                   "pro women's basketball",
    "pro basketball":         "nba",
    "pro football":           "nfl",
    "pro baseball":           "mlb",
    "pro hockey":             "nhl",
    "pro soccer":             "mls",
    "pro women's basketball": "wnba",
}

# Kalshi sports series tickers to query (covers all major leagues active today)
KALSHI_SPORTS_SERIES = [
    # World Cup / Soccer
    "KXWCGAME",          # World Cup match results
    "KXWCGROUPWINNER",   # Group winners
    "KXWCGOALSCORER",    # Goal scorers
    "KXWCWINNER",        # Tournament winner
    "KXWCGOLDENBOOT",    # Golden Boot
    "KXWCTEAMTOTAL",     # Team totals
    "KXWCSPREADS",       # Spreads
    "KXWC1HBTTS",        # 1H both teams to score
    "KXWCGOALIEPEN",     # Penalty shootout
    # NBA / Pro Basketball
    "KXNBADRAFTPICK",    # Draft individual picks
    "KXNBADRAFTTOP30",   # Top 30 picks
    "KXTOP3NBADRAFT",    # Top 3 picks
    "KXNBATOPPICK",      # Draft lottery winner
    "KXNBALOTTERY",      # Draft lottery
    "KXNBADRAFT1",       # #1 pick
    "KXNBADRAFT5",       # #5 pick
    "KXNBADRAFT7",       # #7 pick
    "KXNBADRAFT9",       # #9 pick
    "KXNBANEXTTEAM",     # Player next team
    "KXNBAGAME",         # Regular season games
    "KXNBAPLAYOFF",      # Playoffs
    "KXNBASERIES",       # Series results
    # NFL / Pro Football
    "KXNFLGAME",         # Game matchups
    "KXNFLDRAFTCAT",     # Draft category
    "KXNFLDRAFTTOP",     # Top draft picks
    # MLB / Pro Baseball
    "KXMLBGAME",         # Game matchups
    "KXMLBSERIES",       # Series
    # NHL / Pro Hockey
    "KXNHLDRAFTPICK",    # Draft picks
    "KXNHLGAME",         # Games
    "KXNHLSERIES",       # Series
    # WNBA
    "KXWNBAGAME",        # Games
    # Tennis / Wimbledon
    "KXWMENSINGLES",     # Wimbledon men's singles
    "KXWWOMENSINGLES",   # Wimbledon women's singles
    # UFC / MMA
    "KXUFCMAIN",         # UFC main events
    "KXUFCWEIGHT",       # Weight class events
]

SPORTS_KEYWORDS = {
    "soccer", "football", "basketball", "baseball", "hockey", "tennis",
    "golf", "mma", "ufc", "world cup", "nfl", "nba", "mlb", "nhl", "mls",
    "wnba", "draft", "wimbledon", "fifa", "olympic", "championship",
    "playoff", "vs.", " vs ", "match", "league", "pro basketball",
    "pro football", "pro baseball", "pro hockey",
}

# Titles containing these are NOT sports (esports / gaming)
NON_SPORTS_EXCLUDE = {
    "dota", "counter-strike", "cs:go", "csgo", "valorant",
    "league of legends", "esports", "e-sports", "starcraft",
    "overwatch", "fortnite", "rocket league", "call of duty",
    "hearthstone", "magic: the gathering",
}

HEADERS = {"Accept": "application/json"}


# ── Text helpers ──────────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    """Lowercase + expand league abbreviations for consistent comparison."""
    t = text.lower()
    for abbr, full in LEAGUE_ALIASES.items():
        t = t.replace(abbr, full)
    return t


def is_sports(title: str, tags: str = "") -> bool:
    combined = (title + " " + tags).lower()
    if any(ex in combined for ex in NON_SPORTS_EXCLUDE):
        return False
    return any(kw in combined for kw in SPORTS_KEYWORDS)


def fuzzy_match(a: str, b: str, threshold: float = 0.62) -> bool:
    """Return True if a and b likely describe the same market/event."""
    a_n, b_n = normalize(a), normalize(b)
    if a_n in b_n or b_n in a_n:
        return True
    stops = {"vs", "vs.", "the", "a", "an", "in", "at", "of", "to", "and",
             "or", "for", "by", "on", "be", "will", "is", "are", "was"}
    a_words = set(a_n.split()) - stops
    b_words = set(b_n.split()) - stops
    if a_words and b_words:
        overlap = len(a_words & b_words)
        min_len = min(len(a_words), len(b_words))
        if overlap >= 2 and overlap / min_len >= 0.45:
            return True
    return SequenceMatcher(None, a_n, b_n).ratio() >= threshold


# ── Kalshi ────────────────────────────────────────────────────────────────────

def get_kalshi_trending_sports(top_n: int = 40) -> list:
    """
    Query known sports series on Kalshi for open events.
    Returns events ordered by 24h volume descending.
    """
    events_by_ticker = {}

    for series_ticker in KALSHI_SPORTS_SERIES:
        try:
            resp = requests.get(
                f"{KALSHI_BASE}/events",
                params={
                    "limit": 20,
                    "series_ticker": series_ticker,
                    "status": "open",
                    "with_nested_markets": "true",
                },
                headers=HEADERS,
                timeout=10,
            )
            if not resp.ok:
                continue
            for event in resp.json().get("events", []):
                ticker = event.get("event_ticker", "")
                if ticker in events_by_ticker:
                    continue
                markets = event.get("markets", [])
                vol = sum(float(m.get("volume_24h_fp") or 0) for m in markets)
                events_by_ticker[ticker] = {
                    "title": event.get("title", "").strip(),
                    "source": "kalshi",
                    "volume_24h": vol,
                    "series_ticker": series_ticker,
                }
        except Exception as e:
            print(f"  [kalshi] {series_ticker} error: {e}", file=sys.stderr)
            continue

    ranked = sorted(events_by_ticker.values(), key=lambda x: x["volume_24h"], reverse=True)
    with_vol = [e for e in ranked if e["volume_24h"] > 0]
    zero_vol = [e for e in ranked if e["volume_24h"] == 0]
    return (with_vol + zero_vol)[:top_n]


# ── Polymarket ────────────────────────────────────────────────────────────────

def get_polymarket_trending_sports(top_n: int = 40) -> list:
    """
    Fetch top events from Polymarket sorted by 24h volume, filter for sports.
    """
    try:
        resp = requests.get(
            f"{POLYMARKET_BASE}/events",
            params={
                "active": "true",
                "closed": "false",
                "limit": 100,
                "order": "volume24hr",
                "ascending": "false",
            },
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        events = resp.json()

        sports = []
        for ev in events:
            title = ev.get("title", "").strip()
            tags  = str(ev.get("tags", ""))
            if is_sports(title, tags):
                sports.append({
                    "title": title,
                    "source": "polymarket",
                    "volume_24h": float(ev.get("volume24hr") or 0),
                })

        sports.sort(key=lambda x: x["volume_24h"], reverse=True)
        return sports[:top_n]

    except Exception as e:
        print(f"  [polymarket] error: {e}", file=sys.stderr)
        return []


# ── Freeplay ──────────────────────────────────────────────────────────────────

def get_freeplay_sports_markets(max_pages: int = 8) -> list:
    """
    Fetch all sports market titles from Freeplay ROLR via tRPC.
    No authentication required.
    """
    titles = []
    cursor = None

    for _ in range(max_pages):
        params = {
            "limit": 50,
            "coverageBuckets": ["sports"],
            "prioritizeBucket": "sports",
            "rankByTradeCount": True,
        }
        if cursor:
            params["cursor"] = cursor

        input_json = json.dumps({"0": {"json": params}})

        try:
            resp = requests.get(
                f"{FREEPLAY_BASE}/markets.list",
                params={"batch": "1", "input": input_json},
                headers=HEADERS,
                timeout=15,
            )
            resp.raise_for_status()
            result = resp.json()[0]["result"]["data"]["json"]
            markets = result.get("markets", [])
            titles.extend(m["title"].strip() for m in markets)
            cursor = result.get("cursor")
            if not cursor or len(markets) < 50:
                break
        except Exception as e:
            print(f"  [freeplay] page error: {e}", file=sys.stderr)
            break

    return titles


# ── Comparison ────────────────────────────────────────────────────────────────

def find_gaps(trending: list, freeplay_titles: list) -> list:
    """Return trending items with no fuzzy match in freeplay_titles."""
    gaps = []
    for item in trending:
        if not any(fuzzy_match(item["title"], fp) for fp in freeplay_titles):
            gaps.append(item)
    return gaps


def deduplicate(items: list) -> list:
    """Remove near-duplicate titles across sources (keep highest volume)."""
    seen = []
    unique = []
    for item in sorted(items, key=lambda x: x["volume_24h"], reverse=True):
        if not any(fuzzy_match(item["title"], s) for s in seen):
            seen.append(item["title"])
            unique.append(item)
    return unique


# ── Slack ─────────────────────────────────────────────────────────────────────

def post_to_slack(gaps: list) -> None:
    kalshi_gaps = [g for g in gaps if g["source"] == "kalshi"]
    poly_gaps   = [g for g in gaps if g["source"] == "polymarket"]

    lines = [
        ":stadium: *ROLR Freeplay Market Gap Alert*",
        "Sports topics trending on Kalshi/Polymarket but *not* on Freeplay:\n",
    ]

    if kalshi_gaps:
        lines.append("*From Kalshi:*")
        for g in kalshi_gaps:
            vol = f" _(${g['volume_24h']:,.0f} 24h vol)_" if g["volume_24h"] else ""
            lines.append(f"  - {g['title']}{vol}")

    if poly_gaps:
        lines.append("\n*From Polymarket:*")
        for g in poly_gaps:
            vol = f" _(${g['volume_24h']:,.0f} 24h vol)_" if g["volume_24h"] else ""
            lines.append(f"  - {g['title']}{vol}")

    payload = {"text": "\n".join(lines)}
    resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
    resp.raise_for_status()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not SLACK_WEBHOOK_URL:
        print("ERROR: SLACK_WEBHOOK_URL is not set.", file=sys.stderr)
        sys.exit(1)

    print("Fetching Kalshi trending sports...")
    kalshi = get_kalshi_trending_sports()
    print(f"  -> {len(kalshi)} events found")

    print("Fetching Polymarket trending sports...")
    polymarket = get_polymarket_trending_sports()
    print(f"  -> {len(polymarket)} events found")

    print("Fetching Freeplay (ROLR) sports markets...")
    freeplay = get_freeplay_sports_markets()
    print(f"  -> {len(freeplay)} markets found")

    if not freeplay:
        print("WARNING: Freeplay returned no markets -- skipping.", file=sys.stderr)
        sys.exit(0)

    all_trending = deduplicate(kalshi + polymarket)
    print(f"\nComparing {len(all_trending)} unique trending topics vs {len(freeplay)} Freeplay markets...")

    gaps = find_gaps(all_trending, freeplay)
    print(f"Gaps found: {len(gaps)}")

    if gaps:
        for g in gaps:
            print(f"  MISSING [{g['source']}]: {g['title']}")
        print("\nPosting to Slack...")
        post_to_slack(gaps)
        print("Done.")
    else:
        print("All covered. Nothing posted to Slack.")


if __name__ == "__main__":
    main()
