#!/usr/bin/env python3
"""
Polymarket Daily Alert v4
Tiered markets + orderbook + Dune on-chain + insider detection scoring.
"""

import json, os, smtplib, time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import requests

SMTP_EMAIL = os.getenv("SMTP_EMAIL", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ALERT_RECIPIENT = os.getenv("ALERT_RECIPIENT", "") or SMTP_EMAIL
DUNE_API_KEY = os.getenv("DUNE_API_KEY", "")
SNAPSHOT_FILE = Path("polymarket_snapshot.json")

ODDS_ALERT_PP = 5
VOLUME_SPIKE_X = 2.0
WHALE_ORDER_USD = 10000
INSIDER_MIN_USD = 5000
INSIDER_SCORE_THRESHOLD = 40

SEARCH_TERMS = ["kharg", "iran ceasefire", "iran invasion", "hormuz", "iran war", "iran conflict"]

# Tiered market tracking
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

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DUNE_API = "https://api.dune.com/api/v1"

INSIDER_WEIGHTS = {
    "new_wallet": 30, "very_new_wallet": 15,
    "large_trade": 10, "very_large_trade": 10,
    "contrarian": 15, "coordination": 20,
    "low_trade_count": 10, "volume_spike_market": 10,
}

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

Q_LARGE_TRADES = """
SELECT "taker" AS trader, CAST("makerAssetId" AS VARCHAR) AS asset_id,
    COUNT(*) AS num_trades,
    SUM(GREATEST("makerAmountFilled","takerAmountFilled")/1e6) AS total_usd,
    MAX(GREATEST("makerAmountFilled","takerAmountFilled")/1e6) AS largest_trade,
    MIN(evt_block_time) AS first_trade, MAX(evt_block_time) AS last_trade
FROM polymarket_polygon.CTFExchange_evt_OrderFilled
WHERE evt_block_time >= NOW() - INTERVAL '24' HOUR
AND GREATEST("makerAmountFilled","takerAmountFilled")/1e6 >= {{min_usd}}
GROUP BY "taker", CAST("makerAssetId" AS VARCHAR)
ORDER BY total_usd DESC LIMIT 100
"""

Q_WALLET_META = """
WITH wallet_list AS (
    SELECT unnest(CAST({{wallets}} AS ARRAY<VARCHAR>)) AS wallet
)
SELECT "taker" AS wallet, MIN(evt_block_time) AS first_ever_trade,
    COUNT(*) AS lifetime_trades,
    DATE_DIFF('hour', MIN(evt_block_time), NOW()) AS wallet_age_hours
FROM polymarket_polygon.CTFExchange_evt_OrderFilled
WHERE "taker" IN (SELECT wallet FROM wallet_list)
GROUP BY "taker"
"""

Q_COORDINATION = """
WITH new_wallet_trades AS (
    SELECT of."taker" AS trader, CAST(of."makerAssetId" AS VARCHAR) AS asset_id,
        of.evt_block_time AS trade_time,
        GREATEST(of."makerAmountFilled",of."takerAmountFilled")/1e6 AS trade_usd
    FROM polymarket_polygon.CTFExchange_evt_OrderFilled of
    INNER JOIN (
        SELECT "taker", MIN(evt_block_time) AS ft
        FROM polymarket_polygon.CTFExchange_evt_OrderFilled GROUP BY "taker"
    ) fa ON of."taker" = fa."taker"
    WHERE of.evt_block_time >= NOW() - INTERVAL '24' HOUR
    AND GREATEST(of."makerAmountFilled",of."takerAmountFilled")/1e6 >= {{min_usd}}
    AND fa.ft >= NOW() - INTERVAL '72' HOUR
)
SELECT asset_id, COUNT(DISTINCT trader) AS num_new_wallets,
    SUM(trade_usd) AS total_usd, ARRAY_AGG(DISTINCT trader) AS wallets
FROM new_wallet_trades GROUP BY asset_id
HAVING COUNT(DISTINCT trader) >= 2
ORDER BY num_new_wallets DESC LIMIT 20
"""

def dune_query(sql, parameters=None, label=""):
    if not DUNE_API_KEY:
        print(f"  ⚠️ No DUNE_API_KEY — skipping {label}")
        return None
    headers = {"X-Dune-API-Key": DUNE_API_KEY, "Content-Type": "application/json"}
    payload = {"query_sql": sql, "performance": "medium"}
    if parameters: payload["query_parameters"] = parameters
    try:
        resp = requests.post(f"{DUNE_API}/query/execute/sql", headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"  ⚠️ Dune {label}: {resp.status_code}")
            return None
        eid = resp.json().get("execution_id")
        if not eid: return None
        print(f"  ⏳ Dune {label}: {eid}")
        for _ in range(30):
            time.sleep(5)
            sr = requests.get(f"{DUNE_API}/execution/{eid}/status", headers=headers, timeout=15)
            if sr.status_code != 200: continue
            state = sr.json().get("state", "")
            if state == "QUERY_STATE_COMPLETED": break
            if "FAILED" in state or "CANCELLED" in state:
                print(f"  ⚠️ Dune {label}: {state}")
                return None
        rr = requests.get(f"{DUNE_API}/execution/{eid}/results", headers=headers, timeout=30)
        if rr.status_code != 200: return None
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
        data = requests.get(f"{GAMMA}/markets?active=true&closed=false&limit=200&order=volume24hr&ascending=false", timeout=15).json()
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
                markets[tid] = {"question": m.get("question",""), "slug": m.get("slug",""),
                    "yes_price": yp, "vol24": m.get("volume24hr",0) or 0, "vol1w": m.get("volume1wk",0) or 0}
    except: pass
    return markets

def score_wallets(trades, meta, coordinated, mkt_ctx):
    meta_idx = {r["wallet"]: r for r in (meta or [])}
    coord_wallets = set()
    for row in (coordinated or []):
        for w in (row.get("wallets", []) if isinstance(row.get("wallets"), list) else []):
            coord_wallets.add(w)
    scored = {}
    for row in (trades or []):
        wallet = row.get("trader", "")
        asset = row.get("asset_id", "")
        total_usd = float(row.get("total_usd", 0))
        largest = float(row.get("largest_trade", 0))
        num_trades = row.get("num_trades", 0)
        if wallet in scored:
            scored[wallet]["total_usd"] += total_usd
            scored[wallet]["num_trades"] += num_trades
            if largest > scored[wallet]["largest_trade"]: scored[wallet]["largest_trade"] = largest
            continue
        m = meta_idx.get(wallet, {})
        age = m.get("wallet_age_hours", 9999)
        lt = m.get("lifetime_trades", 9999)
        mkt = mkt_ctx.get(asset, {})
        yp = mkt.get("yes_price", 0.5)
        vol24 = mkt.get("vol24", 0)
        vol1w = mkt.get("vol1w", 0)
        score, reasons = 0, []
        if age <= 72: score += INSIDER_WEIGHTS["new_wallet"]; reasons.append(f"New wallet ({age:.0f}h)")
        if age <= 24: score += INSIDER_WEIGHTS["very_new_wallet"]; reasons.append("Created <24h ago")
        if largest >= 10000: score += INSIDER_WEIGHTS["large_trade"]; reasons.append(f"Trade ${largest:,.0f}")
        if largest >= 50000: score += INSIDER_WEIGHTS["very_large_trade"]; reasons.append(f"Very large ${largest:,.0f}")
        if 0 < yp < 0.20: score += INSIDER_WEIGHTS["contrarian"]; reasons.append(f"Contrarian ({yp*100:.0f}% odds)")
        if wallet in coord_wallets: score += INSIDER_WEIGHTS["coordination"]; reasons.append("Coordinated cluster")
        if lt < 5: score += INSIDER_WEIGHTS["low_trade_count"]; reasons.append(f"{lt} lifetime trades")
        avg_d = vol1w / 7 if vol1w > 0 else 0
        if avg_d > 0 and vol24 > avg_d * 3: score += INSIDER_WEIGHTS["volume_spike_market"]; reasons.append(f"Vol spike {vol24/avg_d:.1f}x")
        scored[wallet] = {"wallet": wallet, "wallet_short": f"{wallet[:6]}...{wallet[-4:]}" if len(wallet)>10 else wallet,
            "score": min(score, 100), "reasons": reasons, "total_usd": total_usd,
            "largest_trade": largest, "num_trades": num_trades, "age_hours": age,
            "lifetime_trades": lt, "question": mkt.get("question", "Unknown")}
    results = [v for v in scored.values() if v["score"] >= INSIDER_SCORE_THRESHOLD]
    results.sort(key=lambda x: x["score"], reverse=True)
    return results

def detect_insiders():
    print("\n🕵️ Insider Detection Module")
    mkt_ctx = get_market_odds()
    print(f"  {len(mkt_ctx)} tokens mapped")
    trades = dune_query(Q_LARGE_TRADES, {"min_usd": str(INSIDER_MIN_USD)}, "large_trades")
    if not trades: return []
    wallets = list(set(r.get("trader","") for r in trades))
    meta = dune_query(Q_WALLET_META, {"wallets": wallets}, "wallet_meta")
    coord = dune_query(Q_COORDINATION, {"min_usd": str(INSIDER_MIN_USD)}, "coordination")
    return score_wallets(trades, meta, coord, mkt_ctx)

# ─── Discovery ────────────────────────────────────────────────────────────────

def discover():
    found = {}
    for term in SEARCH_TERMS:
        for m in search_markets(term):
            q = (m.get("question") or "").lower()
            if any(k in q for k in ["iran", "kharg", "hormuz"]):
                mid = m.get("id") or m.get("conditionId", "")
                if mid: found[mid] = m
        time.sleep(0.3)
    for slug in TRACKED_SLUGS:
        for m in get_by_slug(slug):
            mid = m.get("id") or m.get("conditionId", "")
            if mid: found[mid] = m
        time.sleep(0.3)
    return list(found.values())

# ─── Orderbook Analysis ──────────────────────────────────────────────────────

def analyse_orderbook(token_id):
    result = {"largest_bid": 0, "largest_ask": 0, "bid_depth": 0, "ask_depth": 0,
              "whale_orders": [], "imbalance": 1.0, "bid_wall": None, "ask_wall": None}
    book = get_orderbook(token_id)
    if not book: return result
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
    if isinstance(val, list): return val
    try: return json.loads(val) if isinstance(val, str) else []
    except: return []

def get_tier(slug):
    if slug in TIER1_SET: return 1
    if slug in TIER2_SET: return 2
    return 3

def analyse(market, prev):
    q = market.get("question", "?")
    mid = market.get("id", "")
    slug = market.get("slug", "")
    prices = parse_json_field(market.get("outcomePrices", ""))
    yes = float(prices[0]) if prices else 0
    v24 = market.get("volume24hr", 0) or 0
    v1w = market.get("volume1wk", 0) or 0
    vtot = market.get("volumeNum", 0) or float(market.get("volume", 0) or 0)
    liq = market.get("liquidityNum", 0) or 0
    p = prev.get(mid, {})
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
            buys = len([w for w in ob["whale_orders"] if w["side"] == "BUY"])
            sells = n - buys
            whale_flags.append(f"🐋 {n} large order{'s'*(n>1)}: {buys}B/{sells}S (${total:,.0f})")
        imb = ob["imbalance"]
        if imb >= 2.0: whale_flags.append(f"📈 Buy pressure {imb:.1f}x")
        elif imb <= 0.5: whale_flags.append(f"📉 Sell pressure {1/imb:.1f}x")
        if ob["bid_wall"] and ob["bid_wall"]["usd"] >= 50000:
            whale_flags.append(f"🧱 Bid wall ${ob['bid_wall']['usd']:,.0f} @ {ob['bid_wall']['price']:.2f}")
        if ob["ask_wall"] and ob["ask_wall"]["usd"] >= 50000:
            whale_flags.append(f"🧱 Ask wall ${ob['ask_wall']['usd']:,.0f} @ {ob['ask_wall']['price']:.2f}")
    note = ""
    if tokens:
        h = price_history(tokens[0])
        if len(h) >= 2:
            mv = (h[-1]["p"] - h[-2]["p"]) * 100
            if abs(mv) >= 3: note = f"Intraday {mv:+.1f}pp"
    tier = get_tier(slug)
    return dict(q=q, mid=mid, slug=slug, tokens=tokens, tier=tier,
        url=f"https://polymarket.com/event/{slug}" if slug else "",
        yes=yes, pct=yes*100, delta=delta,
        v24=v24, v1w=v1w, vtot=vtot, liq=liq,
        spike=spike, flags=flags, whale_flags=whale_flags, note=note)

# ─── Context Summary ─────────────────────────────────────────────────────────

def build_context(results):
    lines = []
    inv = next((r for r in results if "forces enter" in r["q"].lower()), None)
    cf = next((r for r in results if "ceasefire" in r["q"].lower() and "oil" not in r["q"].lower()), None)
    kharg = next((r for r in results if "kharg" in r["q"].lower() and "control" in r["q"].lower()), None)
    oil_cf = next((r for r in results if "ceasefire" in r["q"].lower() and "oil" in r["q"].lower()), None)
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
    if wm: lines.append(f"Whale signals in <b>{len(wm)}</b> market{'s'*(len(wm)>1)}.")
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
        for f in r["flags"]: fl += f"<div style='color:#e74c3c;font-weight:bold'>{f}</div>"
        for f in r["whale_flags"]: fl += f"<div style='color:#8e44ad;font-weight:bold'>{f}</div>"
        if r["note"]: fl += f"<div style='color:#e67e22'>{r['note']}</div>"
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
    now = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    t1 = sorted([r for r in results if r["tier"]==1], key=lambda x: len(x["whale_flags"])*10+abs(x["delta"]), reverse=True)
    t2 = sorted([r for r in results if r["tier"]==2], key=lambda x: len(x["whale_flags"])*10+abs(x["delta"]), reverse=True)
    t3 = sorted([r for r in results if r["tier"]==3], key=lambda x: x["v24"], reverse=True)

    n_odds = len([r for r in results if r["flags"]])
    n_ob = len([r for r in results if r["whale_flags"]])
    n_ins = len(insiders)
    hi_ins = len([i for i in insiders if i["score"]>=70])
    parts = []
    if n_odds: parts.append(f"{n_odds} odds")
    if n_ob: parts.append(f"{n_ob} orderbook")
    if n_ins: parts.append(f"{n_ins} suspicious wallets ({hi_ins} high risk)" if hi_ins else f"{n_ins} suspicious wallets")
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
            bar = f"<div style='background:#eee;border-radius:3px;height:8px;width:80px;display:inline-block'><div style='background:{col};border-radius:3px;height:8px;width:{min(w['score'],100)}%'></div></div>"
            reasons = "<br>".join(f"<span style='font-size:11px;color:#666'>• {r}</span>" for r in w["reasons"][:4])
            mkt = f"<div style='font-size:11px;color:#888;margin-top:2px'>{w['question'][:55]}</div>" if w.get("question") != "Unknown" else ""
            irows += f"""<tr style='border-bottom:1px solid #eee'>
<td style='padding:8px'><span style='font-size:20px;font-weight:bold;color:{col}'>{w["score"]}</span> {bar}</td>
<td style='padding:8px'><a href='https://polygonscan.com/address/{w["wallet"]}' style='font-family:monospace;font-size:12px;color:#3498db;text-decoration:none'>{w["wallet_short"]}</a>{mkt}</td>
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
Score: new wallet (+30) · &lt;24h (+15) · ≥$10K trade (+10) · ≥$50K (+10) · contrarian &lt;20% odds (+15) · coordinated cluster (+20) · &lt;5 lifetime trades (+10) · vol spike (+10)<br>
<b>Sell "Yes" = betting AGAINST</b> · <b>Buy "Yes" = betting FOR</b></div>
<p style='color:#bdc3c7;font-size:11px'>Sources: Polymarket Gamma/CLOB + Dune Analytics · Not financial advice</p>
</body></html>"""
    return subj, html

def send(subj, html):
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        print(f"⚠️ No SMTP creds.\nSubject: {subj}"); return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subj, SMTP_EMAIL, ALERT_RECIPIENT
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls(); s.login(SMTP_EMAIL, SMTP_PASSWORD)
        s.sendmail(SMTP_EMAIL, [ALERT_RECIPIENT], msg.as_string())
    print(f"✅ Sent to {ALERT_RECIPIENT}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"🔍 Polymarket Alert v4 — {datetime.now(timezone.utc).isoformat()}")

    # 1. Iran-specific markets
    markets = discover()
    print(f"Found {len(markets)} Iran markets")
    if not markets: return
    prev = json.loads(SNAPSHOT_FILE.read_text()) if SNAPSHOT_FILE.exists() else {}
    results = []
    for m in markets:
        try: results.append(analyse(m, prev))
        except Exception as e: print(f"⚠️ {e}")
        time.sleep(0.2)

    # 2. Insider detection (ALL markets, pattern-based)
    insiders = detect_insiders()

    # 3. Save & send
    snap = {r["mid"]: {"yes": r["yes"], "v24": r["v24"],
            "ts": datetime.now(timezone.utc).isoformat()} for r in results}
    SNAPSHOT_FILE.write_text(json.dumps(snap, indent=2))
    subj, html = build_email(results, insiders)
    send(subj, html)

    # Console
    print(f"\n{'='*60}\n  {subj}\n{'='*60}")
    for r in results:
        tl = {1:"T1",2:"T2",3:"T3"}.get(r["tier"],"?")
        print(f"  [{tl}] {r['pct']:5.1f}% Δ{r['delta']:+5.1f}pp  {r['q'][:55]}")
        for f in r["flags"]: print(f"        🚨 {f}")
        for f in r["whale_flags"]: print(f"        🐋 {f}")
    if insiders:
        print(f"\n  🕵️ INSIDER DETECTION ({len(insiders)} flagged):")
        for w in insiders[:10]:
            print(f"    Score {w['score']:3d}  {w['wallet_short']}  {fmt(w['total_usd'])}  {', '.join(w['reasons'][:2])}")

if __name__ == "__main__":
    main()
