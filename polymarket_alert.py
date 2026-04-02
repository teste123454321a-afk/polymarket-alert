def calculate_insider_score(...):
    ...  # existing code
    if lt > 200 or v > 20:
        score *= 0.2
        notes.append("Suppressed: High-frequency signature (likely Bot/MM)")
    elif age < 72 and total_usd > 5000:
        score += 50
        notes.append("High-Conviction Entry: New wallet + Large Directional Bet")
    return score  # existing return statement
