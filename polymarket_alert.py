# #!/usr/bin/env python3
“””
Polymarket Daily Alert — Iran / Kharg Island Markets

Fetches odds, volume, and price changes from Polymarket’s public APIs,
compares with previous snapshot, and emails a daily digest.

Setup:
pip install requests

Config:
Set environment variables (or edit the CONFIG dict below):
SMTP_EMAIL       — your Gmail address
SMTP_PASSWORD    — Gmail App Password (not your real password)
ALERT_RECIPIENT  — email to receive alerts (defaults to SMTP_EMAIL)

Schedule:
crontab -e
0 8 * * * cd /path/to/this && python3 polymarket_alert.py

Or GitHub Actions — see the generated workflow file.
“””

import json
import os
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import requests

# ─── Configuration ────────────────────────────────────────────────────────────

CONFIG = {
“smtp_email”: os.getenv(“SMTP_EMAIL”, “”),
“smtp_password”: os.getenv(“SMTP_PASSWORD”, “”),
“alert_recipient”: os.getenv(“ALERT_RECIPIENT”, “”),
“smtp_server”: “smtp.gmail.com”,
“smtp_port”: 587,
“snapshot_file”: Path(**file**).parent / “polymarket_snapshot.json”,
# Thresholds for highlighting
“odds_change_alert_pct”: 5,      # highlight if odds moved ≥5pp
“volume_spike_multiplier”: 2.0,  # highlight if 24h vol ≥ 2x the 7d avg daily
}

# Markets to track — add/remove slugs as needed.

# These are Gamma API event slugs (the URL path on polymarket.com).

TRACKED_EVENT_SLUGS = [
“kharg-island-no-longer-under-iranian-control-by-march-31”,
“kharg-island-no-longer-under-iranian-control-by”,
“will-the-kharg-island-oil-terminal-be-hit-by-march-31”,
“us-forces-enter-iran-by”,
“will-the-us-invade-iran-before-2027”,
“us-x-iran-ceasefire-by”,
“trump-announces-end-of-military-operations-against-iran-by”,
“will-the-us-officially-declare-war-on-iran-by”,
]

# Fallback: search terms if slugs change or you want auto-discovery

SEARCH_TERMS = [“kharg island”, “iran ceasefire”, “iran invasion”, “hormuz”]

GAMMA_API = “https://gamma-api.polymarket.com”
CLOB_API = “https://clob.polymarket.com”

# ─── API helpers ──────────────────────────────────────────────────────────────

def gamma_search_events(query: str, limit: int = 10) -> list[dict]:
“”“Search Gamma API for events matching a query.”””
resp = requests.get(
f”{GAMMA_API}/events”,
params={“tag”: “Iran”, “limit”: limit, “active”: True},
timeout=15,
)
resp.raise_for_status()
return resp.json()

def gamma_get_markets(event_slug: str = “”, active: bool = True) -> list[dict]:
“”“Get markets, optionally filtered by event slug.”””
params: dict[str, Any] = {“active”: active, “limit”: 100}
if event_slug:
params[“slug”] = event_slug
resp = requests.get(f”{GAMMA_API}/markets”, params=params, timeout=15)
resp.raise_for_status()
data = resp.json()
return data if isinstance(data, list) else []

def gamma_search_markets(query: str) -> list[dict]:
“”“Full-text search for markets.”””
resp = requests.get(
f”{GAMMA_API}/markets”,
params={”_q”: query, “active”: True, “limit”: 50},
timeout=15,
)
resp.raise_for_status()
data = resp.json()
return data if isinstance(data, list) else []

def clob_get_midpoint(token_id: str) -> float | None:
“”“Get midpoint price for a token (0-1 scale).”””
try:
resp = requests.get(
f”{CLOB_API}/midpoint”, params={“token_id”: token_id}, timeout=10
)
resp.raise_for_status()
return float(resp.json().get(“mid”, 0))
except Exception:
return None

def clob_get_price_history(token_id: str, interval: str = “1d”) -> list[dict]:
“”“Get price history. interval: 1h, 6h, 1d, 1w, max.”””
try:
resp = requests.get(
f”{CLOB_API}/prices-history”,
params={“market”: token_id, “interval”: interval},
timeout=15,
)
resp.raise_for_status()
return resp.json().get(“history”, [])
except Exception:
return []

# ─── Market discovery ─────────────────────────────────────────────────────────

def discover_markets() -> list[dict]:
“”“Find all Iran/Kharg-related markets via slugs + search.”””
found: dict[str, dict] = {}

```
# 1) Try known slugs
for slug in TRACKED_EVENT_SLUGS:
    try:
        markets = gamma_get_markets(event_slug=slug)
        for m in markets:
            mid = m.get("id") or m.get("conditionId", "")
            if mid:
                found[mid] = m
    except Exception:
        pass

# 2) Search for additional markets
for term in SEARCH_TERMS:
    try:
        markets = gamma_search_markets(term)
        for m in markets:
            q = (m.get("question") or "").lower()
            # Filter to Iran/Kharg/Hormuz related
            if any(kw in q for kw in ["iran", "kharg", "hormuz"]):
                mid = m.get("id") or m.get("conditionId", "")
                if mid:
                    found[mid] = m
    except Exception:
        pass

return list(found.values())
```

# ─── Snapshot management ──────────────────────────────────────────────────────

def load_snapshot() -> dict:
path = CONFIG[“snapshot_file”]
if path.exists():
return json.loads(path.read_text())
return {}

def save_snapshot(data: dict):
CONFIG[“snapshot_file”].write_text(json.dumps(data, indent=2))

# ─── Analysis ─────────────────────────────────────────────────────────────────

def analyse_market(market: dict, prev_snapshot: dict) -> dict:
“”“Analyse a single market and return a summary dict.”””
question = market.get(“question”, “Unknown”)
market_id = market.get(“id”, “”)
slug = market.get(“slug”, “”)
url = f”https://polymarket.com/event/{slug}” if slug else “”

```
# Parse current prices from Gamma data
outcome_prices_raw = market.get("outcomePrices", "")
outcomes_raw = market.get("outcomes", "")
try:
    prices = json.loads(outcome_prices_raw) if isinstance(outcome_prices_raw, str) else outcome_prices_raw
except Exception:
    prices = []
try:
    outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
except Exception:
    outcomes = []

yes_price = float(prices[0]) if prices else 0
no_price = float(prices[1]) if len(prices) > 1 else 0

# Volume data
vol_24h = market.get("volume24hr", 0) or 0
vol_1wk = market.get("volume1wk", 0) or 0
vol_total = market.get("volumeNum", 0) or market.get("volume", 0) or 0

# Liquidity
liquidity = market.get("liquidityNum", 0) or 0

# Previous snapshot comparison
prev = prev_snapshot.get(market_id, {})
prev_yes = prev.get("yes_price", yes_price)
odds_change = (yes_price - prev_yes) * 100  # in percentage points

# Volume spike detection
avg_daily_vol = (vol_1wk / 7) if vol_1wk and vol_1wk > 0 else 0
vol_spike = (vol_24h / avg_daily_vol) if avg_daily_vol > 0 else 0

# Flags
flags = []
if abs(odds_change) >= CONFIG["odds_change_alert_pct"]:
    direction = "⬆️" if odds_change > 0 else "⬇️"
    flags.append(f"{direction} Odds moved {odds_change:+.1f}pp")
if vol_spike >= CONFIG["volume_spike_multiplier"]:
    flags.append(f"📊 Volume spike: {vol_spike:.1f}x avg daily")

# Try to get more granular price history from CLOB
token_ids_raw = market.get("clobTokenIds", "")
try:
    token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
except Exception:
    token_ids = []

price_history_note = ""
if token_ids:
    history = clob_get_price_history(token_ids[0], "1d")
    if len(history) >= 2:
        recent = history[-1]["p"]
        prev_h = history[-2]["p"]
        intraday_move = (recent - prev_h) * 100
        if abs(intraday_move) >= 3:
            price_history_note = f"Intraday move: {intraday_move:+.1f}pp"

return {
    "question": question,
    "market_id": market_id,
    "url": url,
    "yes_price": yes_price,
    "no_price": no_price,
    "odds_pct": yes_price * 100,
    "odds_change_pp": odds_change,
    "vol_24h": vol_24h,
    "vol_1wk": vol_1wk,
    "vol_total": vol_total,
    "liquidity": liquidity,
    "vol_spike": vol_spike,
    "flags": flags,
    "price_history_note": price_history_note,
}
```

# ─── Email formatting ────────────────────────────────────────────────────────

def format_money(val: float) -> str:
if val >= 1_000_000:
return f”${val / 1_000_000:.1f}M”
elif val >= 1_000:
return f”${val / 1_000:.0f}K”
return f”${val:.0f}”

def build_email_body(results: list[dict]) -> tuple[str, str]:
“”“Returns (subject, html_body).”””
now = datetime.now(timezone.utc).strftime(”%d %b %Y %H:%M UTC”)

```
# Sort: flagged markets first, then by volume
flagged = [r for r in results if r["flags"]]
normal = [r for r in results if not r["flags"]]
flagged.sort(key=lambda x: abs(x["odds_change_pp"]), reverse=True)
normal.sort(key=lambda x: x["vol_24h"], reverse=True)
ordered = flagged + normal

# Subject line
n_alerts = len(flagged)
if n_alerts:
    subject = f"🚨 Polymarket Iran: {n_alerts} alert{'s' if n_alerts != 1 else ''} — {now}"
else:
    subject = f"📊 Polymarket Iran Daily Digest — {now}"

# Build HTML
rows_html = ""
for r in ordered:
    flag_html = ""
    if r["flags"]:
        flag_items = "".join(f"<div style='color:#e74c3c;font-weight:bold;'>{f}</div>" for f in r["flags"])
        if r["price_history_note"]:
            flag_items += f"<div style='color:#e67e22;'>{r['price_history_note']}</div>"
        flag_html = f"<td style='padding:8px;'>{flag_items}</td>"
    else:
        note = f"<div style='color:#888;'>{r['price_history_note']}</div>" if r["price_history_note"] else ""
        flag_html = f"<td style='padding:8px;color:#888;'>—{note}</td>"

    change_color = "#27ae60" if r["odds_change_pp"] > 0 else "#e74c3c" if r["odds_change_pp"] < 0 else "#888"
    change_str = f"{r['odds_change_pp']:+.1f}pp" if r["odds_change_pp"] != 0 else "—"

    rows_html += f"""
    <tr style='border-bottom:1px solid #eee;'>
        <td style='padding:8px;max-width:300px;'>
            <a href='{r["url"]}' style='color:#2c3e50;text-decoration:none;font-weight:500;'>{r["question"]}</a>
        </td>
        <td style='padding:8px;text-align:center;font-size:18px;font-weight:bold;'>{r["odds_pct"]:.0f}%</td>
        <td style='padding:8px;text-align:center;color:{change_color};'>{change_str}</td>
        <td style='padding:8px;text-align:center;'>{format_money(r["vol_24h"])}</td>
        <td style='padding:8px;text-align:center;'>{format_money(r["vol_total"])}</td>
        {flag_html}
    </tr>"""

html = f"""
<html>
<body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;max-width:900px;margin:0 auto;padding:20px;'>
    <h2 style='color:#2c3e50;border-bottom:2px solid #3498db;padding-bottom:10px;'>
        Polymarket Iran / Kharg Island — Daily Digest
    </h2>
    <p style='color:#7f8c8d;'>{now} · Tracking {len(results)} markets</p>

    <table style='width:100%;border-collapse:collapse;font-size:14px;'>
        <thead>
            <tr style='background:#f8f9fa;border-bottom:2px solid #dee2e6;'>
                <th style='padding:8px;text-align:left;'>Market</th>
                <th style='padding:8px;text-align:center;'>Yes %</th>
                <th style='padding:8px;text-align:center;'>Δ 24h</th>
                <th style='padding:8px;text-align:center;'>Vol 24h</th>
                <th style='padding:8px;text-align:center;'>Vol Total</th>
                <th style='padding:8px;text-align:left;'>Alerts</th>
            </tr>
        </thead>
        <tbody>
            {rows_html}
        </tbody>
    </table>

    <p style='color:#bdc3c7;font-size:12px;margin-top:20px;'>
        Thresholds: odds move ≥{CONFIG["odds_change_alert_pct"]}pp · volume ≥{CONFIG["volume_spike_multiplier"]:.0f}x daily avg
        <br>Data from Polymarket Gamma + CLOB APIs · Not financial advice
    </p>
</body>
</html>
"""
return subject, html
```

# ─── Email sending ────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str):
sender = CONFIG[“smtp_email”]
password = CONFIG[“smtp_password”]
recipient = CONFIG[“alert_recipient”] or sender

```
if not sender or not password:
    print("⚠️  SMTP credentials not set. Printing to stdout instead.\n")
    print(f"Subject: {subject}\n")
    # Print a plain-text version
    print("=" * 60)
    return

msg = MIMEMultipart("alternative")
msg["Subject"] = subject
msg["From"] = sender
msg["To"] = recipient
msg.attach(MIMEText(html_body, "html"))

with smtplib.SMTP(CONFIG["smtp_server"], CONFIG["smtp_port"]) as server:
    server.starttls()
    server.login(sender, password)
    server.sendmail(sender, [recipient], msg.as_string())

print(f"✅ Email sent to {recipient}")
```

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
print(f”🔍 Polymarket Iran Alert — {datetime.now(timezone.utc).isoformat()}”)

```
# 1. Discover markets
print("  Discovering markets...")
markets = discover_markets()
print(f"  Found {len(markets)} markets")

if not markets:
    print("  No markets found. Check network / API availability.")
    return

# 2. Load previous snapshot
prev_snapshot = load_snapshot()

# 3. Analyse each market
results = []
for m in markets:
    try:
        r = analyse_market(m, prev_snapshot)
        results.append(r)
        time.sleep(0.2)  # be nice to the API
    except Exception as e:
        print(f"  ⚠️  Error analysing {m.get('question', '?')}: {e}")

print(f"  Analysed {len(results)} markets")

# 4. Save new snapshot
new_snapshot = {}
for r in results:
    new_snapshot[r["market_id"]] = {
        "yes_price": r["yes_price"],
        "vol_24h": r["vol_24h"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
save_snapshot(new_snapshot)

# 5. Build and send email
subject, html_body = build_email_body(results)
send_email(subject, html_body)

# 6. Print summary to stdout
print(f"\n{'=' * 60}")
print(f"  {subject}")
print(f"{'=' * 60}")
for r in results:
    flags_str = " | ".join(r["flags"]) if r["flags"] else ""
    print(
        f"  {r['odds_pct']:5.1f}%  Δ{r['odds_change_pp']:+5.1f}pp  "
        f"Vol24h={format_money(r['vol_24h']):>6s}  "
        f"{r['question'][:60]}"
    )
    if flags_str:
        print(f"         🚨 {flags_str}")
print()
```

def dry_run():
“”“Test with sample data — verifies email formatting and SMTP without hitting the API.”””
print(“🧪 DRY RUN — using sample data\n”)
sample_results = [
{
“question”: “Kharg Island no longer under Iranian control by June 30?”,
“market_id”: “sample-1”,
“url”: “https://polymarket.com/event/kharg-island-no-longer-under-iranian-control-by”,
“yes_price”: 0.44, “no_price”: 0.56, “odds_pct”: 44,
“odds_change_pp”: 8.2,
“vol_24h”: 320000, “vol_1wk”: 1200000, “vol_total”: 9100000,
“liquidity”: 250000, “vol_spike”: 3.1,
“flags”: [“⬆️ Odds moved +8.2pp”, “📊 Volume spike: 3.1x avg daily”],
“price_history_note”: “Intraday move: +6.3pp”,
},
{
“question”: “US forces enter Iran by April 30?”,
“market_id”: “sample-2”,
“url”: “https://polymarket.com/event/us-forces-enter-iran-by”,
“yes_price”: 0.68, “no_price”: 0.32, “odds_pct”: 68,
“odds_change_pp”: 2.1,
“vol_24h”: 890000, “vol_1wk”: 4500000, “vol_total”: 55000000,
“liquidity”: 500000, “vol_spike”: 1.4,
“flags”: [],
“price_history_note”: “”,
},
{
“question”: “US x Iran ceasefire by April 30?”,
“market_id”: “sample-3”,
“url”: “https://polymarket.com/event/us-x-iran-ceasefire-by”,
“yes_price”: 0.305, “no_price”: 0.695, “odds_pct”: 30.5,
“odds_change_pp”: -5.8,
“vol_24h”: 450000, “vol_1wk”: 2100000, “vol_total”: 12000000,
“liquidity”: 300000, “vol_spike”: 2.2,
“flags”: [“⬇️ Odds moved -5.8pp”, “📊 Volume spike: 2.2x avg daily”],
“price_history_note”: “”,
},
]
subject, html_body = build_email_body(sample_results)
send_email(subject, html_body)

```
# Also save HTML for preview
preview_path = Path(__file__).parent / "email_preview.html"
preview_path.write_text(html_body)
print(f"📄 HTML preview saved to: {preview_path}")
```

if **name** == “**main**”:
import sys
if “–dry-run” in sys.argv:
dry_run()
else:
main()
