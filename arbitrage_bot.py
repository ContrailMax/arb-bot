import os
import re
import json
import time
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timezone

SHEET_NAME = os.getenv("SHEET_NAME", "Asset Management")
LOG_TAB = os.getenv("LOG_TAB", "Log")
SETTING_TAB = os.getenv("SETTING_TAB", "Setting")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

def get_xe_rate(from_curr: str, to_curr: str) -> float:
    url = f"https://www.xe.com/currencyconverter/convert/?Amount=1&From={from_curr}&To={to_curr}"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    text = r.text

    # Pattern 1: "THB": 34.50
    m = re.search(rf'"{re.escape(to_curr)}"\s*:\s*(\d+\.\d+)', text)
    # Pattern 2 fallback: 34.50<span class="faded-digits">
    if not m:
        m = re.search(r'(\d+\.\d+)<span class="faded-digits">', text)

    if not m:
        raise RuntimeError(f"XE parse failed for {from_curr}->{to_curr}")
    return float(m.group(1))

def kucoin_prices(coin: str) -> tuple[float, float]:
    url = f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={coin}-USDT"
    r = requests.get(url, timeout=15)
    j = r.json()
    bid = float(j["data"]["bestBid"])
    ask = float(j["data"]["bestAsk"])
    return bid, ask

def luno_prices_myr(coin: str) -> tuple[float, float]:
    url = f"https://api.luno.com/api/1/ticker?pair={coin}MYR"
    r = requests.get(url, timeout=15)
    j = r.json()
    bid = float(j["bid"])
    ask = float(j["ask"])
    return bid, ask

def send_telegram(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    requests.post(url, json=payload, timeout=15)

def connect_sheet():
    creds_json = json.loads(os.environ["GDRIVE_API_KEY"])
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
    client = gspread.authorize(creds)
    ss = client.open(SHEET_NAME)
    return ss

def load_settings(ws):
    values = ws.get_all_values()
    m = {}
    for row in values:
        if len(row) >= 2 and row[0].strip():
            m[row[0].strip()] = row[1].strip()

    alert_bps = float(m.get("ALERT_BPS", "200"))
    cooldown_min = float(m.get("COOLDOWN_MIN", "10"))
    coins = [c.strip().upper() for c in m.get("COINS", "BTC,ETH").split(",") if c.strip()]
    tg_token = m.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat_id = m.get("TELEGRAM_CHAT_ID", "")
    return alert_bps, cooldown_min, coins, tg_token, tg_chat_id

def ensure_headers(log_ws):
    if log_ws.row_count == 0 or log_ws.get_all_values() == []:
        pass
    # If sheet is empty, add header
    if len(log_ws.get_all_values()) == 0:
        log_ws.append_row([
            "Timestamp", "Side", "Coin",
            "Luno(MYR)", "KuCoin(USDT)",
            "USD/MYR", "USD/THB", "MYR/THB",
            "Luno(USD)", "Spread(bps)", "Error"
        ])

def main():
    ss = connect_sheet()
    setting_ws = ss.worksheet(SETTING_TAB)
    log_ws = ss.worksheet(LOG_TAB)

    ensure_headers(log_ws)

    alert_bps, cooldown_min, coins, tg_token, tg_chat_id = load_settings(setting_ws)

    # XE FX
    usd_thb = get_xe_rate("USD", "THB")
    time.sleep(2)
    myr_thb = get_xe_rate("MYR", "THB")
    usd_myr = usd_thb / myr_thb

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rows = []
    alerts = []

    for coin in coins:
        try:
            ku_bid, ku_ask = kucoin_prices(coin)
            lu_bid, lu_ask = luno_prices_myr(coin)

            # Bid side row: Luno bid vs Ku ask
            luno_usd = lu_bid / usd_myr
            spr = 10000 * ((luno_usd - ku_ask) / ((luno_usd + ku_ask) / 2))
            rows.append([ts, "Bid", coin, lu_bid, ku_ask, usd_myr, usd_thb, myr_thb, luno_usd, spr, ""])
            if spr >= alert_bps:
                alerts.append((coin, "Bid", spr, luno_usd, ku_ask))

            # Ask side row: Luno ask vs Ku bid
            luno_usd = lu_ask / usd_myr
            spr = 10000 * ((luno_usd - ku_bid) / ((luno_usd + ku_bid) / 2))
            rows.append([ts, "Ask", coin, lu_ask, ku_bid, usd_myr, usd_thb, myr_thb, luno_usd, spr, ""])
            if spr >= alert_bps:
                alerts.append((coin, "Ask", spr, luno_usd, ku_bid))

        except Exception as e:
            rows.append([ts, "ERR", coin, "", "", usd_myr, usd_thb, myr_thb, "", "", str(e)])

    # Append rows in batch
    log_ws.append_rows(rows, value_input_option="USER_ENTERED")

    # Telegram alerts (no cooldown here unless you want me to add a State sheet)
    if tg_token and tg_chat_id:
        for coin, side, spr, luno_usd, ku_price in alerts:
            msg = (
                f"🚨 ARB ALERT\n"
                f"Coin: {coin}\n"
                f"Side: {side}\n"
                f"Spread: {spr:.2f} bps\n"
                f"Luno(USD): {luno_usd}\n"
                f"KuCoin(USDT): {ku_price}\n"
                f"USD/THB (XE): {usd_thb}\n"
                f"MYR/THB (XE): {myr_thb}\n"
                f"USD/MYR (derived): {usd_myr}\n"
                f"Time: {ts}"
            )
            send_telegram(tg_token, tg_chat_id, msg)

if __name__ == "__main__":
    main()
