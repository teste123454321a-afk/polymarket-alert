#!/usr/bin/env python3

"""
Polymarket Daily Alert v5

Tiered markets + orderbook + Dune on-chain + insider detection scoring.

Changes from v4:
- Q_LARGE_TRADES rewritten to correctly derive trade direction from makerAssetId
  (makerAssetId=0 → BUY, else → SELL) per Polymarket contract spec
- Net position ratio added (net_ratio): 1.0 = pure directional, 0.0 = market maker
- Bot suppression: lifetime trade dampening replaces flat score multiplier
- Velocity signal now only fires on wallets < 1 week old
- Z-score boost scaled by lifetime trades (not a cliff edge)
- Adaptive threshold raised to 65th percentile with hard floor of 30
- NegRisk exchange address filtered from taker column
"""

import json, os, smtplib, time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import requests

SMTP_EMAIL        = os.getenv("SMTP_EMAIL", "")
SMTP_PASSWORD     = os.getenv("SMTP_PASSWORD", "")
ALERT_RECIPIENT   = os.getenv("ALERT_RECIPIENT", "") or SMTP_EMAIL
DUNE_API_KEY      = os.getenv("DUNE_API_KEY", "")

SNAPSHOT_FILE = Path("polymarket_snapshot.json")

DUNE_QID_LARGE_TRADES    = os.getenv("DUNE_QID_LARGE_TRADES", "")
DUNE_QID_WALLET_META     = os.getenv("DUNE_QID_WALLET_META", "")
DUNE_QID_COORDINATION    = os.getenv("DUNE_QID_COORDINATION", "")
DUNE_QID_FUNDING_SOURCES = os.getenv("DUNE_QID_FUNDING_SOURCES", "")

ODDS_ALERT_PP  = 5
VOLUME_SPIKE_X = 2.0
WHALE_ORDER_USD = 10000

# NegRisk exchange contract — appears as taker in multi-leg matches, not a real trader
NEGRISK_EXCHANGE = "0xc5d563a36ae78145c45a50134d48a1215220f80a"

SEARCH_TERMS = ["kharg", "iran ceasefire", "iran invasion", "hormuz", "iran war", "iran conflict"]

TIER1_SLUGS = [
    "us-forces-enter-iran-by",
    "us-x-iran-ceasefire-by",
    "kharg-island-no-longer-under-iranian-control-by",
]
TIER2_SLUGS = [
    "us-x-iran-ceasefire-before-oil-hits-120",
    "iran-leadership-change-or-us-x-iran-ceasefire-first",
    "iran-x-israelus-conflict-ends-by",
    "trump-announces-end-of-military-operations-against-iran-by",
]
TRACKED_SLUGS = TIER1_SLUGS + TIER2_SLUGS

TIER1_SET = set(TIER1_SLUGS)
TIER2_SET = set(TIER2_SLUGS)

GAMMA    = "https://gamma-api.polymarket.com"
CLOB     = "https://clob.polymarket.com"
DUNE_API = "https://api.dune.com/api/v1"


# ─── API Helpers ──────────────────────────────────────────────────────────────

def fetch(url, headers=None):
    try:
        r = requests.get(url, timeout=15, headers=headers or {"Accept": "application/json"})
        return r.json() if r.status_code == 200 else None
    except:
        return None

def search_markets(q):
    return fetch(f"{GAMMA}/markets?_q={q}&active=true&closed=false&limit=50") or []

def get_by_slug(slug):
    return fetch(f"{GAMMA}/markets?slug={slug}&limit=20") or []

def price_history(token_id):
    d = fetch(f"{CLOB}/prices-history?market={token_id}&interval=1d")
    return d.get("history", []) if d else []

def get_orderbook(token_id):
    return fetch(f"{CLOB}/book?token_id={token_id}")


# ─── Dune ─────────────────────────────────────────────────────────────────────

# Rewritten v5: correctly derives USDC trade size and direction from makerAssetId.
# makerAssetId = 0 → BUY (wallet pays USDC, receives outcome tokens)
# makerAssetId != 0 → SELL (wallet gives outcome tokens, receives USDC)
# Filters out the NegRisk exchange contract address as taker (multi-leg match artefact).
Q_LARGE_TRADES = """
WITH base AS (
    SELECT
        "taker" AS trader,
        -- Use the non-zero asset ID as the canonical market identifier
        CASE
            WHEN "makerAssetId" != 0 THEN CAST("makerAssetId" AS VARCHAR)
            ELSE CAST("takerAssetId" AS VARCHAR)
        END AS asset_id,
        -- Direction: makerAssetId = 0 means wallet is paying USDC (BUY)
        CASE WHEN "makerAssetId" = 0 THEN 'BUY' ELSE 'SELL' END AS side,
        -- USDC value is always on the zero-asset side
        CASE
            WHEN "makerAssetId" = 0 THEN "makerAmountFilled" / 1e6
            ELSE "takerAmountFilled" / 1e6
        END AS trade_usd,
        evt_block_time
    FROM polymarket_polygon.CTFExchange_evt_OrderFilled
    WHERE evt_block_time >= NOW() - INTERVAL '30' DAY
    -- Filter NegRisk exchange contract appearing as taker in multi-leg matches
    AND LOWER(CAST("taker" AS VARCHAR)) != '0xc5d563a36ae78145c45a50134d48a1215220f80a'
),

adaptive_min AS (
    SELECT APPROX_PERCENTILE(trade_usd, 0.90) AS min_usd
    FROM base
    WHERE evt_block_time >= NOW() - INTERVAL '7' DAY
),

market_stats AS (
    SELECT asset_id,
        AVG(trade_usd)                     AS mean_usd,
        STDDEV(trade_usd)                  AS std_usd,
        APPROX_PERCENTILE(trade_usd, 0.95) AS p95_usd
    FROM base
    GROUP BY asset_id
    HAVING COUNT(*) >= 5
),

recent AS (
    SELECT b.trader, b.asset_id, b.side, b.trade_usd, b.evt_block_time
    FROM base b, adaptive_min m
    WHERE b.evt_block_time >= NOW() - INTERVAL '7' DAY
    AND b.trade_usd >= m.min_usd
),

per_wallet_market AS (
    SELECT
        trader,
        asset_id,
        COUNT(*)           AS num_trades,
        SUM(trade_usd)     AS total_usd,
        MAX(trade_usd)     AS largest_trade,
        MIN(evt_block_time) AS first_trade,
        MAX(evt_block_time) AS last_trade,
        SUM(CASE WHEN side = 'BUY'  THEN trade_usd ELSE 0 END) AS buy_usd,
        SUM(CASE WHEN side = 'SELL' THEN trade_usd ELSE 0 END) AS sell_usd,
        -- Positive = net buyer, negative = net seller
        SUM(CASE WHEN side = 'BUY' THEN trade_usd ELSE -trade_usd END) AS net_position_usd,
        COUNT(*) * 60.0 / GREATEST(
            DATE_DIFF('minute', MIN(evt_block_time), MAX(evt_block_time)) + 1, 1
        ) AS trades_per_hour
    FROM recent
    GROUP BY trader, asset_id
),

with_stats AS (
    SELECT
        p.*,
        ms.std_usd,
        ms.p95_usd AS market_p95_usd,
        CASE WHEN ms.std_usd > 0
            THEN (p.largest_trade - ms.mean_usd) / ms.std_usd
            ELSE NULL
        END AS max_zscore,
        -- net_ratio: 1.0 = pure directional bet, 0.0 = perfectly neutral (bot/MM)
        ABS(p.net_position_usd) / NULLIF(p.total_usd, 0) AS net_ratio
    FROM per_wallet_market p
    LEFT JOIN market_stats ms ON p.asset_id = ms.asset_id
)

SELECT
    trader,
    asset_id,
    num_trades,
    total_usd,
    largest_trade,
    first_trade,
    last_trade,
    buy_usd,
    sell_usd,
    net_position_usd,
    net_ratio,
    max_zscore,
    market_p95_usd,
    trades_per_hour
FROM with_stats
ORDER BY total_usd DESC
LIMIT 100
"""

Q_WALLET_META = """
WITH base AS (
    SELECT "taker" AS trader,
        GREATEST("makerAmountFilled","takerAmountFilled")/1e6 AS trade_usd,
        evt_block_time
    FROM polymarket_polygon.CTFExchange_evt_OrderFilled
    WHERE evt_block_time >= NOW() - INTERVAL '30' DAY
),
adaptive_min AS (
    SELECT APPROX_PERCENTILE(trade_usd, 0.90) AS min_usd
    FROM base
    WHERE evt_block_time >= NOW() - INTERVAL '7' DAY
),
flagged AS (
    SELECT DISTINCT b.trader
    FROM base b, adaptive_min m
    WHERE b.evt_block_time >= NOW() - INTERVAL '7' DAY
    AND b.trade_usd >= m.min_usd
)
SELECT "taker" AS wallet,
    MIN(evt_block_time) AS first_ever_trade,
    COUNT(*) AS lifetime_trades,
    DATE_DIFF('hour', MIN(evt_block_time), NOW()) AS wallet_age_hours
FROM polymarket_polygon.CTFExchange_evt_OrderFilled
WHERE "taker" IN (SELECT trader FROM flagged)
GROUP BY "taker"
"""

Q_COORDINATION = """
WITH first_trades AS (
    SELECT "taker", MIN(evt_block_time) AS first_ever_trade
    FROM polymarket_polygon.CTFExchange_evt_OrderFilled
    GROUP BY "taker"
),
adaptive_min AS (
    SELECT APPROX_PERCENTILE(
        GREATEST("makerAmountFilled","takerAmountFilled")/1e6, 0.90
    ) AS min_usd
    FROM polymarket_polygon.CTFExchange_evt_OrderFilled
    WHERE evt_block_time >= NOW() - INTERVAL '7' DAY
),
new_wallet_trades AS (
    SELECT of."taker" AS trader, CAST(of."makerAssetId" AS VARCHAR) AS asset_id,
        GREATEST(of."makerAmountFilled",of."takerAmountFilled")/1e6 AS trade_usd
    FROM polymarket_polygon.CTFExchange_evt_OrderFilled of
    JOIN first_trades ft ON of."taker" = ft."taker"
    CROSS JOIN adaptive_min m
    WHERE of.evt_block_time >= NOW() - INTERVAL '7' DAY
    AND GREATEST(of."makerAmountFilled",of."takerAmountFilled")/1e6 >= m.min_usd
    AND ft.first_ever_trade >= NOW() - INTERVAL '72' HOUR
)
SELECT asset_id, COUNT(DISTINCT trader) AS num_new_wallets,
    SUM(trade_usd) AS total_usd, ARRAY_AGG(DISTINCT trader) AS wallets
FROM new_wallet_trades GROUP BY asset_id
HAVING COUNT(DISTINCT trader) >= 2
ORDER BY num_new_wallets DESC LIMIT 20
"""

Q_FUNDING_SOURCES = """
WITH base AS (
    SELECT "taker" AS trader,
        GREATEST("makerAmountFilled","takerAmountFilled")/1e6 AS trade_usd,
        evt_block_time
    FROM polymarket_polygon.CTFExchange_evt_OrderFilled
    WHERE evt_block_time >= NOW() - INTERVAL '30' DAY
),
adaptive_min AS (
    SELECT APPROX_PERCENTILE(trade_usd, 0.90) AS min_usd
    FROM base
    WHERE evt_block_time >= NOW() - INTERVAL '7' DAY
),
flagged AS (
    SELECT DISTINCT b.trader
    FROM base b, adaptive_min m
    WHERE b.evt_block_time >= NOW() - INTERVAL '7' DAY
    AND b.trade_usd >= m.min_usd
),
all_inflows AS (
    SELECT t."to" AS wallet,
        t."from" AS funder,
        TRY_CAST(t.value AS DOUBLE) / 1e6 AS usdc_amount
    FROM erc20_polygon.evt_Transfer t
    WHERE LOWER(CAST(t.contract_address AS VARCHAR))
        = '0x2791bca1f2de4661ed88a30c99a7a9449aa84174'
    AND t."to" IN (SELECT trader FROM flagged)
    AND t.evt_block_time >= NOW() - INTERVAL '30' DAY
    AND t."from" != '0x0000000000000000000000000000000000000000'
),
adaptive_min_usdc AS (
    SELECT APPROX_PERCENTILE(usdc_amount, 0.50) AS min_usdc
    FROM all_inflows
    WHERE usdc_amount IS NOT NULL
),
filtered AS (
    SELECT a.wallet, a.funder, SUM(a.usdc_amount) AS usdc_received
    FROM all_inflows a, adaptive_min_usdc m
    WHERE a.usdc_amount >= m.min_usdc
    GROUP BY a.wallet, a.funder
)
SELECT funder,
    ARRAY_AGG(DISTINCT wallet) AS funded_wallets,
    COUNT(DISTINCT wallet) AS wallet_count,
    SUM(usdc_received) AS total_usdc
FROM filtered
GROUP BY funder
HAVING COUNT(DISTINCT wallet) >= 2
ORDER BY wallet_count DESC LIMIT 30
"""


def dune_query(query_id, parameters=None, label=""):
    if not DUNE_API_KEY:
        print(f"  ⚠️ No DUNE_API_KEY — skipping {label}")
        return None
    if not query_id:
        print(f"  ⚠️ No query ID for {label} — add DUNE_QID_{label.upper()} to Secrets")
        return None
    headers = {"X-Dune-API-Key": DUNE_API_KEY, "Content-Type": "application/json"}
    payload = {"performance": "medium"}
    if parameters:
        payload["query_parameters"] = [
            {"key": k, "value": v, "type": "text"} for k, v in parameters.items()
        ]
    try:
        resp = requests.post(
            f"{DUNE_API}/query/{query_id}/execute",
            headers=headers, json=payload, timeout=30
        )
        if resp.status_code != 200:
            print(f"  ⚠️ Dune {label}: {resp.status_code} — {resp.text[:120]}")
            return None
        eid = resp.json().get("execution_id")
        if not eid:
            return None
        print(f"  ⏳ Dune {label} ({query_id}): {eid}")
        for _ in range(30):
            time.sleep(5)
            sr = requests.get(f"{DUNE_API}/execution/{eid}/status", headers=headers, timeout=15)
            if sr.status_code != 200:
                continue
            state = sr.json().get("state", "")
            if state == "QUERY_STATE_COMPLETED":
                break
            if "FAILED" in state or "CANCELLED" in state:
                print(f"  ⚠️ Dune {label}: {state}")
                return None
        rr = requests.get(f"{DUNE_API}/execution/{eid}/results", headers=headers, timeout=30)
        if rr.status_code != 200:
            return None
        rows = rr.json().get("result", {}).get("rows", [])
        print(f"  ✅ Dune {label}: {len(rows)} rows")
        return rows
    except Exception as e:
        print(f"  ⚠️ Dune {label}: {e}")
        return None


# ─── Insider Detection ────────────────────────────────────────────────────────

def get_market_odds():
    markets = {}
    try:
        data = requests.get(
            f"{GAMMA}/markets?active=true&closed=false&limit=200&order=volume24hr&ascending=false",
            timeout=15
        ).json()
        for m in (data if isinstance(data, list) else []):
            tokens = m.get("clobTokenIds", "")
            if isinstance(tokens, str):
                try: tokens = json.loads(tokens)
                except: tokens = []
            prices = m.get("outcomePrices", "")
            if isinstance(prices, str):
                try: prices = json.loads(prices)
                except: prices = []
            yp = float(prices[0]) if prices else 0
            for tid in tokens:
                markets[tid] = {
                    "question": m.get("question", ""),
                    "slug": m.get("slug", ""),
                    "yes_price": yp,
                    "vol24": m.get("volume24hr", 0) or 0,
                    "vol1w": m.get("volume1wk", 0) or 0,
                }
    except:
        pass
    return markets


def score_wallets(trades, meta, coordinated, funded_clusters, mkt_ctx):
    meta_idx = {r["wallet"]: r for r in (meta or [])}

    coord_wallets = set()
    for row in (coordinated or []):
        for w in (row.get("wallets", []) if isinstance(row.get("wallets"), list) else []):
            coord_wallets.add(w)

    wallet_to_funder = {}
    for row in (funded_clusters or []):
        funder = row.get("funder", "")
        if row.get("wallet_count", 0) >= 2:
            for w in (row.get("funded_wallets", []) if isinstance(row.get("funded_wallets"), list) else []):
                wallet_to_funder[w] = funder

    scored = {}
    for row in (trades or []):
        wallet          = row.get("trader", "")
        asset           = row.get("asset_id", "")
        total_usd       = float(row.get("total_usd", 0))
        largest         = float(row.get("largest_trade", 0))
        num_trades      = int(row.get("num_trades", 0))
        max_zscore      = row.get("max_zscore")
        market_p95      = float(row.get("market_p95_usd") or 0)
        trades_per_hour = float(row.get("trades_per_hour") or 0)
        net_ratio       = float(row.get("net_ratio") or 0)
        buy_usd         = float(row.get("buy_usd") or 0)
        sell_usd        = float(row.get("sell_usd") or 0)

        # Aggregate across assets for wallets seen multiple times
        if wallet in scored:
            s = scored[wallet]
            s["total_usd"]  += total_usd
            s["num_trades"] += num_trades
            if largest > s["largest_trade"]:
                s["largest_trade"] = largest
            if max_zscore and (s["max_zscore"] is None or max_zscore > s["max_zscore"]):
                s["max_zscore"] = max_zscore
            continue

        m   = meta_idx.get(wallet, {})
        age = m.get("wallet_age_hours", 9999)
        lt  = m.get("lifetime_trades", 9999)
        mkt = mkt_ctx.get(asset, {})
        yp  = mkt.get("yes_price", 0.5)
        vol24 = mkt.get("vol24", 0)
        vol1w = mkt.get("vol1w", 0)

        score, reasons = 0, []

        # ── 1. Wallet age ─────────────────────────────────────────────────────
        if age <= 72:
            score += 25
            reasons.append(f"New wallet ({age:.0f}h old)")
        if age <= 24:
            score += 15
            reasons.append("Created <24h ago")

        # ── 2. Z-score: scaled by lifetime trades ─────────────────────────────
        # High z-score from a fresh wallet = strong signal.
        # Same z-score from a 1000-trade wallet = almost certainly just volume.
        if max_zscore is not None:
            z = float(max_zscore)
            if z >= 3.0:
                if lt < 20:    boost = 40
                elif lt < 100: boost = 25
                elif lt < 500: boost = 10
                else:          boost = 3
                score += boost
                reasons.append(f"Trade size {z:.1f}σ above mean (lt={lt})")
            elif z >= 2.5:
                if lt < 100:   boost = 15
                elif lt < 500: boost = 5
                else:          boost = 1
                score += boost
                reasons.append(f"Trade size {z:.1f}σ above mean (lt={lt})")
            elif z >= 2.0 and lt < 100:
                score += 8
                reasons.append(f"Trade size {z:.1f}σ above mean")
        else:
            reasons.append("Trade in low-volume market (no baseline)")

        # ── 3. Velocity: only meaningful on new wallets ───────────────────────
        # Bots always have high velocity. Velocity only signals urgency
        # if the wallet is also young (< 1 week).
        if age <= 168 and num_trades >= 3:
            if trades_per_hour >= 10:
                score += 20
                reasons.append(f"New wallet burst: {trades_per_hour:.0f} trades/hr")
            elif trades_per_hour >= 5:
                score += 12
                reasons.append(f"New wallet velocity: {trades_per_hour:.1f} trades/hr")
            elif trades_per_hour >= 3:
                score += 6
                reasons.append(f"New wallet elevated: {trades_per_hour:.1f} trades/hr")

        # ── 4. Contrarian: buying low-probability outcome ─────────────────────
        if 0 < yp < 0.20:
            score += 15
            reasons.append(f"Contrarian ({yp*100:.0f}% odds)")

        # ── 5. Coordination: N new wallets on same outcome within 24h ─────────
        if wallet in coord_wallets:
            score += 20
            reasons.append("Coordinated cluster (new wallets)")

        # ── 6. Funding cluster: shared USDC source ────────────────────────────
        if wallet in wallet_to_funder:
            f = wallet_to_funder[wallet]
            score += 25
            reasons.append(f"Shared funder {f[:6]}…{f[-4:]}")

        # ── 7. Above market p95 ───────────────────────────────────────────────
        if market_p95 > 0 and largest > market_p95:
            score += 10
            reasons.append(f"Above market p95 (${market_p95:,.0f})")

        # ── 8. Low lifetime trades ────────────────────────────────────────────
        if lt < 5:
            score += 10
            reasons.append(f"{lt} lifetime trades total")

        # ── 9. Volume spike on market ─────────────────────────────────────────
        avg_d = vol1w / 7 if vol1w > 0 else 0
        if avg_d > 0 and vol24 > avg_d * 3:
            score += 10
            reasons.append(f"Market vol spike {vol24/avg_d:.1f}x")

        # ── 10. Net position direction ────────────────────────────────────────
        # Insiders take a directional position and hold it.
        # Market makers maintain near-neutral books.
        if net_ratio >= 0.7:
            score += 15
            direction = "long" if buy_usd >= sell_usd else "short"
            reasons.append(f"Directional position ({net_ratio:.0%} net {direction})")
        elif net_ratio < 0.2 and total_usd > 0:
            score -= 20
            reasons.append("Near-neutral book (market maker pattern)")

        # ── 11. Bot suppression: lifetime trade dampening ─────────────────────
        # Applied after all additive signals so it doesn't zero out
        # wallets that also hit strong signals (coordination, funded cluster).
        # We dampen rather than zero out — a high-frequency wallet that also
        # has a shared funder is still worth seeing, just ranked lower.
        if lt > 500:
            score = int(score * 0.4)
            reasons.append(f"Dampened: {lt} lifetime trades (bot/MM likely)")
        elif lt > 200:
            score = int(score * 0.65)
            reasons.append(f"Dampened: {lt} lifetime trades")

        scored[wallet] = {
            "wallet":        wallet,
            "wallet_short":  f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 10 else wallet,
            "score":         min(score, 100),
            "reasons":       reasons,
            "total_usd":     total_usd,
            "largest_trade": largest,
            "num_trades":    num_trades,
            "age_hours":     age,
            "lifetime_trades": lt,
            "max_zscore":    max_zscore,
            "trades_per_hour": trades_per_hour,
            "net_ratio":     net_ratio,
            "question":      mkt.get("question", "Unknown"),
        }

    # Adaptive threshold: 65th percentile with hard floor of 30.
    # Raises the bar on busy/noisy days, never drops below 30.
    non_zero = [v["score"] for v in scored.values() if v["score"] > 0]
    if non_zero:
        scores_sorted = sorted(non_zero)
        idx = int(len(scores_sorted) * 0.65)
        threshold = max(scores_sorted[min(idx, len(scores_sorted) - 1)], 30)
    else:
        threshold = 30

    results = [v for v in scored.values() if v["score"] >= threshold]
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:20]


def detect_insiders():
    print("\n🕵️ Insider Detection Module")
    mkt_ctx = get_market_odds()
    print(f"  market_ctx: {len(mkt_ctx)} tokens mapped")

    trades = dune_query(DUNE_QID_LARGE_TRADES, None, "large_trades")
    if not trades:
        print("  ⚠️ No trades returned — check DUNE_API_KEY and DUNE_QID_LARGE_TRADES secrets")
        return []

    wallets = list(set(r.get("trader", "") for r in trades))
    print(f"  {len(trades)} trade rows, {len(wallets)} unique wallets")

    meta   = dune_query(DUNE_QID_WALLET_META,     None, "wallet_meta")
    coord  = dune_query(DUNE_QID_COORDINATION,    None, "coordination")
    funded = dune_query(DUNE_QID_FUNDING_SOURCES, None, "funding_sources")

    coord_assets  = len(coord)  if coord  else 0
    funded_groups = len(funded) if funded else 0
    print(f"  coord: {coord_assets} assets | funded: {funded_groups} shared-funder groups")

    results  = score_wallets(trades, meta, coord, funded, mkt_ctx)
    non_zero = [r["score"] for r in results]
    median_str = str(sorted(non_zero)[len(non_zero) // 2]) if non_zero else "n/a"
    print(f"  scored: {len(results)} wallets surfaced (threshold ~{median_str})")
    return results


# ─── Discovery ────────────────────────────────────────────────────────────────

def discover():
    found = {}
    for term in SEARCH_TERMS:
        for m in search_markets(term):
            q = (m.get("question") or "").lower()
            if any(k in q for k in ["iran", "kharg", "hormuz"]):
                mid = m.get("id") or m.get("conditionId", "")
                if mid:
                    found[mid] = m
        time.sleep(0.3)
    for slug in TRACKED_SLUGS:
        for m in get_by_slug(slug):
            mid = m.get("id") or m.get("conditionId", "")
            if mid:
                found[mid] = m
        time.sleep(0.3)
    return list(found.values())


# ─── Orderbook Analysis ──────────────────────────────────────────────────────

def analyse_orderbook(token_id):
    result = {
        "largest_bid": 0, "largest_ask": 0,
        "bid_depth": 0,   "ask_depth": 0,
        "whale_orders": [], "imbalance": 1.0,
        "bid_wall": None,  "ask_wall": None,
    }
    book = get_orderbook(token_id)
    if not book:
        return result
    for order in (book.get("bids") or []):
        p, s = float(order.get("price", 0)), float(order.get("size", 0))
        usd = p * s
        result["bid_depth"] += usd
        if usd > result["largest_bid"]:
            result["largest_bid"] = usd
            result["bid_wall"] = {"price": p, "size": s, "usd": usd}
        if usd >= WHALE_ORDER_USD:
            result["whale_orders"].append({"side": "BUY", "price": p, "size": s, "usd": usd})
    for order in (book.get("asks") or []):
        p, s = float(order.get("price", 0)), float(order.get("size", 0))
        usd = p * s
        result["ask_depth"] += usd
        if usd > result["largest_ask"]:
            result["largest_ask"] = usd
            result["ask_wall"] = {"price": p, "size": s, "usd": usd}
        if usd >= WHALE_ORDER_USD:
            result["whale_orders"].append({"side": "SELL", "price": p, "size": s, "usd": usd})
    if result["ask_depth"] > 0:
        result["imbalance"] = result["bid_depth"] / result["ask_depth"]
    return result


# ─── Market Analysis ─────────────────────────────────────────────────────────

def parse_json_field(val):
    if isinstance(val, list):
        return val
    try:
        return json.loads(val) if isinstance(val, str) else []
    except:
        return []


def get_tier(slug):
    if slug in TIER1_SET: return 1
    if slug in TIER2_SET: return 2
    return 3


def analyse(market, prev):
    q     = market.get("question", "?")
    mid   = market.get("id", "")
    slug  = market.get("slug", "")
    prices = parse_json_field(market.get("outcomePrices", ""))
    yes   = float(prices[0]) if prices else 0
    v24   = market.get("volume24hr", 0) or 0
    v1w   = market.get("volume1wk", 0) or 0
    vtot  = market.get("volumeNum", 0) or float(market.get("volume", 0) or 0)
    liq   = market.get("liquidityNum", 0) or 0
    p     = prev.get(mid, {})
    prev_yes = p.get("yes", yes)
    delta = (yes - prev_yes) * 100
    avg_d = v1w / 7 if v1w > 0 else 0
    spike = v24 / avg_d if avg_d > 0 else 0

    flags = []
    if abs(delta) >= ODDS_ALERT_PP:
        flags.append(f"{'⬆️' if delta > 0 else '⬇️'} Odds {delta:+.1f}pp")
    if spike >= VOLUME_SPIKE_X:
        flags.append(f"📊 Vol {spike:.1f}x avg")

    tokens = parse_json_field(market.get("clobTokenIds", ""))
    whale_flags = []
    if tokens:
        ob = analyse_orderbook(tokens[0])
        time.sleep(0.2)
        n = len(ob["whale_orders"])
        if n > 0:
            total = sum(w["usd"] for w in ob["whale_orders"])
            buys  = len([w for w in ob["whale_orders"] if w["side"] == "BUY"])
            sells = n - buys
            whale_flags.append(f"🐋 {n} large order{'s'*(n>1)}: {buys}B/{sells}S (${total:,.0f})")
        imb = ob["imbalance"]
        if imb >= 2.0:
            whale_flags.append(f"📈 Buy pressure {imb:.1f}x")
        elif imb <= 0.5:
            whale_flags.append(f"📉 Sell pressure {1/imb:.1f}x")
        if ob["bid_wall"] and ob["bid_wall"]["usd"] >= 50000:
            whale_flags.append(f"🧱 Bid wall ${ob['bid_wall']['usd']:,.0f} @ {ob['bid_wall']['price']:.2f}")
        if ob["ask_wall"] and ob["ask_wall"]["usd"] >= 50000:
            whale_flags.append(f"🧱 Ask wall ${ob['ask_wall']['usd']:,.0f} @ {ob['ask_wall']['price']:.2f}")

    note = ""
    if tokens:
        h = price_history(tokens[0])
        if len(h) >= 2:
            mv = (h[-1]["p"] - h[-2]["p"]) * 100
            if abs(mv) >= 3:
                note = f"Intraday {mv:+.1f}pp"

    tier = get_tier(slug)
    return dict(
        q=q, mid=mid, slug=slug, tokens=tokens, tier=tier,
        url=f"https://polymarket.com/event/{slug}" if slug else "",
        yes=yes, pct=yes*100, delta=delta,
        v24=v24, v1w=v1w, vtot=vtot, liq=liq,
        spike=spike, flags=flags, whale_flags=whale_flags, note=note,
    )


# ─── Context Summary ─────────────────────────────────────────────────────────

def build_context(results):
    lines = []
    inv    = next((r for r in results if "forces enter"  in r["q"].lower()), None)
    cf     = next((r for r in results if "ceasefire"     in r["q"].lower() and "oil" not in r["q"].lower()), None)
    kharg  = next((r for r in results if "kharg"         in r["q"].lower() and "control" in r["q"].lower()), None)
    oil_cf = next((r for r in results if "ceasefire"     in r["q"].lower() and "oil" in r["q"].lower()), None)

    if inv:
        lines.append(f"US ground entry: <b>{inv['pct']:.0f}%</b> — {'troops expected' if inv['pct']>50 else 'air campaign only'}.")
    if cf:
        lines.append(f"Ceasefire: <b>{cf['pct']:.0f}%</b> — {'deal likely' if cf['pct']>50 else 'deep scepticism'}.")
    if kharg:
        lines.append(f"Kharg Island changes hands: <b>{kharg['pct']:.0f}%</b>.")
    if oil_cf:
        lines.append(f"Ceasefire before $120 oil: <b>{oil_cf['pct']:.0f}%</b>.")

    movers = sorted(results, key=lambda x: abs(x["delta"]), reverse=True)
    if movers and abs(movers[0]["delta"]) >= 3:
        m = movers[0]
        lines.append(f"Biggest move: <b>{m['q'][:50]}</b> {'up' if m['delta']>0 else 'down'} {abs(m['delta']):.1f}pp.")

    wm = [r for r in results if r["whale_flags"]]
    if wm:
        lines.append(f"Whale signals in <b>{len(wm)}</b> market{'s'*(len(wm)>1)}.")

    return " ".join(lines)


# ─── Email ────────────────────────────────────────────────────────────────────

def fmt(v):
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def build_market_rows(markets):
    rows = ""
    for r in markets:
        cc = "#27ae60" if r["delta"] > 0 else "#e74c3c" if r["delta"] < 0 else "#888"
        cs = f"{r['delta']:+.1f}pp" if r["delta"] else "—"
        fl = ""
        for f in r["flags"]:       fl += f"<div style='color:#e74c3c;font-weight:bold'>{f}</div>"
        for f in r["whale_flags"]: fl += f"<div style='color:#8e44ad;font-weight:bold'>{f}</div>"
        if r["note"]:              fl += f"<div style='color:#e67e22'>{r['note']}</div>"
        if not fl: fl = "—"
        rows += f"""<tr style='border-bottom:1px solid #eee'>
<td style='padding:8px;max-width:280px'><a href='{r["url"]}' style='color:#2c3e50;text-decoration:none;font-weight:500'>{r["q"]}</a></td>
<td style='padding:8px;text-align:center;font-size:18px;font-weight:bold'>{r["pct"]:.0f}%</td>
<td style='padding:8px;text-align:center;color:{cc}'>{cs}</td>
<td style='padding:8px;text-align:center'>{fmt(r["v24"])}</td>
<td style='padding:8px;text-align:center'>{fmt(r["vtot"])}</td>
<td style='padding:8px'>{fl}</td></tr>"""
    return rows


def score_color(s):
    if s >= 70: return "#c0392b"
    if s >= 50: return "#e74c3c"
    return "#e67e22"


TH = """<table style='width:100%;border-collapse:collapse;font-size:14px'>
<thead><tr style='background:#f8f9fa;border-bottom:2px solid #dee2e6'>
<th style='padding:8px;text-align:left'>Market</th>
<th style='padding:8px;text-align:center'>Yes %</th>
<th style='padding:8px;text-align:center'>Δ 24h</th>
<th style='padding:8px;text-align:center'>Vol 24h</th>
<th style='padding:8px;text-align:center'>Vol Total</th>
<th style='padding:8px;text-align:left'>Signals</th>
</tr></thead>"""


def build_email(results, insiders):
    now  = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    t1   = sorted([r for r in results if r["tier"]==1], key=lambda x: len(x["whale_flags"])*10+abs(x["delta"]), reverse=True)
    t2   = sorted([r for r in results if r["tier"]==2], key=lambda x: len(x["whale_flags"])*10+abs(x["delta"]), reverse=True)
    t3   = sorted([r for r in results if r["tier"]==3], key=lambda x: x["v24"], reverse=True)

    n_odds = len([r for r in results if r["flags"]])
    n_ob   = len([r for r in results if r["whale_flags"]])
    n_ins  = len(insiders)
    hi_ins = len([i for i in insiders if i["score"] >= 70])

    parts = []
    if n_odds: parts.append(f"{n_odds} odds")
    if n_ob:   parts.append(f"{n_ob} orderbook")
    if n_ins:  parts.append(f"{n_ins} suspicious wallets ({hi_ins} high risk)" if hi_ins else f"{n_ins} suspicious wallets")
    subj = f"🚨 Polymarket: {', '.join(parts)} — {now}" if parts else f"📊 Polymarket Daily — {now}"

    context = build_context(results)

    tier1_html = f"""<h3 style='color:#c0392b;margin-top:25px;margin-bottom:5px'>🔴 Tier 1 — Binary Catalysts</h3>
<p style='color:#7f8c8d;font-size:12px;margin-top:0'>Ground entry, ceasefire, Kharg control. Move everything.</p>
{TH}<tbody>{build_market_rows(t1)}</tbody></table>""" if t1 else ""

    tier2_html = f"""<h3 style='color:#e67e22;margin-top:25px;margin-bottom:5px'>🟠 Tier 2 — Second-Order</h3>
<p style='color:#7f8c8d;font-size:12px;margin-top:0'>Oil race, leadership, conflict timeline. Early warnings.</p>
{TH}<tbody>{build_market_rows(t2)}</tbody></table>""" if t2 else ""

    tier3_html = f"""<h3 style='color:#3498db;margin-top:25px;margin-bottom:5px'>🔵 Tier 3 — Auto-Discovered</h3>
<p style='color:#7f8c8d;font-size:12px;margin-top:0'>Other Iran/Kharg/Hormuz markets found via search.</p>
{TH}<tbody>{build_market_rows(t3)}</tbody></table>""" if t3 else ""

    insider_html = ""
    if insiders:
        total_sus_usd = sum(i["total_usd"] for i in insiders)
        irows = ""
        for w in insiders[:20]:
            col = score_color(w["score"])
            bar = (
                f"<div style='background:#eee;border-radius:3px;height:8px;width:80px;display:inline-block'>"
                f"<div style='background:{col};border-radius:3px;height:8px;width:{min(w['score'],100)}%'></div></div>"
            )
            reasons = "<br>".join(f"<span style='font-size:11px;color:#666'>• {r}</span>" for r in w["reasons"][:5])
            mkt_line = (
                f"<div style='font-size:11px;color:#888;margin-top:2px'>{w['question'][:55]}</div>"
                if w.get("question") != "Unknown" else ""
            )
            net_str = f"<span style='font-size:11px;color:#555'> · net {w['net_ratio']:.0%}</span>" if w.get("net_ratio") else ""
            irows += f"""<tr style='border-bottom:1px solid #eee'>
<td style='padding:8px'><span style='font-size:20px;font-weight:bold;color:{col}'>{w["score"]}</span> {bar}</td>
<td style='padding:8px'><a href='https://polygonscan.com/address/{w["wallet"]}' style='font-family:monospace;font-size:12px;color:#3498db;text-decoration:none'>{w["wallet_short"]}</a>{net_str}{mkt_line}</td>
<td style='padding:8px;text-align:right;font-weight:bold'>{fmt(w["total_usd"])}</td>
<td style='padding:8px;text-align:center'>{w["num_trades"]}</td>
<td style='padding:8px'>{reasons}</td></tr>"""

        insider_html = f"""
<h3 style='color:#c0392b;margin-top:30px;border-bottom:2px solid #c0392b;padding-bottom:8px'>🕵️ Insider Detection — All Polymarket Markets</h3>
<p style='color:#7f8c8d;font-size:13px'>{len(insiders)} suspicious wallets. {fmt(total_sus_usd)} total volume.
{f'<b style="color:#c0392b">{hi_ins} high risk (score ≥70).</b>' if hi_ins else ''}</p>
<table style='width:100%;border-collapse:collapse;font-size:13px'>
<thead><tr style='background:#fdf2f2;border-bottom:2px solid #e6b0aa'>
<th style='padding:8px;text-align:left;width:100px'>Score</th>
<th style='padding:8px;text-align:left'>Wallet / Market</th>
<th style='padding:8px;text-align:right'>Volume</th>
<th style='padding:8px;text-align:center'>Trades</th>
<th style='padding:8px;text-align:left'>Why</th>
</tr></thead><tbody>{irows}</tbody></table>"""
    else:
        insider_html = """<div style='background:#f0f4f8;padding:12px;border-radius:4px;margin:20px 0'>
<b>🕵️ Insider Detection:</b> No suspicious patterns in last 24h.</div>"""

    html = f"""<html><body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;max-width:900px;margin:0 auto;padding:20px'>
<h2 style='color:#2c3e50;border-bottom:2px solid #3498db;padding-bottom:10px'>Polymarket Iran / Kharg Island</h2>
<p style='color:#7f8c8d;font-size:13px'>{now} · {len(results)} markets</p>
<div style='background:#f0f4f8;border-left:4px solid #3498db;padding:12px 16px;margin:15px 0;border-radius:0 4px 4px 0'>
<b style='color:#2c3e50'>📍 Situation:</b> <span style='color:#34495e'>{context}</span></div>
{tier1_html}{tier2_html}{tier3_html}{insider_html}
<div style='background:#fef9e7;border-left:4px solid #f39c12;padding:10px 14px;margin:20px 0;border-radius:0 4px 4px 0;font-size:12px'>
<b>Signals guide:</b><br>
<b style='color:#e74c3c'>Red</b> = odds/volume anomaly · <b style='color:#8e44ad'>Purple</b> = orderbook whale<br>
🐋 Orderbook = resting orders (intent) · 🕵️ Insider = executed trades scored on patterns<br>
Score: new wallet (+25) · &lt;24h (+15) · z-score scaled by lifetime trades (+3→+40) · velocity on new wallets only (+6→+20) · contrarian &lt;20% odds (+15) · coordinated cluster (+20) · shared funder (+25) · above p95 (+10) · &lt;5 lifetime trades (+10) · vol spike (+10) · directional net position (+15)<br>
Penalties: near-neutral book/MM pattern (−20) · &gt;200 lifetime trades (×0.65) · &gt;500 lifetime trades (×0.40)<br>
Threshold: 65th percentile of day's scores, floor 30. All USD thresholds adapt to daily distribution.<br>
<b>Sell "Yes" = betting AGAINST</b> · <b>Buy "Yes" = betting FOR</b></div>
<p style='color:#bdc3c7;font-size:11px'>Sources: Polymarket Gamma/CLOB + Dune Analytics · Not financial advice</p>
</body></html>"""

    return subj, html


def send(subj, html):
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        print(f"⚠️ No SMTP creds.\nSubject: {subj}")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subj, SMTP_EMAIL, ALERT_RECIPIENT
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(SMTP_EMAIL, SMTP_PASSWORD)
        s.sendmail(SMTP_EMAIL, [ALERT_RECIPIENT], msg.as_string())
    print(f"✅ Sent to {ALERT_RECIPIENT}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"🔍 Polymarket Alert v5 — {datetime.now(timezone.utc).isoformat()}")

    # 1. Iran-specific markets
    markets = discover()
    print(f"Found {len(markets)} Iran markets")
    if not markets:
        return

    prev = json.loads(SNAPSHOT_FILE.read_text()) if SNAPSHOT_FILE.exists() else {}
    results = []
    for m in markets:
        try:
            results.append(analyse(m, prev))
        except Exception as e:
            print(f"⚠️ {e}")
        time.sleep(0.2)

    # 2. Insider detection (ALL markets, pattern-based)
    insiders = detect_insiders()

    # 3. Save snapshot & send
    snap = {
        r["mid"]: {"yes": r["yes"], "v24": r["v24"], "ts": datetime.now(timezone.utc).isoformat()}
        for r in results
    }
    SNAPSHOT_FILE.write_text(json.dumps(snap, indent=2))

    subj, html = build_email(results, insiders)
    send(subj, html)

    # Console summary
    print(f"\n{'='*60}\n  {subj}\n{'='*60}")
    for r in results:
        tl = {1: "T1", 2: "T2", 3: "T3"}.get(r["tier"], "?")
        print(f"  [{tl}] {r['pct']:5.1f}% Δ{r['delta']:+5.1f}pp  {r['q'][:55]}")
        for f in r["flags"]:       print(f"        🚨 {f}")
        for f in r["whale_flags"]: print(f"        🐋 {f}")

    if insiders:
        print(f"\n  🕵️ INSIDER DETECTION ({len(insiders)} flagged):")
        for w in insiders[:10]:
            print(
                f"    Score {w['score']:3d}  {w['wallet_short']}  "
                f"{fmt(w['total_usd'])}  net={w['net_ratio']:.0%}  "
                f"{', '.join(w['reasons'][:2])}"
            )


if __name__ == "__main__":
    main()
