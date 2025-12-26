import os, time
import requests
import ccxt
import pandas as pd

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

TIMEFRAMES = ["4h", "1d"]
LIMIT = 200
LOOKBACK = 50
NEAR_PCT = 0.8
SCAN_EVERY_SEC = 300
COOLDOWN = 3600

def send(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg})

def get_data(ex, symbol, tf):
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=tf, limit=LIMIT)
    df = pd.DataFrame(ohlcv, columns=["t","o","h","l","c","v"])
    return df

def find_breakout(df):
    high = df["h"]
    last = df["c"].iloc[-1]
    res = high[-LOOKBACK:-1].max()
    dist = (res - last) / res * 100
    return res, dist

def main():
    ex = ccxt.binance()
    markets = ex.load_markets()
    symbols = [s for s in markets if s.endswith("/USDT")]

    send("🤖 Breakout bot aktif!")

    while True:
        for sym in symbols:
            try:
                df = get_data(ex, sym, "4h")
                res, dist = find_breakout(df)
                if 0 < dist < NEAR_PCT:
                    send(f"🚨 {sym} dirence yaklaştı!\nMesafe: %{dist:.2f}")
                    time.sleep(COOLDOWN)
            except:
                pass
        time.sleep(300)

if __name__ == "__main__":
    main()
