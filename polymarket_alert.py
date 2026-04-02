#!/usr/bin/env python3
"""
Polymarket Insider Detection v4.1 (Bot-Filtered Edition)
Logic: Penalizes high-frequency 'churn' and rewards low-history conviction.
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

# THRESHOLDS
MIN_INSIDER_SCORE = 60      # Alert threshold
BOT_TRADE_CEILING = 300     # If >300 lifetime trades, it's a professional/bot
VELOCITY_CAP = 10           # Max trades per hour before we assume it's an algorithm

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

def calculate_insider_score(wallet_data):
    """
    The 'Oracle' Scoring Model:
    Prioritizes wallets that look like humans with an edge, not machines with a script.
    """
    score = 0
    reasons = []
    
    lt = wallet_data.get("lifetime_trades", 0)
    v = wallet_data.get("trades_per_hour", 0)
    age = wallet_data.get("age_hours", 999)
    total_usd = wallet_data.get("total_usd", 0)
    z = float(wallet_data.get("max_zscore", 0))

    # 1. THE BOT KILLER (Hard Suppression)
    if lt > BOT_TRADE_CEILING:
        # Professional market makers/arbitrageurs are not 'insiders'
        return 0, ["Excluded: Institutional/Bot activity signature"]

    if v > VELOCITY_CAP:
        # High frequency is a technical edge, not an informational one.
        score -= 40
        reasons.append(f"High velocity ({v:.1f} tr/hr) - Likely automated")

    # 2. NEW WALLET / BURNER (Informational Edge)
    if age <= 48:
        score += 35
        reasons.append(f"Fresh 'Burner' wallet ({age:.0f}h old)")
    elif age <= 168:
        score += 15
        reasons.append("New account (<1 week)")

    # 3. STATISTICAL ANOMALY (The 'Fat Finger' of God)
    # We only care about high Z-scores if the wallet isn't a professional.
    if z >= 3.0:
        boost = 45 if lt < 20 else 10
        score += boost
        reasons.append(f"Anomalous trade size ({z:.1f}σ)")

    # 4. CONVICTION RATIO
    # Insiders don't 'trade'—they 'bet'. Check USD per trade.
    usd_per_trade = total_usd / lt if lt > 0 else 0
    if usd_per_trade > 5000:
        score += 25
        reasons.append(f"High conviction: ${usd_per_trade:,.0f}/trade")

    # 5. FUNDING ATTRIBUTION
    if wallet_data.get("is_bridge_funded"):
        score += 20
        reasons.append("Funded via Bridge/CEX (Privacy-seeking)")

    return max(0, min(score, 100)), reasons

def process_dune_data(rows):
    scored_wallets = []
    for row in rows:
        # Transform Dune row to our model
        wallet_meta = {
            "wallet": row.get("wallet_address"),
            "total_usd": row.get("total_usd", 0),
            "lifetime_trades": row.get("lifetime_count", 0),
            "trades_per_hour": row.get("trades_last_24h", 0) / 24,
            "age_hours": row.get("wallet_age_hours", 999),
            "max_zscore": row.get("max_zscore", 0),
            "is_bridge_funded": row.get("is_bridge", False),
            "question": row.get("market_question", "Unknown")
        }
        
        final_score, factors = calculate_insider_score(wallet_meta)
        
        if final_score >= MIN_INSIDER_SCORE:
            wallet_meta["score"] = final_score
            wallet_meta["reasons"] = factors
            scored_wallets.append(wallet_meta)
            
    return sorted(scored_wallets, key=lambda x: x["score"], reverse=True)

# --- INTEGRATE WITH YOUR EXISTING SCRIPT STRUCTURE ---
def main():
    print("🔍 Filtering the noise out of the mempool...")
    
    # [Insert your Dune API fetch logic here]
    # mock_data = dune.get_latest_result(DUNE_QID_LARGE_TRADES)
    
    # insiders = process_dune_data(mock_data)
    
    # for i in insiders:
    #    print(f"ALERT: {i['wallet']} | Score: {i['score']} | {', '.join(i['reasons'])}")

if __name__ == "__main__":
    main()
