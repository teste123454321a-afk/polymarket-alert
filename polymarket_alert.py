#!/usr/bin/env python3
"""
Polymarket Insider Detection v5.0 - Production Grade
Filters out high-frequency bots to find low-noise, high-conviction 'Oracle' trades.
"""
import json, os, smtplib, time, requests
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# --- CONFIG ---
DUNE_API_KEY = os.getenv("DUNE_API_KEY", "")
SMTP_EMAIL = os.getenv("SMTP_EMAIL", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ALERT_RECIPIENT = os.getenv("ALERT_RECIPIENT", "") or SMTP_EMAIL

# DUNE QUERY IDs (Ensure these are set in your environment)
DUNE_QID = os.getenv("DUNE_QID_LARGE_TRADES", "") 

# SCORING THRESHOLDS
BOT_TRADE_LIMIT = 500       # Hard filter: Ignore wallets with >500 lifetime trades
MAX_VELOCITY = 10           # Max trades/hour (Suppresses HFT bots)
MIN_INSIDER_SCORE = 65      # Alert threshold
GAMMA = "https://gamma-api.polymarket.com"

# --- CORE LOGIC: THE SCORING ENGINE ---
def calculate_insider_score(wallet):
    """
    Identifies the 'Oracle' profile: New, Quiet, and Aggressive.
    """
    score = 0
    reasons = []
    
    lt = wallet.get("lifetime_trades", 0)
    v = wallet.get("trades_per_hour", 0)
    age = wallet.get("age_hours", 999)
    total_usd = wallet.get("total_usd", 0)
    z = float(wallet.get("max_zscore", 0))

    # 1. BOT SUPPRESSION (The MEV Filter)
    if lt > BOT_TRADE_LIMIT:
        return 0, [] # Immediate disqualification
    
    if v > MAX_VELOCITY:
        score -= 40
        reasons.append(f"High Frequency Signature ({v:.1f} tr/hr)")

    # 2. ACCOUNT FRESHNESS
    if age <= 24:
        score += 45
        reasons.append("New 'Burner' wallet (<24h)")
    elif age <= 72:
        score += 20
        reasons.append("Recent account (<72h)")

    # 3. STATISTICAL ANOMALY
    if z >= 3.0:
        # High Z-score is only suspicious if the wallet isn't a professional
        score += 40 if lt < 50 else 10
        reasons.append(f"Anomalous trade size ({z:.1f}σ)")

    # 4. CONVICTION DEPTH
    avg_trade = total_usd / lt if lt > 0 else 0
    if avg_trade > 5000:
        score += 20
        reasons.append(f"High Conviction: ${avg_trade:,.0f} avg/trade")

    return max(0, min(score, 100)), reasons

# --- DUNE API INTEGRATION ---
def get_dune_results(query_id):
    headers = {"X-Dune-API-Key": DUNE_API_KEY}
    # Trigger execution
    exec_url = f"https://api.dune.com/api/v1/query/{query_id}/execute"
    res = requests.post(exec_url, headers=headers)
    if res.status_code != 200: return []
    
    execution_id = res.json()['execution_id']
    
    # Wait for completion
    for _ in range(10):
        status_url = f"https://api.dune.com/api/v1/execution/{execution_id}/results"
        results = requests.get(status_url, headers=headers).json()
        if results.get('state') == 'QUERY_STATE_COMPLETED':
            return results['result']['rows']
        time.sleep(10)
    return []

# --- ALERTING SYSTEM ---
def send_alert(insiders):
    if not insiders: return
    
    rows = ""
    for i in insiders:
        reason_list = "".join([f"<li>{r}</li>" for r in i['reasons']])
        rows += f"""
        <tr style="border-bottom:1px solid #eee">
            <td style="padding:10px"><b>{i['score']}</b></td>
            <td style="padding:10px"><a href="https://polygonscan.com/address/{i['wallet']}">{i['wallet'][:10]}...</a></td>
            <td style="padding:10px">${i['total_usd']:,.0f}</td>
            <td style="padding:10px"><i>{i['market']}</i></td>
            <td style="padding:10px"><ul>{reason_list}</ul></td>
        </tr>"""

    html = f"<html><body><h2>Polymarket Insider Report</h2><table border='1' cellpadding='10' style='border-collapse:collapse'><tr><th>Score</th><th>Wallet</th><th>Volume</th><th>Market</th><th>Reasons</th></tr>{rows}</table></body></html>"
    
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🚨 Insider Alert: {len(insiders)} Suspicious Wallets"
    msg["From"] = SMTP_EMAIL
    msg["To"] = ALERT_RECIPIENT
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(SMTP_EMAIL, SMTP_PASSWORD)
        s.sendmail(SMTP_EMAIL, ALERT_RECIPIENT, msg.as_string())

# --- MAIN RUNNER ---
def main():
    print(f"[{datetime.now()}] Fetching on-chain data...")
    rows = get_dune_results(DUNE_QID)
    
    insiders = []
    for row in rows:
        wallet_data = {
            "wallet": row.get("wallet_address"),
            "total_usd": row.get("total_usd", 0),
            "lifetime_trades": row.get("lifetime_count", 0),
            "trades_per_hour": row.get("trades_last_24h", 0) / 24,
            "age_hours": row.get("wallet_age_hours", 999),
            "max_zscore": row.get("max_zscore", 0),
            "market": row.get("market_question", "N/A")
        }
        
        score, reasons = calculate_insider_score(wallet_data)
        if score >= MIN_INSIDER_SCORE:
            wallet_data['score'] = score
            wallet_data['reasons'] = reasons
            insiders.append(wallet_data)

    if insiders:
        print(f"Found {len(insiders)} suspicious wallets. Sending alert...")
        send_alert(insiders)
    else:
        print("No suspicious activity detected.")

if __name__ == "__main__":
    main()
