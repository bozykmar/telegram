import os
import requests

# Adres portfela Hyperliquid
WALLET_ADDRESS = "0x936cf4fb95c30ce83f658b5bbb247e4bb381bb0f"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def get_hyperliquid_positions(address):
    url = "https://api.hyperliquid.xyz/info"
    headers = {"Content-Type": "application/json"}
    
    positions = []
    
    # 1. Pobieranie pozycji PERP (kontrakty terminowe)
    payload_perp = {
        "type": "clearinghouseState",
        "user": address
    }
    
    try:
        res_perp = requests.post(url, json=payload_perp, headers=headers).json()
        asset_positions = res_perp.get("assetPositions", [])
        
        for item in asset_positions:
            pos = item.get("position", {})
            szi = float(pos.get("szi", 0))
            if szi != 0:
                coin = pos.get("coin")
                entry_px = float(pos.get("entryPx", 0))
                position_value = float(pos.get("positionValue", 0))
                upnl = float(pos.get("unrealizedPnl", 0))
                direction = "LONG" if szi > 0 else "SHORT"
                color_icon = "🟢" if szi > 0 else "🔴"
                
                leverage_raw = pos.get("leverage", {}).get("value", 1)
                try:
                    leverage_val = float(leverage_raw)
                except (ValueError, TypeError):
                    leverage_val = 1.0

                margin_used = float(pos.get("marginUsed", 0))
                if margin_used <= 0 and leverage_val > 0:
                    margin_used = position_value / leverage_val
                
                pnl_pct = (upnl / margin_used * 100) if margin_used > 0 else 0.0
                
                positions.append({
                    "coin": coin,
                    "direction": direction,
                    "color_icon": color_icon,
                    "entry_px": entry_px,
                    "val": position_value,
                    "upnl": upnl,
                    "pnl_pct": pnl_pct,
                    "leverage": leverage_raw
                })
    except Exception as e:
        print(f"Błąd pobierania perps: {e}")

    # 2. Pobieranie pozycji SPOT / HIP-1 (np. xyz:BRENTOIL)
    payload_spot = {
        "type": "spotClearinghouseState",
        "user": address
    }
    
    try:
        res_spot = requests.post(url, json=payload_spot, headers=headers).json()
        balances = res_spot.get("balances", [])
        
        for b in balances:
            coin = b.get("coin")
            total_sz = float(b.get("total", 0))
            entry_px = float(b.get("entryPx", 0)) if b.get("entryPx") else 0.0
            
            # Pomijamy małe/puste pozycje
            if total_sz > 0 and coin not in ["USDC", "USDT"]:
                # Jeśli brakuje ceny wejścia lub wartości w obiekcie spot
                val = float(b.get("hold", 0)) + float(b.get("total", 0))
                # Próba oszacowania wartości pozycji jeśli API spot dostarcza wartość
                pos_val = float(b.get("entryPx", 0)) * total_sz if entry_px > 0 else total_sz
                
                positions.append({
                    "coin": f"xyz:{coin}" if not coin.startswith("xyz:") else coin,
                    "direction": "SPOT",
                    "color_icon": "⚪",
                    "entry_px": entry_px,
                    "val": pos_val,
                    "upnl": 0.0,
                    "pnl_pct": 0.0,
                    "leverage": "1"
                })
    except Exception as e:
        print(f"Błąd pobierania spot: {e}")
            
    # Sortowanie pozycji malejąco po wartości pozycji (positionValue)
    positions.sort(key=lambda x: x["val"], reverse=True)
    return positions[:5]

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    requests.post(url, json=payload)

def main():
    top5 = get_hyperliquid_positions(WALLET_ADDRESS)
    
    if not top5:
        send_telegram_message("Brak aktywnych pozycji na portfelu.")
        return

    msg = "[#1 WhaleWallet](https://hyperbot.network/trader/0x936cf4fb95c30ce83f658b5bbb247e4bb381bb0f)\n\n"
    
    for i, p in enumerate(top5, 1):
        pnl_sign = "+" if p["upnl"] >= 0 else ""
        
        msg += f"*{i}. {p['coin']}* | {p['color_icon']} *{p['direction']}* *{p['leverage']}x*\n"
        msg += f"   Position: **${p['val']:,.2f}**\n"
        
        if p["direction"] != "SPOT":
            msg += f"   uPnL: **{pnl_sign}${p['upnl']:,.2f}** (**{pnl_sign}{p['pnl_pct']:.2f}%**)\n"
        
        msg += f"   Avg. Price: **${p['entry_px']:,.4f}**\n\n"

    send_telegram_message(msg)

if __name__ == "__main__":
    main()
