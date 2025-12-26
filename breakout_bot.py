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

# Ayarlar
NEAR_PCT = float(os.getenv("NEAR_PCT", "1.0"))          # Dirence % kaç kala uyarı? (1.0 => %1 kala)
LOOKBACK_4H = int(os.getenv("LOOKBACK_4H", "120"))      # 4h swing direnci için kaç mum geriye bakılsın
LOOKBACK_1D = int(os.getenv("LOOKBACK_1D", "120"))      # 1d swing direnci için kaç mum
CANDLE_LIMIT = int(os.getenv("CANDLE_LIMIT", "200"))    # Gate limit (max 1000 ama biz 200 yeter)
SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "60"))   # Döngü arası bekleme
MAX_PAIRS = int(os.getenv("MAX_PAIRS", "120"))          # Her turda kaç USDT parite taransın
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "180"))    # Aynı uyarıyı tekrar atma süresi (dk)

STATE_FILE = "state.json"

GATE_BASE = "https://api.gateio.ws/api/v4"


# =========================
# Utils
# =========================
def now_utc_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass

def tg_send(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("BOT_TOKEN / CHAT_ID eksik.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            print("Telegram send error:", r.status_code, r.text[:200])
    except Exception as e:
        print("Telegram exception:", e)

def gate_get(path: str, params: dict | None = None):
    url = f"{GATE_BASE}{path}"
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def fmt_pct(x: float) -> str:
    return f"{x:.2f}%"

def safe_float(x) -> float:
    try:
        return float(x)
    except Exception:
        return float("nan")


# =========================
# Gate.io Data
# Candles format (Gate Spot):
# response: list of arrays like:
# [timestamp, volume, close, high, low, open]
# all fields are strings
# =========================
def get_usdt_pairs() -> list[str]:
    pairs = gate_get("/spot/currency_pairs")
    out = []
    for p in pairs:
        try:
            if p.get("quote") == "USDT" and p.get("trade_status") == "tradable":
                out.append(p["id"])  # e.g. "BTC_USDT"
        except Exception:
            continue
    # Çok fazla olmasın diye stabil sırala
    out.sort()
    return out

def get_candles(pair: str, interval: str, limit: int):
    # interval: "4h" or "1d"
    data = gate_get("/spot/candlesticks", params={
        "currency_pair": pair,
        "interval": interval,
        "limit": limit
    })
    # Gate genelde en yeni mumdan eskiye döndürür; biz kronolojik yapalım:
    data = list(reversed(data))
    return data

def swing_resistance(candles, lookback: int) -> float:
    """
    Basit swing direnç:
    - son mum hariç (henüz kapanmamış olabilir)
    - son 'lookback' mum içindeki en yüksek 'high'
    """
    if len(candles) < 10:
        return float("nan")
    usable = candles[:-1]  # son mumu çıkar
    if lookback > len(usable):
        lookback = len(usable)
    window = usable[-lookback:]
    highs = [safe_float(c[3]) for c in window]  # high index=3
    return max(highs) if highs else float("nan")

def last_close(candles) -> float:
    if not candles:
        return float("nan")
    return safe_float(candles[-1][2])  # close index=2


# =========================
# Signal Logic
# =========================
def check_pair(pair: str, state: dict):
    """
    4h ve 1d için:
    - Dirence yakın: close, dirençten NEAR_PCT aşağıdaysa
    - Breakout: close direnç üstüne attıysa
    """
    results = []
    for tf, interval, lookback in [
        ("4H", "4h", LOOKBACK_4H),
        ("1D", "1d", LOOKBACK_1D),
    ]:
        candles = get_candles(pair, interval=interval, limit=CANDLE_LIMIT)
        c = last_close(candles)
        r = swing_resistance(candles, lookback=lookback)
        if not (c > 0 and r > 0):
            continue

        dist_pct = (r - c) / r * 100.0  # dirençten ne kadar aşağıda
        breakout = c > r
        near = (0 <= dist_pct <= NEAR_PCT)

        # cooldown key
        key = f"{pair}:{tf}:{'breakout' if breakout else 'near' if near else 'none'}"
        last_sent = state.get(key, 0)
        cooldown_sec = COOLDOWN_MIN * 60

        if breakout:
            if now_utc_ts() - last_sent >= cooldown_sec:
                results.append((tf, "BREAKOUT", c, r, dist_pct, key))
        elif near:
            if now_utc_ts() - last_sent >= cooldown_sec:
                results.append((tf, "NEAR", c, r, dist_pct, key))

    return results


def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ BOT_TOKEN veya CHAT_ID yok. Render -> Environment Variables ekle.")
        return

    state = load_state()
    tg_send("✅ Breakout bot (Gate.io) başladı. 4H & 1D tarama aktif.")

    pairs = get_usdt_pairs()
    if not pairs:
        tg_send("❌ USDT pariteleri çekilemedi (Gate.io).")
        return

    tg_send(f"📌 Toplam {len(pairs)} USDT parite bulundu. Her turda MAX_PAIRS={MAX_PAIRS} taranacak.")

    idx = 0
    while True:
        try:
            # döngüde pariteleri sırayla gez
            batch = []
            for _ in range(MAX_PAIRS):
                batch.append(pairs[idx])
                idx = (idx + 1) % len(pairs)

            for pair in batch:
                try:
                    hits = check_pair(pair, state)
                    for tf, kind, close_p, res_p, dist_pct, key in hits:
                        symbol = pair.replace("_", "/")
                        if kind == "BREAKOUT":
                            msg = (
                                f"🚀 BREAKOUT ({tf})\n"
                                f"{symbol}\n"
                                f"Close: {close_p:.6g}\n"
                                f"Direnç: {res_p:.6g}\n"
                                f"Mesafe: -{fmt_pct(abs(dist_pct))} (üstünde)\n"
                            )
                        else:
                            msg = (
                                f"⚠️ Dirence Yakın ({tf})\n"
                                f"{symbol}\n"
                                f"Close: {close_p:.6g}\n"
                                f"Direnç: {res_p:.6g}\n"
                                f"Kalan: {fmt_pct(dist_pct)}\n"
                            )
                        tg_send(msg)
                        state[key] = now_utc_ts()
                        save_state(state)

                except requests.HTTPError as e:
                    # Gate bazen rate limit / geçici hata
                    print("HTTPError:", pair, str(e))
                    continue
                except Exception as e:
                    print("Err:", pair, e)
                    continue

            time.sleep(SLEEP_SECONDS)

        except Exception as e:
            print("Loop error:", e)
            time.sleep(10)


if __name__ == "__main__":
    main()
