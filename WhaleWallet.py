import os
import requests

# Adres portfela Hyperliquid
WALLET_ADDRESS = "0x936cf4fb95c30ce83f658b5bbb247e4bb381bb0f"  # Wpisz pełny adres portfela

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def get_hyperliquid_positions(address):
    url = "https://api.hyperliquid.xyz/info"
    headers = {"Content-Type": "application/json"}
    payload = {
        "type": "clearinghouseState",
        "user": address
    }
    
    response = requests.post(url, json=payload, headers=headers)
    data = response.json()
    
    asset_positions = data.get("assetPositions", [])
    positions = []
    
    for item in asset_positions:
        pos = item.get("position", {})
        szi = float(pos.get("szi", 0))
        if szi != 0:
            coin = pos.get("coin")
            entry_px = float(pos.get("entryPx", 0))
            position_value = float(pos.get("positionValue", 0))
            upnl = float(pos.get("unrealizedPnl", 0))
            direction = "LONG" if szi > 0 else "SHORT"
            leverage = pos.get("leverage", {}).get("value", "-")
            
            positions.append({
                "coin": coin,
                "direction": direction,
                "val": position_value,
                "upnl": upnl,
                "szi": abs(szi),
                "leverage": leverage
            })
            
    # Sortowanie pozycji malejąco po wartości pozycji (positionValue)
    positions.sort(key=lambda x: x["val"], reverse=True)
    return positions[:4]

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload)

def main():
    top4 = get_hyperliquid_positions(WALLET_ADDRESS)
    
    if not top4:
        send_telegram_message("Brak aktywnych pozycji na portfelu.")
        return

    msg = "* Raport 4 największych pozycji (Hyperliquid)*\n\n"
    for i, p in enumerate(top4, 1):
        pnl_sign = "+" if p["upnl"] >= 0 else ""
        msg += f"*{i}. {p['coin']}* ({p['direction']} {p['leverage']}x)\n"
        msg += f"   Wartość: `${p['val']:,.2f}`\n"
        msg += f"   uPnL: `{pnl_sign}${p['upnl']:,.2f}`\n\n"

    send_telegram_message(msg)

if __name__ == "__main__":
    main()
