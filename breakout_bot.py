import os
import time
import json
import requests
from datetime import datetime, timezone

# =========================
# ENV (Render -> Environment Variables)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Ayarlar (istersen Render env'den değiştirebilirsin)
NEAR_PCT = float(os.getenv("NEAR_PCT", "1.0"))          # Breakout seviyesine ne kadar yakınsa (yüzde)
LOOKBACK_4H = int(os.getenv("LOOKBACK_4H", "50"))       # 4H breakout için kaç mum geri bakılsın
LOOKBACK_1D = int(os.getenv("LOOKBACK_1D", "30"))       # 1D breakout için kaç mum geri bakılsın
CANDLE_LIMIT = int(os.getenv("CANDLE_LIMIT", "300"))    # API'den çekilecek mum limiti
SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "90"))   # tur arası bekleme
MAX_PAIRS = int(os.getenv("MAX_PAIRS", "120"))          # her turda taranacak max parite
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "180"))    # aynı pariteye tekrar sinyal atmadan önce (dk)

STATE_FILE = "state.json"

GATE_BASE = "https://api.gateio.ws/api/v4"

# =========================
# Helpers
# =========================
def now_ts() -> int:
    return int(time.time())

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_alert": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_alert": {}}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def send_message(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ BOT_TOKEN veya CHAT_ID eksik. Render Env'e ekle.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            print("Telegram send error:", r.status_code, r.text[:200])
    except Exception as e:
        print("Telegram send exception:", e)

def gate_get(path: str, params=None):
    url = f"{GATE_BASE}{path}"
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

# =========================
# Gate.io Market Data
# =========================
def fetch_usdt_pairs():
    """
    Spot market USDT quote pariteleri.
    Gate format: BTC_USDT
    """
    tickers = gate_get("/spot/tickers")
    pairs = []
    for t in tickers:
        cur = t.get("currency_pair", "")
        if cur.endswith("_USDT"):
            pairs.append(cur)
    # stabil sıralama
    pairs.sort()
    return pairs

def fetch_last_price(pair: str) -> float:
    t = gate_get("/spot/tickers", params={"currency_pair": pair})
    # API bazen liste döndürür
    if isinstance(t, list) and len(t) > 0:
        return float(t[0].get("last", "0"))
    if isinstance(t, dict):
        return float(t.get("last", "0"))
    return 0.0

def fetch_candles(pair: str, interval: str, limit: int):
    """
    interval örn: '4h', '1d'
    Gate candles: /spot/candlesticks
    dönüş: list of [t, v, c, h, l, o] (string)
    """
    data = gate_get("/spot/candlesticks", params={
        "currency_pair": pair,
        "interval": interval,
        "limit": limit
    })
    # en yeni -> en eski gelebilir, biz zamanına göre sırala
    # t alanı epoch seconds string
    data.sort(key=lambda x: int(x[0]))
    return data

def highest_high(candles, lookback: int):
    # candles format: [t, v, c, h, l, o]
    # son mum hariç lookback kadar geriye bak (breakout için geçmiş tepe)
    if len(candles) < lookback + 2:
        return None
    subset = candles[-(lookback+1):-1]  # son (current) mum hariç
    highs = [float(x[3]) for x in subset]
    return max(highs) if highs else None

# =========================
# Breakout Logic
# =========================
def is_near_breakout(last_price: float, level: float, near_pct: float) -> bool:
    if level <= 0:
        return False
    diff_pct = (last_price - level) / level * 100.0
    return diff_pct >= 0 and diff_pct <= near_pct

def is_confirmed_breakout(candles, level: float) -> bool:
    """
    Basit onay:
    - Son kapanış (c) level'ın üstünde
    """
    if not candles or level is None:
        return False
    last_close = float(candles[-1][2])
    return last_close > level

def cooldown_ok(state, key: str, cooldown_min: int) -> bool:
    last = state.get("last_alert", {}).get(key, 0)
    return (now_ts() - last) >= cooldown_min * 60

def mark_alert(state, key: str):
    state.setdefault("last_alert", {})[key] = now_ts()

# =========================
# Main loop
# =========================
def main():
    state = load_state()

    send_message("✅ Breakout bot (Gate.io) başladı. 4H & 1D tarama aktif.")
    pairs = fetch_usdt_pairs()
    send_message(f"📌 Toplam {len(pairs)} USDT parite bulundu. Her turda MAX_PAIRS={MAX_PAIRS} taranacak.")

    idx = 0
    while True:
        try:
            # her turda MAX_PAIRS kadar parite döndür (round-robin)
            batch = []
            for _ in range(min(MAX_PAIRS, len(pairs))):
                batch.append(pairs[idx % len(pairs)])
                idx += 1

            for pair in batch:
                try:
                    last_price = fetch_last_price(pair)
                    if last_price <= 0:
                        continue

                    # 4H
                    c4 = fetch_candles(pair, "4h", CANDLE_LIMIT)
                    lvl4 = highest_high(c4, LOOKBACK_4H)
                    if lvl4 and cooldown_ok(state, f"{pair}|4h", COOLDOWN_MIN):
                        if is_confirmed_breakout(c4, lvl4) and is_near_breakout(last_price, lvl4, NEAR_PCT):
                            msg = (
                                f"🚀 4H Breakout Yakın/Onay!\n"
                                f"Pair: {pair}\n"
                                f"Fiyat: {last_price:.6g}\n"
                                f"Seviye(4H {LOOKBACK_4H}H): {lvl4:.6g}\n"
                                f"Yakınlık: <= %{NEAR_PCT}\n"
                                f"Zaman: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                            )
                            send_message(msg)
                            mark_alert(state, f"{pair}|4h")
                            save_state(state)

                    # 1D
                    c1 = fetch_candles(pair, "1d", CANDLE_LIMIT)
                    lvl1 = highest_high(c1, LOOKBACK_1D)
                    if lvl1 and cooldown_ok(state, f"{pair}|1d", COOLDOWN_MIN):
                        if is_confirmed_breakout(c1, lvl1) and is_near_breakout(last_price, lvl1, NEAR_PCT):
                            msg = (
                                f"🔥 1D Breakout Yakın/Onay!\n"
                                f"Pair: {pair}\n"
                                f"Fiyat: {last_price:.6g}\n"
                                f"Seviye(1D {LOOKBACK_1D}D): {lvl1:.6g}\n"
                                f"Yakınlık: <= %{NEAR_PCT}\n"
                                f"Zaman: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                            )
                            send_message(msg)
                            mark_alert(state, f"{pair}|1d")
                            save_state(state)

                    time.sleep(0.2)

                except Exception as e:
                    print("pair error:", pair, e)

            time.sleep(SLEEP_SECONDS)

        except Exception as e:
            print("loop error:", e)
            time.sleep(10)

if __name__ == "__main__":
    main()
