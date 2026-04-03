#!/usr/bin/env python3
"""
Polymarket Daily Alert v5 - Insider Detection Logic
Optimized for: High-Conviction, Low-Noise filtering.
Author: Your AI Collaborator (with a touch of grit)
"""
import json, os, smtplib, time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import requests

# --- CONFIG ---
SMTP_EMAIL = os.getenv("SMTP_EMAIL", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ALERT_RECIPIENT = os.getenv("ALERT_RECIPIENT", "") or SMTP_EMAIL
DUNE_API_KEY = os.getenv("DUNE_API_KEY", "")
SNAPSHOT_FILE = Path("polymarket_snapshot.json")

# THRESHOLDS (The 'Bot Killers')
BOT_TRADE_LIMIT = 400       # Max lifetime trades before we ignore the wallet
MAX_VELOCITY = 8            # Max trades/hour (Machines trade fast, insiders trade once)
MIN_INSIDER_SCORE = 65      # Only alert if it hits this threshold
WHALE_ORDER_USD = 10000

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DUNE_API = "https://api.v1.dune.com/api/v1"

# --- API HELPERS ---
def fetch(url, headers=None):
    try:
        r = requests.get(url, timeout=15, headers=headers or {"Accept": "application/json"})
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None

def get_market_data(slug):
    return fetch(f"{GAMMA}/markets?slug={slug}&limit=1") or []

# --- THE FIX: ENHANCED SCORING ENGINE ---
def calculate_insider_score(wallet):
    """
    Filters for the 'Oracle' profile: New, Quiet, and Aggressive.
    """
    score = 0
    reasons = []
    
    lt = wallet.get("lifetime_trades", 0)
    v = wallet.get("trades_per_hour", 0)
    age = wallet.get("age_hours", 999)
    total_usd = wallet.get("total_usd", 0)
    z = float(wallet.get("max_zscore", 0))

    # 1. HARD BOT FILTER (The MEV Killer)
    if lt > BOT_TRADE_LIMIT:
        return 0, ["Institution/MM: Too many lifetime trades."]
    
    if v > MAX_VELOCITY:
        # High frequency is a signal of a bot, not an insider.
        score -= 50
        reasons.append(f"Machine Signature: {v:.1f} trades/hr")

    # 2. ACCOUNT FRESHNESS (The 'Burner' Signal)
    if age <= 24:
        score += 45
        reasons.append("New 'Burner' wallet (<24h)")
    elif age <= 72:
        score += 25
        reasons.append("Recent account (<3 days)")

    # 3. STATISTICAL ANOMALY
    # Only care about Z-scores if the wallet isn't a professional trader.
    if z >= 3.0:
        weight = 40 if lt < 50 else 10
        score += weight
        reasons.append(f"Statistically massive trade ({z:.1f}σ)")

    # 4. CONVICTION DEPTH
    # Insiders buy big blocks, they don't scalp.
    avg_trade = total_usd / lt if lt > 0 else 0
    if avg_trade > 5000:
        score += 20
        reasons.append(f"High Conviction: ${avg_trade:,.0f} avg/trade")

    # 5. FUNDING ANALYSIS
    if wallet.get("is_bridge_funded"):
        score += 15
        reasons.append("Funded via Bridge (Typical privacy move)")

    return max(0, min(score, 100)), reasons

# --- EMAIL LOGIC ---
def build_html_report(insiders):
    rows = ""
    for i in insiders:
        color = "#e74c3c" if i['score'] >= 80 else "#f39c12"
        reasons_html = "".join([f"<li>{r}</li>" for r in i['reasons']])
        rows += f"""
        <tr style="border-bottom: 1px solid #ddd;">
            <td style="padding:10px; color:{color}; font-weight:bold;">{i['score']}</td>
            <td style="padding:10px;"><a href="https://polygonscan.com/address/{i['wallet']}">{i['wallet'][:10]}...</a></td>
            <td style="padding:10px;">${i['total_usd']:,.2f}</td>
            <td style="padding:10px;"><ul>{reasons_html}</ul></td>
        </tr>"""
    
    return f"""
    <html>
    <body style="font-family: sans-serif;">
        <h2>Polymarket Insider Alert</h2>
        <table style="width:100%; border-collapse: collapse;">
            <tr style="background:#f4f4f4;">
                <th>Score</th><th>Wallet</th><th>Volume</th><th>Evidence</th>
            </tr>
            {rows}
        </table>
    </body>
    </html>"""

def send_alert(html):
    if not SMTP_EMAIL or not html: return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🚨 Polymarket Insider Alert - {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = SMTP_EMAIL
    msg["To"] = ALERT_RECIPIENT
    msg.attach(MIMEText(html, "html"))
    
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.sendmail(SMTP_EMAIL, ALERT_RECIPIENT, msg.as_string())
    print("✅ Alert Sent.")

# --- MAIN EXECUTION ---
def main():
    print("🚀 Running Insider Detection...")
    
    # This is a mock of your Dune data ingestion
    # You should replace this with your actual DUNE_API call
    raw_data = [
        # Example of the bot you flagged: High count, high velocity
        {"wallet_address": "0xd99f3bec...", "total_usd": 50000, "lifetime_count": 500000, "trades_last_24h": 1200, "wallet_age_hours": 4000, "max_zscore": 4.5},
        # Example of a potential insider: New, few trades, high volume
        {"wallet_address": "0x123abc...", "total_usd": 15000, "lifetime_count": 2, "trades_last_24h": 2, "wallet_age_hours": 12, "max_zscore": 5.0}
    ]

    detected_insiders = []
    for entry in raw_data:
        score, reasons = calculate_insider_score({
            "wallet": entry['wallet_address'],
            "total_usd": entry['total_usd'],
            "lifetime_trades": entry['lifetime_count'],
            "trades_per_hour": entry['trades_last_24h'] / 24,
            "age_hours": entry['wallet_age_hours'],
            "max_zscore": entry['max_zscore']
        })
        
        if score >= MIN_INSIDER_SCORE:
            detected_insiders.append({
                "wallet": entry['wallet_address'],
                "score": score,
                "reasons": reasons,
                "total_usd": entry['total_usd']
            })

    if detected_insiders:
        report = build_html_report(detected_insiders)
        send_alert(report)
    else:
        print("☕ No suspicious human activity found. Only machines detected.")

if __name__ == "__main__":
    main()
