#!/usr/bin/env python3
"""
Polymarket Iran/Kharg Island — Daily Email Alert
Runs via GitHub Actions, sends HTML email digest.
"""

import json, os, smtplib, time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import requests

# ─── Config ───────────────────────────────────────────────────────────────────

SMTP_EMAIL = os.getenv("SMTP_EMAIL", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ALERT_RECIPIENT = os.getenv("ALERT_RECIPIENT", "") or SMTP_EMAIL
SNAPSHOT_FILE = Path("polymarket_snapshot.json")

ODDS_ALERT_PP = 5
VOLUME_SPIKE_X = 2.0

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

# ─── API ──────────────────────────────────────────────────────────────────────

def fetch(url):
    try:
        r = requests.get(url, timeout=15, headers={"Accept": "application/json"})
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

# ─── Analysis ─────────────────────────────────────────────────────────────────

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

    note = ""
    tokens = parse_json_field(market.get("clobTokenIds", ""))
    if tokens:
        h = price_history(tokens[0])
        if len(h) >= 2:
            mv = (h[-1]["p"] - h[-2]["p"]) * 100
            if abs(mv) >= 3: note = f"Intraday {mv:+.1f}pp"

    return dict(q=q, mid=mid, slug=slug,
        url=f"https://polymarket.com/event/{slug}" if slug else "",
        yes=yes, pct=yes*100, delta=delta,
        v24=v24, v1w=v1w, vtot=vtot, liq=liq,
        spike=spike, flags=flags, note=note)

# ─── Email ────────────────────────────────────────────────────────────────────

def fmt(v):
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"

def build_email(results):
    now = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    flagged = sorted([r for r in results if r["flags"]], key=lambda x: abs(x["delta"]), reverse=True)
    normal = sorted([r for r in results if not r["flags"]], key=lambda x: x["v24"], reverse=True)
    ordered = flagged + normal

    n = len(flagged)
    subj = f"🚨 Polymarket Iran: {n} alert{'s'*(n!=1)} — {now}" if n else f"📊 Polymarket Iran — {now}"

    rows = ""
    for r in ordered:
        cc = "#27ae60" if r["delta"] > 0 else "#e74c3c" if r["delta"] < 0 else "#888"
        cs = f"{r['delta']:+.1f}pp" if r["delta"] else "—"
        fl = "".join(f"<div style='color:#e74c3c;font-weight:bold'>{f}</div>" for f in r["flags"])
        if r["note"]: fl += f"<div style='color:#e67e22'>{r['note']}</div>"
        if not fl: fl = "—"
        rows += f"""<tr style='border-bottom:1px solid #eee'>
<td style='padding:8px;max-width:280px'><a href='{r["url"]}' style='color:#2c3e50;text-decoration:none;font-weight:500'>{r["q"]}</a></td>
<td style='padding:8px;text-align:center;font-size:18px;font-weight:bold'>{r["pct"]:.0f}%</td>
<td style='padding:8px;text-align:center;color:{cc}'>{cs}</td>
<td style='padding:8px;text-align:center'>{fmt(r["v24"])}</td>
<td style='padding:8px;text-align:center'>{fmt(r["vtot"])}</td>
<td style='padding:8px'>{fl}</td></tr>"""

    html = f"""<html><body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;max-width:900px;margin:0 auto;padding:20px'>
<h2 style='color:#2c3e50;border-bottom:2px solid #3498db;padding-bottom:10px'>Polymarket Iran / Kharg Island</h2>
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
<p style='color:#bdc3c7;font-size:12px;margin-top:20px'>Alerts: odds ≥{ODDS_ALERT_PP}pp · vol ≥{VOLUME_SPIKE_X:.0f}x daily avg · Not financial advice</p>
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
    print(f"🔍 {datetime.now(timezone.utc).isoformat()}")
    markets = discover()
    print(f"Found {len(markets)} markets")
    if not markets: return

    prev = json.loads(SNAPSHOT_FILE.read_text()) if SNAPSHOT_FILE.exists() else {}
    results = []
    for m in markets:
        try: results.append(analyse(m, prev))
        except Exception as e: print(f"⚠️ {e}")
        time.sleep(0.2)

    snap = {r["mid"]: {"yes": r["yes"], "v24": r["v24"], "ts": datetime.now(timezone.utc).isoformat()} for r in results}
    SNAPSHOT_FILE.write_text(json.dumps(snap, indent=2))

    subj, html = build_email(results)
    send(subj, html)

    for r in results:
        f = " | ".join(r["flags"])
        print(f"  {r['pct']:5.1f}% Δ{r['delta']:+5.1f}pp  {r['q'][:55]}")
        if f: print(f"       🚨 {f}")

if __name__ == "__main__":
    main()