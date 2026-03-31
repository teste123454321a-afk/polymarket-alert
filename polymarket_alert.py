#!/usr/bin/env python3
"""
Polymarket Iran/Kharg Island — Daily Email Alert v3
Orderbook analysis + Dune Analytics on-chain whale detection.
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
WHALE_TRADE_USD = 5000
NEW_WALLET_HOURS = 72

SEARCH_TERMS = ["kharg", "iran ceasefire", "iran invasion", "hormuz", "iran war", "iran conflict"]
TRACKED_SLUGS = [
    "kharg-island-no-longer-under-iranian-control-by",
    "will-the-kharg-island-oil-terminal-be-hit-by",
    "us-forces-enter-iran-by",
    "will-the-us-invade-iran-before-2027",
    "us-x-iran-ceasefire-by",
    "trump-announces-end-of-military-operations-against-iran-by",
    "will-the-us-officially-declare-war-on-iran-by",
    "iran-x-israelus-conflict-ends-by",
    "us-x-iran-ceasefire-before-oil-hits-120",
]

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DUNE_API = "https://api.dune.com/api/v1"

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

DUNE_SIMPLE_SQL = """
SELECT
    "taker" AS trader,
    COUNT(*) AS num_trades,
    SUM(GREATEST("makerAmountFilled", "takerAmountFilled") / 1e6) AS total_usd,
    MAX(GREATEST("makerAmountFilled", "takerAmountFilled") / 1e6) AS largest_trade_usd,
    MIN(evt_block_time) AS first_trade_time,
    MAX(evt_block_time) AS last_trade_time
FROM polymarket_polygon.CTFExchange_evt_OrderFilled
WHERE evt_block_time >= NOW() - INTERVAL '24' HOUR
AND GREATEST("makerAmountFilled", "takerAmountFilled") / 1e6 >= {{min_trade_usd}}
GROUP BY "taker"
HAVING SUM(GREATEST("makerAmountFilled", "takerAmountFilled") / 1e6) >= 10000
ORDER BY total_usd DESC
LIMIT 30
"""

def dune_execute_query(sql, parameters=None):
    if not DUNE_API_KEY:
        print("  ⚠️ No DUNE_API_KEY set, skipping on-chain analysis")
        return None
    headers = {"X-Dune-API-Key": DUNE_API_KEY, "Content-Type": "application/json"}
    payload = {"query_sql": sql, "performance": "medium"}
    if parameters:
        payload["query_parameters"] = parameters
    try:
        resp = requests.post(f"{DUNE_API}/query/execute/sql", headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            print(f"  ⚠️ Dune execute failed: {resp.status_code} {resp.text[:200]}")
            return None
        execution_id = resp.json().get("execution_id")
        if not execution_id:
            return None
        print(f"  Dune query submitted: {execution_id}")
        for i in range(24):
            time.sleep(5)
            sr = requests.get(f"{DUNE_API}/execution/{execution_id}/status", headers=headers, timeout=15)
            if sr.status_code != 200: continue
            state = sr.json().get("state", "")
            if state == "QUERY_STATE_COMPLETED": break
            elif state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
                print(f"  ⚠️ Dune query {state}")
                return None
        rr = requests.get(f"{DUNE_API}/execution/{execution_id}/results", headers=headers, timeout=30)
        if rr.status_code != 200: return None
        rows = rr.json().get("result", {}).get("rows", [])
        print(f"  Dune returned {len(rows)} whale traders")
        return rows
    except Exception as e:
        print(f"  ⚠️ Dune error: {e}")
        return None

def get_onchain_whales(token_ids_all):
    params = {"min_trade_usd": str(WHALE_TRADE_USD)}
    rows = dune_execute_query(DUNE_SIMPLE_SQL, params)
    if not rows: return []
    whales = []
    for row in rows:
        t = row.get("trader", "")
        whales.append({
            "trader": f"{t[:6]}...{t[-4:]}" if len(t) > 10 else t,
            "trader_full": t,
            "num_trades": row.get("num_trades", 0),
            "total_usd": float(row.get("total_usd", 0)),
            "largest_trade": float(row.get("largest_trade_usd", 0)),
            "is_new": row.get("is_new_wallet", False),
            "first_trade": row.get("first_trade_time", ""),
            "last_trade": row.get("last_trade_time", ""),
        })
    return whales

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

def parse_json_field(val):
    if isinstance(val, list): return val
    try: return json.loads(val) if isinstance(val, str) else []
    except: return []

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
    return dict(q=q, mid=mid, slug=slug, tokens=tokens,
        url=f"https://polymarket.com/event/{slug}" if slug else "",
        yes=yes, pct=yes*100, delta=delta,
        v24=v24, v1w=v1w, vtot=vtot, liq=liq,
        spike=spike, flags=flags, whale_flags=whale_flags, note=note)

def fmt(v):
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"

def build_email(results, onchain_whales):
    now = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    flagged = [r for r in results if r["flags"] or r["whale_flags"]]
    normal = [r for r in results if not r["flags"] and not r["whale_flags"]]
    flagged.sort(key=lambda x: len(x["whale_flags"]) * 10 + abs(x["delta"]), reverse=True)
    normal.sort(key=lambda x: x["v24"], reverse=True)
    ordered = flagged + normal
    n_odds = len([r for r in results if r["flags"]])
    n_ob = len([r for r in results if r["whale_flags"]])
    n_chain = len(onchain_whales)
    parts = []
    if n_odds: parts.append(f"{n_odds} odds")
    if n_ob: parts.append(f"{n_ob} orderbook")
    if n_chain: parts.append(f"{n_chain} on-chain whales")
    alert_text = ", ".join(parts)
    subj = f"🚨 Polymarket Iran: {alert_text} — {now}" if parts else f"📊 Polymarket Iran — {now}"
    rows = ""
    for r in ordered:
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
    whale_html = ""
    if onchain_whales:
        whale_rows = ""
        for w in onchain_whales[:15]:
            new_badge = "<span style='background:#e74c3c;color:white;padding:2px 6px;border-radius:3px;font-size:11px'>NEW</span> " if w.get("is_new") else ""
            suspicion = ""
            if w.get("is_new") and w["total_usd"] >= 20000:
                suspicion = "<span style='background:#8e44ad;color:white;padding:2px 6px;border-radius:3px;font-size:11px'>⚠️ SUS</span>"
            whale_rows += f"""<tr style='border-bottom:1px solid #eee'>
<td style='padding:6px;font-family:monospace;font-size:12px'><a href='https://polygonscan.com/address/{w.get("trader_full","")}' style='color:#3498db;text-decoration:none'>{w["trader"]}</a></td>
<td style='padding:6px;text-align:center'>{w["num_trades"]}</td>
<td style='padding:6px;text-align:right;font-weight:bold'>{fmt(w["total_usd"])}</td>
<td style='padding:6px;text-align:right'>{fmt(w["largest_trade"])}</td>
<td style='padding:6px'>{new_badge}{suspicion}</td></tr>"""
        whale_html = f"""
<h3 style='color:#8e44ad;margin-top:30px;border-bottom:2px solid #8e44ad;padding-bottom:8px'>🐋 On-Chain Whales (24h) — Dune Analytics</h3>
<p style='color:#7f8c8d;font-size:13px'>Wallets ≥${WHALE_TRADE_USD/1000:.0f}K executed. <b style='color:#e74c3c'>NEW</b> = wallet &lt;72h. <b style='color:#8e44ad'>SUS</b> = new + &gt;$20K.</p>
<table style='width:100%;border-collapse:collapse;font-size:13px'>
<thead><tr style='background:#f5eef8;border-bottom:2px solid #d2b4de'>
<th style='padding:6px;text-align:left'>Wallet</th>
<th style='padding:6px;text-align:center'>Trades</th>
<th style='padding:6px;text-align:right'>Total $</th>
<th style='padding:6px;text-align:right'>Largest</th>
<th style='padding:6px;text-align:left'>Flags</th>
</tr></thead><tbody>{whale_rows}</tbody></table>"""
    html = f"""<html><body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;max-width:900px;margin:0 auto;padding:20px'>
<h2 style='color:#2c3e50;border-bottom:2px solid #3498db;padding-bottom:10px'>Polymarket Iran / Kharg Island — v3</h2>
<p style='color:#7f8c8d'>{now} · {len(results)} markets</p>
<table style='width:100%;border-collapse:collapse;font-size:14px'>
<thead><tr style='background:#f8f9fa;border-bottom:2px solid #dee2e6'>
<th style='padding:8px;text-align:left'>Market</th>
<th style='padding:8px;text-align:center'>Yes %</th>
<th style='padding:8px;text-align:center'>Δ 24h</th>
<th style='padding:8px;text-align:center'>Vol 24h</th>
<th style='padding:8px;text-align:center'>Vol Total</th>
<th style='padding:8px;text-align:left'>Alerts</th>
</tr></thead><tbody>{rows}</tbody></table>
{whale_html}
<p style='color:#bdc3c7;font-size:11px;margin-top:25px'>
🐋 orderbook = resting orders · 🐋 on-chain = executed trades · Sources: Polymarket + Dune · Not financial advice</p>
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

def main():
    print(f"🔍 Polymarket Alert v3 — {datetime.now(timezone.utc).isoformat()}")
    markets = discover()
    print(f"Found {len(markets)} markets")
    if not markets: return
    prev = json.loads(SNAPSHOT_FILE.read_text()) if SNAPSHOT_FILE.exists() else {}
    results = []
    all_token_ids = []
    for m in markets:
        try:
            r = analyse(m, prev)
            results.append(r)
            all_token_ids.extend(r.get("tokens", []))
        except Exception as e:
            print(f"⚠️ {e}")
        time.sleep(0.2)
    print("\n🐋 Querying Dune for on-chain whales...")
    onchain_whales = get_onchain_whales(all_token_ids)
    if onchain_whales is None: onchain_whales = []
    snap = {r["mid"]: {"yes": r["yes"], "v24": r["v24"],
            "ts": datetime.now(timezone.utc).isoformat()} for r in results}
    SNAPSHOT_FILE.write_text(json.dumps(snap, indent=2))
    subj, html = build_email(results, onchain_whales)
    send(subj, html)
    print(f"\n{'='*60}\n  {subj}\n{'='*60}")
    for r in results:
        print(f"  {r['pct']:5.1f}% Δ{r['delta']:+5.1f}pp  {r['q'][:55]}")
        for f in r["flags"]: print(f"       🚨 {f}")
        for f in r["whale_flags"]: print(f"       🐋 {f}")
    if onchain_whales:
        print(f"\n  🐋 ON-CHAIN ({len(onchain_whales)}):")
        for w in onchain_whales[:10]:
            new = " 🆕" if w.get("is_new") else ""
            print(f"     {w['trader']}  {w['num_trades']}tx  {fmt(w['total_usd'])}{new}")

if __name__ == "__main__":
    main()
