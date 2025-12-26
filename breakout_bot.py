import os
import time
import math
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# =========================================================
# ENV (Render -> Environment Variables)
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Render Web Service port (zorunlu)
PORT = int(os.getenv("PORT", "10000"))

# Genel ayarlar
SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "120"))          # kaç saniyede bir tarasın
MAX_PAIRS = int(os.getenv("MAX_PAIRS", "120"))                  # her turda kaç parite
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "15"))             # aynı coin tekrar mesaj min.
COOLDOWN_SEC = COOLDOWN_MIN * 60

# Breakout yakınlık / onay
NEAR_PCT = float(os.getenv("NEAR_PCT", "1.0"))                  # dirence yakınlık %
CONFIRM_BREAKOUT = os.getenv("CONFIRM_BREAKOUT", "1") == "1"    # 1=close üstünde onay

# Timeframe lookback (direnç hesap)
LOOKBACK_1H = int(os.getenv("LOOKBACK_1H", "50"))
LOOKBACK_4H = int(os.getenv("LOOKBACK_4H", "50"))
LOOKBACK_1D = int(os.getenv("LOOKBACK_1D", "30"))

# EMA ve hacim filtresi
EMA_FAST = int(os.getenv("EMA_FAST", "21"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "50"))
VOL_LOOKBACK = int(os.getenv("VOL_LOOKBACK", "20"))             # ortalama hacim için geçmiş bar
VOL_MULT = float(os.getenv("VOL_MULT", "1.8"))                  # son hacim >= ort * VOL_MULT

# Fake breakout filtresi (mum gövdesi)
MIN_BODY_PCT = float(os.getenv("MIN_BODY_PCT", "0.25"))         # body/(high-low) en az

STATE_FILE = "state.json"
GATE_BASE = "https://api.gateio.ws/api/v4"

# =========================================================
# Basit HTTP server (Render Web Service port zorunluluğu için)
# =========================================================
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

def start_http_server():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()

# =========================================================
# Telegram
# =========================================================
def send_message(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("BOT_TOKEN / CHAT_ID eksik.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload, timeout=20)
    except Exception as e:
        print("Telegram hata:", e)

# =========================================================
# State (cooldown)
# =========================================================
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_sent": {}}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass

def can_send(state, key: str) -> bool:
    now = int(time.time())
    last = int(state["last_sent"].get(key, 0))
    return (now - last) >= COOLDOWN_SEC

def mark_sent(state, key: str):
    state["last_sent"][key] = int(time.time())

# =========================================================
# Gate.io yardımcılar
# =========================================================
def gate_get(path, params=None):
    url = f"{GATE_BASE}{path}"
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def get_usdt_pairs():
    # spot currency_pairs
    data = gate_get("/spot/currency_pairs")
    pairs = []
    for p in data:
        # id örn: "BTC_USDT"
        pid = p.get("id", "")
        if pid.endswith("_USDT") and p.get("trade_status") == "tradable":
            pairs.append(pid)
    return pairs

def get_candles(pair: str, interval: str, limit: int):
    # /spot/candlesticks -> array, newest last (çoğu zaman)
    # fields: [t, volume, close, high, low, open] (Gate v4)
    data = gate_get("/spot/candlesticks", params={"currency_pair": pair, "interval": interval, "limit": limit})
    # Gate bazen newest first döndürebiliyor; timestamp'e göre sırala
    candles = []
    for row in data:
        try:
            t = int(row[0])
            v = float(row[1])
            c = float(row[2])
            h = float(row[3])
            l = float(row[4])
            o = float(row[5])
            candles.append((t, o, h, l, c, v))
        except Exception:
            continue
    candles.sort(key=lambda x: x[0])
    return candles

# =========================================================
# İndikatörler
# =========================================================
def ema(values, period: int):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for x in values[1:]:
        e = (x * k) + (e * (1 - k))
    return e

def ema_trend(closes, fast, slow):
    ef = ema(closes, fast)
    es = ema(closes, slow)
    if ef is None or es is None:
        return None
    if ef > es:
        return "UP"
    if ef < es:
        return "DOWN"
    return "FLAT"

def volume_spike(volumes, lookback, mult):
    if len(volumes) < lookback + 1:
        return False
    recent = volumes[-1]
    avg = sum(volumes[-(lookback+1):-1]) / lookback
    if avg <= 0:
        return False
    return recent >= avg * mult

def body_ok(o, h, l, c, min_body_pct):
    rng = h - l
    if rng <= 0:
        return False
    body = abs(c - o)
    return (body / rng) >= min_body_pct

# =========================================================
# Breakout logic
# =========================================================
def analyze_pair(pair: str, state):
    # --- 1H / 4H / 1D trend (EMA filtresi)
    c1h = get_candles(pair, "1h", max(LOOKBACK_1H, EMA_SLOW + 5, VOL_LOOKBACK + 5))
    c4h = get_candles(pair, "4h", max(LOOKBACK_4H, EMA_SLOW + 5, VOL_LOOKBACK + 5))
    c1d = get_candles(pair, "1d", max(LOOKBACK_1D, EMA_SLOW + 5, VOL_LOOKBACK + 5))

    if len(c4h) < 10 or len(c1d) < 10:
        return

    closes_1h = [x[4] for x in c1h]
    closes_4h = [x[4] for x in c4h]
    closes_1d = [x[4] for x in c1d]

    trend_1h = ema_trend(closes_1h[-(EMA_SLOW+5):], EMA_FAST, EMA_SLOW)
    trend_4h = ema_trend(closes_4h[-(EMA_SLOW+5):], EMA_FAST, EMA_SLOW)
    trend_1d = ema_trend(closes_1d[-(EMA_SLOW+5):], EMA_FAST, EMA_SLOW)

    aligned = (trend_1h == trend_4h == trend_1d) and trend_1h in ("UP", "DOWN")

    # --- Breakout taraması: 4H ve 1D üzerinde direnç/tepe
    checks = [
        ("4H", c4h, LOOKBACK_4H, "4h"),
        ("1D", c1d, LOOKBACK_1D, "1d"),
    ]

    for tf_name, candles, lb, interval in checks:
        # Direnç: son lb barın (son bar hariç) en yüksek high'ı
        window = candles[-(lb+1):-1] if len(candles) >= lb + 2 else candles[:-1]
        if len(window) < 5:
            continue

        resistance = max(x[2] for x in window)  # high
        last = candles[-1]
        t, o, h, l, c, v = last

        # yakınlık hesabı (close dirence ne kadar yakın)
        if resistance <= 0:
            continue
        dist_pct = abs(resistance - c) / resistance * 100

        # Fake breakout filtreleri
        vols = [x[5] for x in candles]
        vol_ok = volume_spike(vols, VOL_LOOKBACK, VOL_MULT)
        body_good = body_ok(o, h, l, c, MIN_BODY_PCT)

        # Sinyal türü: Yakınlık veya Onaylı breakout
        is_near = (dist_pct <= NEAR_PCT)
        is_break = (c > resistance) if CONFIRM_BREAKOUT else (h > resistance)

        # Trend filtresi (UP/DOWN’a göre davran)
        # Biz “dirence yakın / breakout”ı daha çok long mantığında ele alıyoruz:
        # Trend UP değilse sinyali bastır (istersen env ile açarsın)
        trend_ok = (trend_4h == "UP" or trend_1d == "UP")

        # cooldown anahtarı: pair + timeframe
        key = f"{pair}:{tf_name}"
        if not can_send(state, key):
            continue

        if (is_break or is_near) and trend_ok and vol_ok and body_good:
            when = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            left_pct = (resistance - c) / resistance * 100
            left_pct_abs = abs(left_pct)

            direction_tag = ""
            if aligned:
                direction_tag = f"\n🧭 Trend: 1H+4H+1D = {trend_1h}"

            title = "🚀 Breakout Onay!" if is_break else "⚠️ Dirence Yakın"
            msg = (
                f"{title} ({tf_name})\n"
                f"Pair: {pair}\n"
                f"Close: {c:.8g}\n"
                f"Direnç: {resistance:.8g}\n"
                f"Kalan: {left_pct_abs:.2f}%\n"
                f"📈 Hacim: x{VOL_MULT} OK\n"
                f"📊 EMA({EMA_FAST}/{EMA_SLOW}) 4H:{trend_4h} 1D:{trend_1d}\n"
                f"🕒 Zaman: {when}"
                f"{direction_tag}"
            )
            send_message(msg)
            mark_sent(state, key)

def main_loop():
    state = load_state()

    send_message("✅ Breakout bot (Gate.io) başladı. 4H & 1D tarama aktif.\n"
                 f"Cooldown: {COOLDOWN_MIN}dk | VOL x{VOL_MULT} | EMA {EMA_FAST}/{EMA_SLOW}")

    while True:
        try:
            pairs = get_usdt_pairs()
            send_message(f"📌 Toplam {len(pairs)} USDT parite bulundu. Her turda MAX_PAIRS={MAX_PAIRS} taranacak.")
            # basit örnek: ilk MAX_PAIRS
            for pair in pairs[:MAX_PAIRS]:
                try:
                    analyze_pair(pair, state)
                except Exception as e:
                    # tek parite patlarsa döngü ölmesin
                    print("pair hata", pair, e)

            save_state(state)
        except Exception as e:
            print("Genel hata:", e)

        time.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    # Web Service'in port testini geçmek için HTTP server
    t = threading.Thread(target=start_http_server, daemon=True)
    t.start()

    # Bot döngüsü
    main_loop()
    
