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

# Tarama ayarları
LOOKBACK_4H = int(os.getenv("LOOKBACK_4H", "50"))      # direnç için geçmiş mum sayısı
LOOKBACK_1D = int(os.getenv("LOOKBACK_1D", "50"))
CANDLE_LIMIT = int(os.getenv("CANDLE_LIMIT", "220"))   # mum çekme limiti
SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "60"))
MAX_PAIRS = int(os.getenv("MAX_PAIRS", "120"))         # her tur taranacak max pair
NEAR_PCT = float(os.getenv("NEAR_PCT", "1.0"))         # dirence yakınlık yüzdesi

# Filtreler
EMA_FAST = int(os.getenv("EMA_FAST", "21"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "55"))

VOL_MULT = float(os.getenv("VOL_MULT", "1.8"))          # son kapanan mum hacmi >= ort*VOL_MULT
VOL_AVG_LEN = int(os.getenv("VOL_AVG_LEN", "20"))

WICK_MAX_PCT = float(os.getenv("WICK_MAX_PCT", "0.6"))  # üst fitil % max
BODY_MIN_PCT = float(os.getenv("BODY_MIN_PCT", "0.2"))  # gövde % min

COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "15"))     # aynı coin aynı tf mesaj aralığı
TREND_TFS = [x.strip().lower() for x in os.getenv("TREND_TFS", "1h,4h,1d").split(",") if x.strip()]

STATE_FILE = "state.json"
GATE_BASE = "https://api.gateio.ws/api/v4"


# =========================
# Helpers
# =========================
def now_ts():
    return int(time.time())

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_alert_ts": {}}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass

def send_message(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("BOT_TOKEN/CHAT_ID eksik. Mesaj gönderilemedi.")
        print(text)
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        requests.post(url, json=payload, timeout=20)
    except Exception as e:
        print("Telegram send error:", e)

def cooldown_ok(state, key: str):
    last = state.get("last_alert_ts", {}).get(key, 0)
    return (now_ts() - last) >= COOLDOWN_MIN * 60

def mark_alert(state, key: str):
    state.setdefault("last_alert_ts", {})
    state["last_alert_ts"][key] = now_ts()


# =========================
# EMA / Filters
# =========================
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = (v * k) + (e * (1 - k))
    return e

def ema_series(values, period):
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out = []
    e = values[0]
    out.append(e)
    for v in values[1:]:
        e = (v * k) + (e * (1 - k))
        out.append(e)
    return out

def crossed_up(closes, fast=EMA_FAST, slow=EMA_SLOW, lookback=8):
    # Son lookback mum içinde EMA fast, EMA slow'u yukarı kesti mi?
    need = max(fast, slow) + lookback + 5
    if len(closes) < need:
        return False
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    start = max(1, len(closes) - lookback - 2)
    for i in range(start, len(closes)):
        if ef[i-1] <= es[i-1] and ef[i] > es[i]:
            return True
    return False

def trend_up(closes):
    # EMA_FAST > EMA_SLOW ise trend yukarı
    if len(closes) < max(EMA_FAST, EMA_SLOW) + 5:
        return False
    ef = ema(closes, EMA_FAST)
    es = ema(closes, EMA_SLOW)
    return (ef is not None and es is not None and ef > es)

def volume_ok(volumes):
    # Son kapanan mum hacmi / ortalama hacim >= VOL_MULT
    if len(volumes) < VOL_AVG_LEN + 5:
        return False
    last = volumes[-2]  # kapanmış son mum
    avg_slice = volumes[-(VOL_AVG_LEN+2):-2]
    avg = sum(avg_slice) / max(1, len(avg_slice))
    if avg <= 0:
        return False
    return last >= avg * VOL_MULT

def fake_breakout_filter(last_closed_candle):
    # candle: [t, volume, close, high, low, open] (Gate format)
    # biz parse ederken o,h,l,c,v yapacağız. burada (o,h,l,c) üzerinden bakıyoruz.
    o, h, l, c, v = last_closed_candle
    if o <= 0 or c <= 0:
        return False

    body_pct = abs(c - o) / o * 100.0
    upper_wick_pct = (h - max(o, c)) / c * 100.0

    # Gövde çok küçükse veya üst fitil çok uzunsa fake ihtimali
    if body_pct < BODY_MIN_PCT:
        return False
    if upper_wick_pct > WICK_MAX_PCT:
        return False
    return True


# =========================
# Gate.io API
# =========================
_session = requests.Session()

def gate_get(path, params=None):
    url = f"{GATE_BASE}{path}"
    r = _session.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def list_usdt_pairs():
    # /spot/tickers -> currency_pair listesi
    data = gate_get("/spot/tickers")
    pairs = []
    for item in data:
        cp = item.get("currency_pair", "")
        if not cp:
            continue
        # USDT quote filtre
        if cp.endswith("_USDT"):
            pairs.append(cp)
    # stabil olsun diye sort
    pairs.sort()
    return pairs

def fetch_gate_candles(symbol, tf, limit=CANDLE_LIMIT):
    # Gate endpoint: /spot/candlesticks
    # interval: 1m,5m,15m,30m,1h,4h,1d
    params = {"currency_pair": symbol, "interval": tf, "limit": str(limit)}
    data = gate_get("/spot/candlesticks", params=params)
    # Gate candlestick: [t, v, c, h, l, o] strings
    # Sıralama genelde newest-first geliyor; biz oldest-first yapacağız.
    # Güvenli olsun:
    if not data:
        return []

    # convert + reverse
    out = []
    for row in data:
        try:
            t = int(float(row[0]))
            v = float(row[1])
            c = float(row[2])
            h = float(row[3])
            l = float(row[4])
            o = float(row[5])
            out.append((t, o, h, l, c, v))
        except Exception:
            continue

    # oldest first
    out.sort(key=lambda x: x[0])
    return out


# =========================
# Trend alignment (1h+4h+1d)
# =========================
def trend_alignment(symbol):
    for tf in TREND_TFS:
        candles = fetch_gate_candles(symbol, tf, limit=max(CANDLE_LIMIT, EMA_SLOW + 80))
        if len(candles) < max(EMA_SLOW + 10, 80):
            return False
        closes = [x[4] for x in candles]  # c
        if not trend_up(closes):
            return False
    return True


# =========================
# Breakout logic
# =========================
def compute_resistance(candles, lookback):
    # candles: (t,o,h,l,c,v) oldest->newest
    # resistance: geçmiş lookback mumun en yüksek high değeri (son kapanmış mumu hariç tutuyoruz)
    if len(candles) < lookback + 5:
        return None
    highs = [x[2] for x in candles]  # h
    # son kapanmış mum: -2, current forming: -1 -> ikisini de dışarıda bırakmak daha temiz
    window = highs[-(lookback+2):-2]
    if not window:
        return None
    return max(window)

def analyze_symbol_tf(state, symbol, tf, lookback):
    candles = fetch_gate_candles(symbol, tf, limit=max(CANDLE_LIMIT, EMA_SLOW + 80))
    if len(candles) < max(lookback + 10, EMA_SLOW + 20):
        return

    # Son kapanmış mum (-2)
    t, o, h, l, c, v = candles[-2]
    last_closed = (o, h, l, c, v)

    resistance = compute_resistance(candles, lookback)
    if not resistance or resistance <= 0:
        return

    # Yakınlık %:
    near_pct = ((resistance - c) / resistance) * 100.0

    closes = [x[4] for x in candles]
    volumes = [x[5] for x in candles]

    # Trend alignment şartı (1h+4h+1d)
    if not trend_alignment(symbol):
        return

    # Fake filtre + hacim + EMA cross şartları (breakout ve near için de kalite)
    if not fake_breakout_filter(last_closed):
        return
    if not volume_ok(volumes):
        return
    if not crossed_up(closes):
        return

    # Cooldown (near ve breakout ayrı anahtar olsun)
    key_near = f"{symbol}:{tf}:near"
    key_break = f"{symbol}:{tf}:break"

    dt = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Breakout Onay: close >= resistance
    if c >= resistance:
        if cooldown_ok(state, key_break):
            msg = (
                f"🚀 {tf.upper()} Breakout Onay!\n"
                f"Pair: {symbol}\n"
                f"Fiyat (Close): {c}\n"
                f"Direnç: {resistance}\n"
                f"Hacim: ✅ (artış)\n"
                f"EMA: ✅ (kesişim + trend)\n"
                f"Align(1H+4H+1D): ✅\n"
                f"Zaman: {dt}"
            )
            send_message(msg)
            mark_alert(state, key_break)
        return

    # Dirence yakın (NEAR_PCT içinde)
    if 0 <= near_pct <= NEAR_PCT:
        if cooldown_ok(state, key_near):
            msg = (
                f"⚠️ Dirence Yakın ({tf.upper()})\n"
                f"{symbol}\n"
                f"Close: {c}\n"
                f"Direnç: {resistance}\n"
                f"Kalan: {near_pct:.2f}%\n"
                f"Hacim: ✅  EMA: ✅  Align: ✅\n"
                f"Zaman: {dt}"
            )
            send_message(msg)
            mark_alert(state, key_near)


def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("BOT_TOKEN ve CHAT_ID Render env'de set değil.")
        return

    state = load_state()

    send_message("✅ Breakout bot (Gate.io) başladı. 4H & 1D tarama aktif.")
    try:
        pairs = list_usdt_pairs()
    except Exception as e:
        send_message(f"❌ Pair listesi alınamadı: {e}")
        return

    send_message(f"📌 Toplam {len(pairs)} USDT parite bulundu. Her turda MAX_PAIRS={MAX_PAIRS} taranacak.")

    idx = 0
    while True:
        try:
            # döngü içinde MAX_PAIRS kadar tarayıp sonra başa sar
            chunk = pairs[idx: idx + MAX_PAIRS]
            if not chunk:
                idx = 0
                continue

            for symbol in chunk:
                # 4H
                analyze_symbol_tf(state, symbol, "4h", LOOKBACK_4H)
                # 1D
                analyze_symbol_tf(state, symbol, "1d", LOOKBACK_1D)

            idx += MAX_PAIRS
            save_state(state)
            time.sleep(SLEEP_SECONDS)

        except Exception as e:
            # hata olursa düşmesin
            print("Loop error:", e)
            time.sleep(10)


if __name__ == "__main__":
    main()
