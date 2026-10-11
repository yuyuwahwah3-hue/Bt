"""
backtest_web.py — Backtest strategi Support & Resistance + EMA cross ULANG, H1
================================================================================
Level Support/Resistance dari 2 candle berlawanan arah (c1,c2), dikonfirmasi
1 candle kanan bersih (c3) + EMA cross searah di c2-c4. Setelah level
TERSENTUH, MENUNGGU EMA cross searah LAGI. Saat cross itu terjadi (candle
closed) -> cari level S/R BARU di 4 candle sebelum cross, lalu pasang LIMIT
di level baru itu. TEST1 dan TEST2 (engulfing) sudah DIHAPUS.

RINGKASAN STRATEGI
-------------------
1) DETEKSI LEVEL (H1) -- TIDAK BERUBAH:
   Support: candle c1 bearish (close<open) lalu c2 bullish (close>open).
            Level = close[c1].
   Resistance: candle c1 bullish (close>open) lalu c2 bearish (close<open).
            Level = close[c1].
   Valid kalau:
            - KANAN: N_RIGHT candle SETELAH c1,c2 (default 1, c3 saja) --
              WICK-nya tidak boleh menyentuh level SAMA SEKALI.
            - EMA CROSS AWAL: SALAH SATU dari candle c2, c3, atau c4 HARUS
              menjadi PENYEBAB cross EMA_FAST/EMA_SLOW (default 4/10, dari
              close H1) searah level: Support -> GOLDEN CROSS, Resistance ->
              DEATH CROSS. Kalau tidak ada, level GUGUR.

   WARMUP: c1 harus di candle ke-EMA_WARMUP (default 50) ke atas. EMA dihitung
   dari awal data (ewm), jadi cross di candle-candle awal bisa palsu/belum stabil.

2) SENTUHAN LEVEL -- candle PERTAMA setelah c3 yang wick/body-nya MENYENTUH
   ATAU MELEBIHI level (support: low <= level; resistance: high >= level).
   Tersentuh persis di harga level juga VALID. Tidak ada syarat arah candle.
   FILTER DORONGAN KUAT (USE_WICK_FILTER, default aktif): cari wick TERPANJANG
   antara c1 dan c2 (support: wick bawah, resistance: wick atas); ujungnya =
   wick_tip. Mulai candle sentuhan dan candle-candle sesudahnya (sampai candle
   cross), kalau BODY candle (min(open,close) utk support, max utk resistance)
   MELEWATI wick_tip -> level GAGAL. Kalau hanya WICK yang melewati -> boleh.

3) EMA CROSS ULANG (mencari level SEBELUMNYA) -- pencarian dibalik: tiap
   cross EMA4/EMA10 (Support -> GOLDEN CROSS, Resistance -> DEATH CROSS)
   mengecek ke BELAKANG, adakah level searah yang SUDAH terbentuk dan SUDAH
   tersentuh di candle SEBELUM candle cross (bukan candle cross itu sendiri),
   dan belum dipakai. Kalau ada -> cross itu jadi sinyal. Kalau ada beberapa
   level sekaligus, dipakai level TERBARU (c1 paling baru); semua level yg
   menunggu cross itu dianggap terpakai, jadi 1 cross = maks 1 entry (tidak
   ada limit kembar di harga yang sama). Level yang baru terbentuk/tersentuh
   di/sesudah candle cross TIDAK ikut. Cross lama di c2-c4 tidak dihitung.
   Kalau tidak ada level sebelumnya -> cross itu diabaikan.

4) LEVEL BARU -- pada candle cross, cari level S/R BARU di ENTRY_LOOKBACK (4)
   candle SEBELUM candle cross (jendela j-4..j-1). Pola sama dengan level awal:
   c1+c2 berlawanan arah (c1,c2 di dalam jendela), level = close[c1], c3 bersih
   (c3 boleh candle cross itu sendiri), TANPA syarat EMA (cross di j sudah
   syaratnya). Bukan level lama yg tadi tersentuh, belum pernah dipakai, dan
   close candle cross masih di sisi yg benar (Long: close > level; Short:
   close < level). Kalau ada beberapa -> c1 paling baru. Tidak ada -> tidak
   ada sinyal (cross itu terbuang, tidak dicoba ulang).

5) ENTRY -- LIMIT di LEVEL BARU itu, dipasang begitu candle cross CLOSED
   (tanpa lookahead). Baru RESMI AKTIF (armed) begitu harga masuk radius
   APPROACH_PCT (default 2%) dari entry_price, lalu ditunggu sampai TERSENTUH.
   Kalau harga menjauh >2%, disarm (balik waiting, tetap hidup). KADALUARSA:
   EXPIRE_CANDLES candle H1 setelah candle cross closed tanpa fill -> gugur.
   SL = tepat SL_PCT (1%) dari entry (Long: entry x 0.99, Short: entry x 1.01).
   TRAILING STOP: aktif di TRAIL_ACTIVATE_R (2R), SL mengikuti TRAIL_STOP_R (1R)
   di belakang harga ekstrem (dipantau M5). Kalau di candle M5 yg sama harga
   menyentuh SL dan trailing sekaligus aktif/naik -> yg dihitung SL (SL dicek
   duluan). Level MATI setelah 1x FILLED.

Deploy ke Railway:
  Start command -> python backtest_web.py
  Buka domain Railway -> lihat progress & hasil di browser (auto-refresh)

Periode default: 1 tahun ke belakang dari hari script dijalankan (bisa
di-override lewat env BACKTEST_START_DATE / BACKTEST_END_DATE).

trades.csv menyertakan waktu (WIB, UTC+7) level terbentuk, sentuhan level,
cross, entry, dan exit -- kolom 'src_wick_tip' (ujung wick filter), 'src_level_formed_wib' (level lama), 'level_formed_wib' (level baru/entry), 'touch_wib',
'cross_wib', 'entry_wib', 'exit_wib' -- selain versi epoch ms mentahnya.
Kolom 'reason' berisi 'SL' atau 'TRAIL'.
"""

import os, threading, time, io, csv
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import numpy as np
import pandas as pd
from pybit.unified_trading import HTTP

# ============================================================
# CONFIG (override via environment variable kalau perlu)
# ============================================================
PORT             = int(os.environ.get('PORT', 8080))
INITIAL_BALANCE  = float(os.environ.get('INITIAL_BALANCE', '10.0'))   # modal awal, 1 akun bersama
RISK_PCT         = float(os.environ.get('RISK_PCT', '0.05'))          # risk 1% balance/trade (compound)
FEE_ENTRY_PCT    = float(os.environ.get('FEE_ENTRY_PCT', '0.001'))
FEE_EXIT_PCT     = float(os.environ.get('FEE_EXIT_PCT', str(0.001 * 3)))

SL_PCT           = float(os.environ.get('SL_PCT', '0.01'))           # SL tepat 1% dari entry (= 1R)
APPROACH_PCT     = float(os.environ.get('APPROACH_PCT', '0.02'))       # limit baru AKTIF (armed) kalau harga sudah dlm radius 2% dari entry_price
TRAIL_ACTIVATE_R = float(os.environ.get('TRAIL_ACTIVATE_R', '2.0'))    # trailing aktif begitu profit capai 2R
TRAIL_STOP_R     = float(os.environ.get('TRAIL_STOP_R', '1.0'))       # setelah aktif, SL mengikuti 1R di belakang harga tertinggi/terendah

LEVERAGE           = float(os.environ.get('LEVERAGE', '50'))
MARGIN_USAGE_CAP    = float(os.environ.get('MARGIN_USAGE_CAP', '0.90'))

MAX_CONCURRENT_RAW = os.environ.get('MAX_CONCURRENT', '0').strip().lower()
MAX_CONCURRENT = float('inf') if MAX_CONCURRENT_RAW in ('', '0', 'unlimited', 'inf') else int(MAX_CONCURRENT_RAW)

ALLOW_HEDGE      = os.environ.get('ALLOW_HEDGE', 'true').lower() == 'true'

SIMULATE_MIN_ORDER  = os.environ.get('SIMULATE_MIN_ORDER', '1').strip() not in ('0', 'false', 'False', '')
MIN_ORDER_USD       = float(os.environ.get('MIN_ORDER_USD', '5.0'))
ORDER_BUMP_FLOOR     = float(os.environ.get('ORDER_BUMP_FLOOR', '4.0'))
QTY_STEP_APPROX      = float(os.environ.get('QTY_STEP_APPROX', '0.000001'))

def _default_end_date():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')

def _default_start_date():
    d = datetime.now(timezone.utc)
    # mundur 1 tahun kalender dari hari ini
    return (d - timedelta(days=365)).strftime('%Y-%m-%d')

BACKTEST_START_DATE = os.environ.get('BACKTEST_START_DATE', _default_start_date())
BACKTEST_END_DATE   = os.environ.get('BACKTEST_END_DATE', _default_end_date())

CACHE_DIR = os.environ.get('CACHE_DIR', './data_cache')
os.makedirs(CACHE_DIR, exist_ok=True)

# Semua koin yang dipakai bot.
# Hasil backtest 1 tahun (Support & Resistance + EMA4/10 cross) -- hanya
# koin dengan ROI% > 0 yang dipakai. 22 koin sisanya (ROI negatif/breakeven
# di bawah FLOWUSDT -0.1%) tidak diikutkan lagi.
# Hasil backtest 1 tahun (S&R + EMA cross c2-c4 + TEST1/TEST2 engulfing) --
# hanya koin dengan WIN RATE >= 50% yang dipakai (bukan filter ROI kali ini).
# CATATAN: daftar ini dipilih saat strategi masih TEST1/TEST2 -- jalankan ulang
# backtest dgn strategi baru (entry di tengah EMA cross) lalu filter ulang kalau perlu.
SYMBOLS = [
    'PENDLEUSDT', 'PYTHUSDT', 'BLURUSDT', '1000BONKUSDT', 'WUSDT',
    'USUALUSDT', 'ETHFIUSDT', 'LABUSDT', 'IOTAUSDT', '1000FLOKIUSDT',
    'HBARUSDT', 'PLUMEUSDT', 'BERAUSDT', 'MASKUSDT', 'ESPORTSUSDT',
    'IMXUSDT', 'CRVUSDT', 'ACHUSDT', 'FARTCOINUSDT', 'AEVOUSDT',
    'ICPUSDT', 'ENAUSDT', 'ADAUSDT', 'WIFUSDT', 'DYDXUSDT',
    'BATUSDT', 'LRCUSDT', 'IOUSDT', 'BOMEUSDT', 'OPUSDT',
    'POPCATUSDT', 'WOOUSDT', 'STORJUSDT', 'ROSEUSDT', 'POWRUSDT',
    'JUPUSDT', 'HUSDT', 'STRKUSDT', 'IOTXUSDT', 'MEWUSDT',
    'VIRTUALUSDT', 'POLUSDT', 'CKBUSDT', 'ASTRUSDT'
]


def _apply_min_order_size(raw_qty, entry_p):
    if not SIMULATE_MIN_ORDER:
        return raw_qty, False, False
    step = QTY_STEP_APPROX
    qty = round(raw_qty / step) * step
    if qty < step:
        return 0, True, False
    order_value = qty * entry_p
    if order_value < MIN_ORDER_USD:
        if order_value >= ORDER_BUMP_FLOOR:
            qty = round((MIN_ORDER_USD / entry_p) / step) * step
            if qty * entry_p < MIN_ORDER_USD:
                qty += step
            return qty, False, True
        else:
            return 0, True, False
    return qty, False, False


def _date_to_ms(date_str):
    dt = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

_START_MS = _date_to_ms(BACKTEST_START_DATE)
_END_MS   = _date_to_ms(BACKTEST_END_DATE) + 86400 * 1000 - 1

# ============================================================
# GLOBAL STATE
# ============================================================
_lock       = threading.Lock()
_log        = []
_phase      = 'running'
_results    = []
_kind_results = []
_per_coin_results = []   # list dict: {symbol, n_trades, n_win, wr, total_r, final_balance, roi}
_all_trades = []
_monthly_results = []   # list dict: {month, start_balance, end_balance, profit_usd, growth_pct, n_trades, wr}
_avg_monthly_growth = 0.0   # rata-rata growth_pct antar bulan yg ada trade
_combined_result = {
    'n_trades': 0, 'n_win': 0, 'n_loss': 0, 'wr': 0, 'total_pnl': 0, 'roi': 0,
    'total_r': 0, 'avg_r': 0, 'final_balance': INITIAL_BALANCE,
    'blocked_by_slot': 0, 'blocked_by_margin': 0, 'blocked_by_min_order': 0, 'blocked_by_invalid_sl': 0,
    'expired_count': 0,
}


def _ts():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).strftime('%H:%M:%S')

def _fmt_wib(ts_ms):
    """epoch ms (UTC) -> 'YYYY-MM-DD HH:MM WIB' (UTC+7)."""
    if ts_ms is None:
        return ''
    dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc) + timedelta(hours=7)
    return dt.strftime('%Y-%m-%d %H:%M WIB')

def _log_msg(msg: str):
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    with _lock:
        _log.append(line)


# ============================================================
# FETCH DATA DARI BYBIT (dgn CACHE ke disk)
# ============================================================

def _cache_path(symbol: str, tf: str) -> str:
    return os.path.join(CACHE_DIR, f"{symbol}_{tf}_{BACKTEST_START_DATE}_{BACKTEST_END_DATE}.csv")


def _load_cache(symbol: str, tf: str):
    path = _cache_path(symbol, tf)
    if os.path.exists(path):
        try:
            df = pd.read_csv(path)
            if not df.empty and {'ts', 'open', 'high', 'low', 'close', 'vol'}.issubset(df.columns):
                return df
        except Exception as e:
            _log_msg(f"   ⚠ {symbol} {tf}: cache korup ({e}), fetch ulang dari Bybit.")
    return None


def _save_cache(symbol: str, tf: str, df: pd.DataFrame):
    try:
        df.to_csv(_cache_path(symbol, tf), index=False)
    except Exception as e:
        _log_msg(f"   ⚠ {symbol} {tf}: gagal simpan cache — {e}")


def fetch_bybit(symbol: str, interval, tf: str) -> pd.DataFrame:
    """interval: 60 utk H1, 5 utk M5 (parameter get_kline Bybit)."""
    cached = _load_cache(symbol, tf)
    if cached is not None:
        _log_msg(f"   💾 {symbol} {tf}: pakai cache ({len(cached):,} candle) — skip fetch Bybit.")
        return cached

    session = HTTP(testnet=False)
    rows, cur_end, n_call = [], _END_MS, 0
    while True:
        for attempt in range(4):
            try:
                res = session.get_kline(symbol=symbol, category='linear', interval=interval,
                                        limit=1000, start=_START_MS, end=cur_end)
                data = res['result']['list']
                break
            except Exception as e:
                wait = 2 ** attempt
                _log_msg(f"   ⚠ {symbol} {tf} API error (attempt {attempt+1}): {e} — retry {wait}s")
                time.sleep(wait)
        else:
            _log_msg(f"   ❌ {symbol} {tf}: gagal fetch setelah 4 percobaan.")
            break
        if not data:
            break
        for kl in data:
            rows.append({'ts': int(kl[0]), 'open': float(kl[1]), 'high': float(kl[2]),
                         'low': float(kl[3]), 'close': float(kl[4]), 'vol': float(kl[5])})
        n_call += 1
        oldest_ts = int(data[-1][0])
        if oldest_ts <= _START_MS:
            break
        cur_end = oldest_ts - 1
        time.sleep(0.15)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).drop_duplicates(subset='ts').sort_values('ts').reset_index(drop=True)
    _save_cache(symbol, tf, df)
    return df


def fetch_bybit_h1(symbol: str) -> pd.DataFrame:
    return fetch_bybit(symbol, 60, 'H1')


def fetch_bybit_m5(symbol: str) -> pd.DataFrame:
    return fetch_bybit(symbol, 5, 'M5')


# ============================================================
# DETEKSI LEVEL SUPPORT / RESISTANCE (H1)
# ============================================================

EMA_FAST = int(os.environ.get('EMA_FAST', 4))
EMA_SLOW = int(os.environ.get('EMA_SLOW', 10))
EMA_WARMUP = int(os.environ.get('EMA_WARMUP', 50))   # c1 level harus di candle ke-N ke atas (EMA awal data belum stabil, cross di awal bisa palsu)
USE_WICK_FILTER = os.environ.get('USE_WICK_FILTER', '1').lower() not in ('0', 'false', 'no')   # filter 'dorongan kuat': body tidak boleh melewati ujung wick terpanjang c1/c2 setelah level tersentuh
ENTRY_LOOKBACK = int(os.environ.get('ENTRY_LOOKBACK', 4))   # saat EMA cross terjadi, cari level S/R BARU di N candle H1 sebelum candle cross
N_RIGHT = 1   # jumlah candle kanan yang harus bersih (tidak menyentuh wick) -- cukup c3 saja
EXPIRE_CANDLES = 4   # limit kadaluarsa kalau tak tersentuh dlm N candle H1 setelah candle cross closed

def _compute_emas(c):
    """EMA_FAST & EMA_SLOW dari array close (dipakai find_levels & detect_snr_events)."""
    ema_fast = pd.Series(c).ewm(span=EMA_FAST, adjust=False).mean().values
    ema_slow = pd.Series(c).ewm(span=EMA_SLOW, adjust=False).mean().values
    return ema_fast, ema_slow

def find_levels(df):
    """Deteksi level Support & Resistance dari candle H1 (basis body candle).
    TANPA syarat kiri lagi -- cukup c3 (candle tepat setelah c1,c2) yang
    wick-nya tidak boleh menyentuh level.
    Syarat kanan: N_RIGHT candle SETELAH c1,c2 (c3) -- WICK-nya (bukan cuma
                  body) tidak boleh menyentuh level sama sekali.
    TIDAK ADA syarat wick lagi -- c1/c2 boleh sama sekali tidak punya wick
                  (wick selevel dengan ujung body juga tidak masalah).
    Syarat EMA CROSS di RENTANG c2-c4 (EMA_FAST/EMA_SLOW, default 4/10 dari
                  close H1): cukup SALAH SATU dari candle c2, c3, ATAU c4
                  yang menjadi penyebab cross yang searah dengan arah level
                  (tidak harus persis di c2 lagi):
                  - Support -> GOLDEN CROSS di c2/c3/c4 (EMA4 dari <= EMA10
                    di candle sebelumnya, jadi > EMA10 di candle itu).
                  - Resistance -> DEATH CROSS di c2/c3/c4 (EMA4 dari >=
                    EMA10 di candle sebelumnya, jadi < EMA10 di candle itu).
                  Kalau tidak ada satupun cross yang sesuai di c2-c4, level
                  GUGUR.
    'patokan' = LEVEL itu sendiri (ujung body candle c1, close[c1]) --
                  dipakai sebagai acuan sentuhan level (lihat detect_snr_events).
                  Sama persis dengan 'level'.
    'level' = body candle c1 (close[c1]) -- dipakai untuk syarat validitas
                  candle c3 (wick tidak boleh menyentuh level ini).
    'wick_tip' = ujung wick TERPANJANG antara c1 dan c2 (support: low wick bawah
                  terpanjang; resistance: high wick atas terpanjang) -- dipakai filter
                  'dorongan kuat' di detect_snr_events.
    Return list dict: {'type', 'level', 'patokan', 'wick_tip', 'c1', 'c2', 'c_right'}."""
    o = df['open'].values; h = df['high'].values; l = df['low'].values; c = df['close'].values
    n = len(df)
    ema_fast, ema_slow = _compute_emas(c)

    def golden_cross_at(j):   # cross TEPAT di index j (dibanding j-1)
        return ema_fast[j - 1] <= ema_slow[j - 1] and ema_fast[j] > ema_slow[j]

    def death_cross_at(j):
        return ema_fast[j - 1] >= ema_slow[j - 1] and ema_fast[j] < ema_slow[j]

    levels = []
    for i in range(EMA_WARMUP, n - 3):   # skip candle warmup EMA; perlu c1,c2,c3,c4 (i..i+3) semua ada di dalam data
        golden_cross_c2_c4 = any(golden_cross_at(j) for j in (i + 1, i + 2, i + 3))
        death_cross_c2_c4  = any(death_cross_at(j) for j in (i + 1, i + 2, i + 3))
        if c[i] < o[i] and c[i + 1] > o[i + 1]:          # bearish lalu bullish -> support
            S = c[i]
            right_ok = all(l[i + 2 + k] > S + 1e-9 for k in range(N_RIGHT))
            if right_ok and golden_cross_c2_c4:
                # wick BAWAH terpanjang antara c1 dan c2 -> ujungnya (low) = wick_tip
                w1 = min(o[i], c[i]) - l[i]
                w2 = min(o[i + 1], c[i + 1]) - l[i + 1]
                tip = l[i] if w1 >= w2 else l[i + 1]
                levels.append({'type': 'support', 'level': S, 'patokan': S, 'wick_tip': float(tip),
                                'c1': i, 'c2': i + 1, 'c_right': [i + 2 + k for k in range(N_RIGHT)]})
        if c[i] > o[i] and c[i + 1] < o[i + 1]:          # bullish lalu bearish -> resistance
            R = c[i]
            right_ok = all(h[i + 2 + k] < R - 1e-9 for k in range(N_RIGHT))
            if right_ok and death_cross_c2_c4:
                # wick ATAS terpanjang antara c1 dan c2 -> ujungnya (high) = wick_tip
                w1 = h[i] - max(o[i], c[i])
                w2 = h[i + 1] - max(o[i + 1], c[i + 1])
                tip = h[i] if w1 >= w2 else h[i + 1]
                levels.append({'type': 'resistance', 'level': R, 'patokan': R, 'wick_tip': float(tip),
                                'c1': i, 'c2': i + 1, 'c_right': [i + 2 + k for k in range(N_RIGHT)]})
    return levels


# ============================================================
# DETEKSI SBR & RBS (Support/Resistance Break & Retest)
# ============================================================
# SBR = Support jadi Resistance (arah entry: Short)
# RBS = Resistance jadi Support (arah entry: Long)
#
# Untuk tiap level (mulai scan dari candle c3), cari SEKALI kejadian pertama:
#   TEST   -> minimal 1 candle wick menyentuh level tapi close masih aman
#   BREAK  -> candle pertama SETELAH test yang close-nya menembus level
#   KONFIRM-> candle TEPAT SETELAH break, wick-nya TIDAK balik menyentuh level
#
# Catatan urutan: TEST harus terjadi SEBELUM BREAK (candle test dan candle
# break boleh saja candle yang sama SELAMA candle itu masih "test" dulu di
# baca -- tapi karena test butuh close aman & break butuh close tembus, satu
# candle tidak bisa jadi TEST dan BREAK sekaligus, jadi otomatis break selalu
# candle yang berbeda dan setelah test pertama).

def _find_entry_level(o, h, l, c, j, ty, exclude_c1, used):
    """Cari level S/R BARU di ENTRY_LOOKBACK candle SEBELUM candle cross j.
    Jendela = candle j-ENTRY_LOOKBACK .. j-1. c1 dan c2 harus di dalam jendela;
    c3 (candle bersih) boleh = candle cross j itu sendiri (sudah closed).
    Pola sama dengan find_levels, TANPA syarat EMA cross (cross di j-lah syaratnya):
      support    : c1 bearish lalu c2 bullish, level = close[c1], low c3 > level
      resistance : c1 bullish lalu c2 bearish, level = close[c1], high c3 < level
    Tambahan: level BUKAN level lama yg tadi tersentuh (exclude_c1), belum pernah
    dipakai (used), dan harga close candle cross masih di sisi yg benar (Long: close
    > level; Short: close < level) supaya limit tidak berada di sisi pasar yg salah.
    Kalau ada beberapa, dipakai c1 paling baru. Return (c1_index, level) atau None."""
    found = None
    for i in range(j - ENTRY_LOOKBACK, j - 1):
        if i < 0 or i == exclude_c1 or (ty, i) in used:
            continue
        if i + 1 + N_RIGHT > j:
            continue
        if ty == 'support':
            if not (c[i] < o[i] and c[i + 1] > o[i + 1]):
                continue
            S = c[i]
            if all(l[i + 2 + k] > S + 1e-9 for k in range(N_RIGHT)) and c[j] > S:
                found = (i, float(S))
        else:
            if not (c[i] > o[i] and c[i + 1] < o[i + 1]):
                continue
            R = c[i]
            if all(h[i + 2 + k] < R - 1e-9 for k in range(N_RIGHT)) and c[j] < R:
                found = (i, float(R))
    return found


def detect_snr_events(df):
    """Level Support & Resistance -> tersentuh -> EMA cross searah -> cari level
    BARU di ENTRY_LOOKBACK candle sebelum cross. Entry = LIMIT di level baru itu.

    Urutan:
    1) Level terbentuk (find_levels): c1+c2, c3 bersih, EMA cross searah di
       c2-c4, c1 >= EMA_WARMUP. 'patokan' = 'level' (close c1). Ini level LAMA.
    2) SENTUHAN per level lama: candle PERTAMA setelah c3 yang menyentuh/melebihi
       level (support: low <= level; resistance: high >= level). FILTER: kalau body
       candle sentuhan atau candle sesudahnya (s/d candle cross) melewati wick_tip
       (ujung wick terpanjang c1/c2) -> level gagal (hanya wick melewati: boleh).
    3) Tiap EMA cross di candle j (golden -> support, death -> resistance) mengecek
       ke BELAKANG: ada level lama searah yg touch_i < j dan belum dipakai? Ada ->
       level lama dipilih = c1 paling baru, semua level lama yg menunggu cross ini
       ditandai terpakai (1 cross = maks 1 event, cross ini tidak dicoba ulang).
    4) CARI LEVEL BARU di ENTRY_LOOKBACK (4) candle sebelum candle cross
       (_find_entry_level). Tidak ada -> tidak ada sinyal.
    5) ENTRY: LIMIT di level baru itu (close c1 level baru), dipasang begitu
       candle cross CLOSED. Armed dlm radius APPROACH_PCT, expire EXPIRE_CANDLES
       candle H1. SL tepat SL_PCT (1%) dari entry.

    Return list dict:
    {'kind', 'type', 'level' (= level BARU/entry), 'patokan', 'direction',
     'entry_price', 'sl_price', 'ready_ts', 'expire_ts', 'touch_ts', 'cross_ts',
     'confirm_ts', 'c1_ts', 'c1', 'c2', 'src_level', 'src_c1_ts'}
    """
    o = df['open'].values; h = df['high'].values; l = df['low'].values; c = df['close'].values
    ts = df['ts'].values
    n = len(df)
    WICK_EPS = 1e-9
    ema_fast, ema_slow = _compute_emas(c)

    # 1+2) level terbentuk + candle sentuhan pertamanya
    cands = []
    for lv in find_levels(df):
        ty = lv['type']
        patokan = lv['patokan']
        last_right_i = lv['c_right'][-1]   # c3 -- confirm
        touch_i = None
        for k in range(last_right_i + 1, n):
            if ty == 'support':
                touch = l[k] <= patokan + WICK_EPS
            else:
                touch = h[k] >= patokan - WICK_EPS
            if touch:
                touch_i = k
                break
        if touch_i is None:
            continue   # belum pernah tersentuh sampai akhir data

        # FILTER dorongan kuat: dari candle sentuhan dst, kalau BODY (bukan wick)
        # melewati ujung wick terpanjang c1/c2 -> level GAGAL di candle itu.
        # Hanya wick yang melewati ujung -> boleh.
        fail_i = None
        if USE_WICK_FILTER:
            tip = lv['wick_tip']
            for k in range(touch_i, n):
                if ty == 'support':
                    broke = min(o[k], c[k]) < tip - WICK_EPS
                else:
                    broke = max(o[k], c[k]) > tip + WICK_EPS
                if broke:
                    fail_i = k
                    break
        cands.append({'lv': lv, 'ty': ty, 'touch_i': touch_i, 'fail_i': fail_i,
                      'last_right_i': last_right_i, 'used': False})

    # 3) tiap EMA cross mencari level SEBELUMNYA (touch_i < j, belum dipakai)
    events = []
    used_entry = set()   # (type, c1) level baru yg sudah dipakai jadi entry
    for j in range(1, n):
        if ema_fast[j - 1] <= ema_slow[j - 1] and ema_fast[j] > ema_slow[j]:
            ty = 'support'      # golden cross
        elif ema_fast[j - 1] >= ema_slow[j - 1] and ema_fast[j] < ema_slow[j]:
            ty = 'resistance'   # death cross
        else:
            continue
        # level gagal kalau body menembus ujung wick di candle sentuhan..j (j inklusif)
        pool = [cd for cd in cands if cd['ty'] == ty and not cd['used'] and cd['touch_i'] < j
                and (cd['fail_i'] is None or cd['fail_i'] > j)]
        if not pool:
            continue   # tidak ada level sebelumnya -> cross diabaikan
        chosen = max(pool, key=lambda cd: (cd['lv']['c1'], cd['touch_i']))
        for cd in pool:
            cd['used'] = True
        lv = chosen['lv']   # level LAMA (yg tadi tersentuh)

        # 4) cari level BARU di ENTRY_LOOKBACK candle sebelum candle cross
        found = _find_entry_level(o, h, l, c, j, ty, lv['c1'], used_entry)
        if found is None:
            continue   # tidak ada level baru di jendela -> cross ini terbuang
        e_c1, e_level = found
        used_entry.add((ty, e_c1))

        kind = 'SNR_SUPPORT' if ty == 'support' else 'SNR_RESISTANCE'
        direction = 'Long' if ty == 'support' else 'Short'

        # Entry: LIMIT di level baru. SL: tepat SL_PCT dari entry.
        entry_price = e_level
        if direction == 'Long':
            sl_price = entry_price * (1 - SL_PCT)
        else:
            sl_price = entry_price * (1 + SL_PCT)

        # ready_ts = saat candle cross CLOSED (open + 1 jam - 1ms) -> limit baru
        # dipasang setelahnya (tanpa lookahead, EMA dihitung dari close).
        ready_ts = int(ts[j]) + 3600 * 1000 - 1
        expire_ts = ready_ts + EXPIRE_CANDLES * 3600 * 1000
        events.append({
            'kind': kind, 'type': ty, 'level': e_level, 'patokan': e_level,
            'direction': direction,
            'entry_price': entry_price, 'sl_price': sl_price, 'ready_ts': ready_ts,
            'touch_ts': int(ts[chosen['touch_i']]),
            'cross_ts': int(ts[j]),
            'confirm_ts': int(ts[e_c1 + 1 + N_RIGHT]),
            'c1_ts': int(ts[e_c1]),
            'expire_ts': expire_ts,
            'c1': e_c1, 'c2': e_c1 + 1,
            'src_level': lv['level'], 'src_c1_ts': int(ts[lv['c1']]), 'src_wick_tip': float(lv['wick_tip']),
        })

    events.sort(key=lambda e: e['ready_ts'])
    return events


def detect_all_events(df):
    """Support & Resistance + sentuhan + EMA cross + level baru -- SNR_SUPPORT (Long)
    dan SNR_RESISTANCE (Short). Dedup: 2 event dgn (kind, level, ready_ts)
    SAMA PERSIS dianggap 1 sinyal yg sama -- ambil salah satu saja."""
    events = detect_snr_events(df)
    seen = set()
    deduped = []
    for e in events:
        dedup_key = (e['kind'], round(e['level'], 10), e['ready_ts'])
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        deduped.append(e)
    deduped.sort(key=lambda e: e['ready_ts'])
    return deduped





# ============================================================
# STRUKTUR M5 UTK LOOKUP CEPAT
# ============================================================

def prepare_m5(df_m5):
    if df_m5 is None or df_m5.empty:
        return None
    df_m5 = df_m5.sort_values('ts').reset_index(drop=True)
    return {
        'TS': df_m5['ts'].values.astype(np.int64),
        'O': df_m5['open'].values, 'H': df_m5['high'].values,
        'L': df_m5['low'].values, 'C': df_m5['close'].values,
    }


# ============================================================
# PERSIAPAN PER-KOIN
# ============================================================

def prepare_coin(symbol, df):
    df = df.sort_values('ts').reset_index(drop=True)
    events = detect_all_events(df)
    return {
        'symbol': symbol,
        'TS': df['ts'].values.astype(np.int64),
        'events': events,   # list SBR, urut by confirm_ts
        'n': len(df),
    }


# ============================================================
# SIMULASI GABUNGAN (semua koin, 1 balance, 1 pool slot)
# ============================================================
#
# Tiap event dari detect_all_events() SUDAH final (level tersentuh + EMA cross
# ulang sudah terjadi + level baru ditemukan, entry_price = level baru itu) --
# begitu waktu (M5) sampai di ready_ts event itu (candle cross closed), limit order
# "dipasang" (GTC), lalu dipantau tiap candle M5 sampai TERSENTUH baru
# open_trade. Kalau slot/margin/min order tidak cukup PAS saat tersentuh,
# limit TETAP GTC (tidak batal), dicoba lagi kalau tersentuh lagi nanti.

def run_combined_backtest(coins: dict, m5_data: dict) -> dict:
    balance = INITIAL_BALANCE
    active_positions = {}     # key(symbol,direction+level) -> {...}
    trades = []

    def _akey(symbol, direction, level, kind):
        return f"{symbol}|{direction}|{kind}|{level:.10f}"

    total_margin_used = 0.0   # dijaga incremental, bukan sum() ulang tiap panggilan (O(1) bukan O(n))

    def _slots_used():
        return len(active_positions)

    positions_by_symbol = {}   # symbol -> set(keys)

    # pending_activation: event yg ready_ts-nya BELUM lewat, urut asc per simbol.
    # Begitu ready_ts lewat -> limit MASUK STATUS 'waiting' (live_levels_by_symbol),
    # dipantau tiap candle M5: harga masuk radius APPROACH_PCT -> ARMED, lalu ditunggu
    # sampai TERSENTUH (open_trade).
    pending_activation = {}
    live_levels_by_symbol = {symbol: [] for symbol in coins}
    # level_state: (symbol, idx) -> {'status': 'waiting'/'armed', 'entry_price', 'expire_ts'}
    level_state = {}
    for symbol, cp in coins.items():
        pending_activation[symbol] = sorted(
            [(ev['ready_ts'], idx) for idx, ev in enumerate(cp['events'])])

    def open_trade(symbol, ev, entry_price, entry_ts):
        nonlocal balance, total_margin_used
        direction = ev['direction']
        sl = ev['sl_price']
        dist = abs(entry_price - sl)   # = 1R (SL = SL_PCT dari entry)

        risk_amount = balance * RISK_PCT
        raw_qty = risk_amount / dist if dist > 0 else 0
        qty, skipped, bumped = _apply_min_order_size(raw_qty, entry_price)
        if skipped or qty <= 0:
            return None, 'min_order'

        notional = entry_price * qty
        margin_needed = notional / LEVERAGE
        if (total_margin_used + margin_needed) > balance * MARGIN_USAGE_CAP:
            return None, 'margin'

        if _slots_used() >= MAX_CONCURRENT:
            return None, 'slot'

        key = _akey(symbol, direction, ev['level'], ev['kind'])
        active_positions[key] = {
            'symbol': symbol, 'direction': direction, 'entry': entry_price, 'sl': sl,
            'dist': dist, 'qty': qty, 'entry_ts': entry_ts, 'level': ev['level'],
            'kind': ev['kind'], 'margin': margin_needed, 'confirm_ts': ev['confirm_ts'],
            'c1_ts': ev['c1_ts'], 'touch_ts': ev['touch_ts'], 'cross_ts': ev['cross_ts'],
            'src_c1_ts': ev['src_c1_ts'], 'src_level': ev['src_level'], 'src_wick_tip': ev['src_wick_tip'],
            'trail_active': False, 'extreme': entry_price,   # high/low-water mark, mulai dari entry
        }
        total_margin_used += margin_needed
        positions_by_symbol.setdefault(symbol, set()).add(key)
        return key, None

    def close_trade(key, exit_price, reason, exit_ts):
        nonlocal balance, total_margin_used
        pos = active_positions.pop(key)
        positions_by_symbol.get(pos['symbol'], set()).discard(key)
        total_margin_used -= pos['margin']
        entry, dist, qty, direction = pos['entry'], pos['dist'], pos['qty'], pos['direction']
        pnl_gross = (exit_price - entry) * qty if direction == 'Long' else (entry - exit_price) * qty
        fee = entry * qty * FEE_ENTRY_PCT + exit_price * qty * FEE_EXIT_PCT
        pnl_net = pnl_gross - fee
        balance += pnl_net
        r_mult = pnl_net / (dist * qty) if dist * qty > 0 else 0
        trades.append({
            'symbol': pos['symbol'], 'direction': direction, 'entry': entry, 'sl': pos['sl'],
            'exit': exit_price, 'reason': reason, 'r_mult': r_mult, 'pnl_usd': pnl_net,
            'entry_ts': pos['entry_ts'], 'exit_ts': exit_ts, 'balance_after': balance,
            'level': pos['level'], 'kind': pos['kind'],
            'level_formed_ts': pos['c1_ts'],
            'level_formed_wib': _fmt_wib(pos['c1_ts']),
            'src_level': pos['src_level'],
            'src_wick_tip': pos['src_wick_tip'],
            'src_level_formed_ts': pos['src_c1_ts'],
            'src_level_formed_wib': _fmt_wib(pos['src_c1_ts']),
            'touch_ts': pos['touch_ts'],
            'touch_wib': _fmt_wib(pos['touch_ts']),
            'cross_ts': pos['cross_ts'],
            'cross_wib': _fmt_wib(pos['cross_ts']),
            'entry_wib': _fmt_wib(pos['entry_ts']),
            'exit_wib': _fmt_wib(exit_ts),
        })

    def process_symbol_tick(symbol, j):
        """Proses SEMUA hal (exit posisi & level live) untuk 1 simbol di index candle j."""
        m5 = m5_data[symbol]
        now_ts = int(m5['TS'][j])
        hi, lo, close_p = m5['H'][j], m5['L'][j], m5['C'][j]

        # 1) exit posisi aktif simbol ini (trailing stop: aktif di TRAIL_ACTIVATE_R,
        #    SL mengikuti TRAIL_STOP_R di belakang harga ekstrem).
        #    ATURAN SIMULTAN: kalau di candle M5 yg SAMA harga menyentuh SL
        #    (level SL SEBELUM candle ini) dan sekaligus trailing akan aktif /
        #    naik, maka yg dihitung = SL (SL dicek DULUAN, sebelum trailing
        #    di-update memakai high/low candle yg sama).
        keys = positions_by_symbol.get(symbol)
        if keys:
            for key in list(keys):
                pos = active_positions[key]
                direction = pos['direction']
                entry, dist = pos['entry'], pos['dist']
                reason_stop = 'TRAIL' if pos['trail_active'] else 'SL'

                if direction == 'Long':
                    # 1a) SL (level sebelum candle ini) tersentuh -> keluar di SL, trailing tidak diproses
                    if lo <= pos['sl'] + 1e-12:
                        close_trade(key, pos['sl'], reason_stop, now_ts)
                        continue
                    # 1b) tidak kena SL -> update high-water mark & trailing pakai HIGH candle
                    if hi > pos['extreme']:
                        pos['extreme'] = hi
                    profit_r = (pos['extreme'] - entry) / dist
                    if not pos['trail_active'] and profit_r >= TRAIL_ACTIVATE_R:
                        pos['trail_active'] = True
                    if pos['trail_active']:
                        new_sl = pos['extreme'] - TRAIL_STOP_R * dist
                        if new_sl > pos['sl']:
                            pos['sl'] = new_sl   # SL cuma boleh naik (menguntungkan), tak pernah mundur
                            if lo <= pos['sl'] + 1e-12:   # candle yg sama balik menyentuh SL trailing baru
                                close_trade(key, pos['sl'], 'TRAIL', now_ts)
                else:  # Short
                    if hi >= pos['sl'] - 1e-12:
                        close_trade(key, pos['sl'], reason_stop, now_ts)
                        continue
                    if lo < pos['extreme']:
                        pos['extreme'] = lo
                    profit_r = (entry - pos['extreme']) / dist
                    if not pos['trail_active'] and profit_r >= TRAIL_ACTIVATE_R:
                        pos['trail_active'] = True
                    if pos['trail_active']:
                        new_sl = pos['extreme'] + TRAIL_STOP_R * dist
                        if new_sl < pos['sl']:
                            pos['sl'] = new_sl
                            if hi >= pos['sl'] - 1e-12:
                                close_trade(key, pos['sl'], 'TRAIL', now_ts)

        # 2) limit live simbol ini: waiting (belum dlm radius 2%) -> armed
        #    (sudah dlm radius 2%, limit resmi terpasang) -> tersentuh (fill).
        #    Entry = level S/R BARU (dicari sebelum cross). KADALUARSA: now_ts >=
        #    expire_ts dan belum tersentuh -> setup gugur, dibuang.
        cp = coins[symbol]
        live_idxs = live_levels_by_symbol.get(symbol)
        if live_idxs:
            still_live = []
            for idx in live_idxs:
                ev = cp['events'][idx]
                st = level_state[(symbol, idx)]

                expire_ts = st['expire_ts']
                if expire_ts is not None and now_ts >= expire_ts:
                    nonlocal_blocks['expired'] += 1
                    continue   # kadaluarsa -> dibuang, tidak pernah dicoba lagi
                entry_price = st['entry_price']
                dist_pct = abs(close_p - entry_price) / entry_price
                touched = (lo <= entry_price <= hi)
                if st['status'] == 'waiting':
                    if dist_pct <= APPROACH_PCT:
                        st['status'] = 'armed'
                    still_live.append(idx)
                else:   # armed
                    if touched:
                        opened_key, block_reason = open_trade(symbol, ev, entry_price, now_ts)
                        if opened_key is None:
                            nonlocal_blocks[block_reason] += 1
                            # gagal dibuka (slot/margin/min_order) -> limit tetap
                            # GTC (armed), coba lagi kalau tersentuh lagi nanti
                            still_live.append(idx)
                    else:
                        if dist_pct > APPROACH_PCT:
                            st['status'] = 'waiting'   # menjauh lagi -> disarm
                        still_live.append(idx)
            live_levels_by_symbol[symbol] = still_live

    nonlocal_blocks = {'slot': 0, 'margin': 0, 'min_order': 0, 'invalid_sl': 0, 'expired': 0}

    # ── TIMELINE EFISIEN via K-WAY MERGE (pointer index, bukan searchsorted) ──
    # Tiap simbol punya pointer int ke posisi candle M5 berikutnya yg BELUM
    # diproses. Heap cuma menyimpan (ts_candle_berikutnya, symbol) utk tahu
    # simbol mana yg harus diproses duluan (urutan kronologis lintas simbol,
    # perlu utk shared balance/margin/slot). Advance pointer = O(1) (i+=1),
    # bukan np.searchsorted (O(log n) tapi overhead call besar tiap tick).
    import heapq

    ptr = {symbol: 0 for symbol in coins}   # pointer index candle M5 berikutnya per simbol
    m5_ts_arr = {symbol: m5['TS'] for symbol, m5 in m5_data.items() if m5 is not None}
    m5_len = {symbol: len(arr) for symbol, arr in m5_ts_arr.items()}

    # simbol mulai diproses dari pointer candle M5 pertama SETELAH confirm_ts level pertamanya
    def _advance_ptr_to(symbol, target_ts):
        """Majukan ptr[symbol] sampai candle M5 pertama dgn ts >= target_ts (linear, tapi
        dipanggil jarang -- hanya saat lompat jauh, bukan tiap tick normal)."""
        arr = m5_ts_arr.get(symbol)
        if arr is None:
            return
        i = ptr[symbol]
        n = m5_len[symbol]
        while i < n and arr[i] < target_ts:
            i += 1
        ptr[symbol] = i

    heap = []   # (ts, symbol) -- symbol siap diproses di candle ptr[symbol]

    for symbol, cp in coins.items():
        if not cp['events'] or symbol not in m5_ts_arr:
            continue
        first_ready_ts = cp['events'][0]['ready_ts']
        _advance_ptr_to(symbol, first_ready_ts + 1)
        if ptr[symbol] < m5_len[symbol]:
            heapq.heappush(heap, (int(m5_ts_arr[symbol][ptr[symbol]]), symbol))

    while heap:
        now_ts, symbol = heapq.heappop(heap)
        i = ptr[symbol]
        if i >= m5_len[symbol] or m5_ts_arr[symbol][i] != now_ts:
            continue   # stale entry (seharusnya tidak terjadi, safety check)

        # begitu waktu (M5) sudah lewat ready_ts sebuah event (candle cross closed) -> limit mulai dipantau
        # (status 'waiting' -- belum dlm radius APPROACH_PCT dari entry_price)
        plist = pending_activation.get(symbol)
        if plist:
            while plist and plist[0][0] < now_ts:
                _, idx = plist.pop(0)
                ev = coins[symbol]['events'][idx]
                level_state[(symbol, idx)] = {
                    'status': 'waiting', 'entry_price': ev['entry_price'],
                    'expire_ts': ev['expire_ts'],
                }
                live_levels_by_symbol[symbol].append(idx)

        process_symbol_tick(symbol, i)

        # advance pointer: kalau simbol masih punya posisi aktif ATAU limit live
        # yg belum tersentuh (butuh dipantau tiap candle), lanjut candle
        # BERIKUTNYA (i+1, O(1)). Kalau tidak ada apa-apa, lompat jauh ke
        # ready_ts event pending berikutnya (hemat banyak tick).
        has_active = bool(positions_by_symbol.get(symbol)) or bool(live_levels_by_symbol.get(symbol))
        if has_active:
            ptr[symbol] = i + 1
        else:
            plist = pending_activation.get(symbol)
            if plist:
                _advance_ptr_to(symbol, plist[0][0] + 1)
            else:
                ptr[symbol] = m5_len[symbol]   # tidak ada event pending lagi -> selesai

        if ptr[symbol] < m5_len[symbol]:
            heapq.heappush(heap, (int(m5_ts_arr[symbol][ptr[symbol]]), symbol))

    blocked_by_slot = nonlocal_blocks['slot']
    blocked_by_margin = nonlocal_blocks['margin']
    blocked_by_min_order = nonlocal_blocks['min_order']
    blocked_by_invalid_sl = nonlocal_blocks['invalid_sl']
    expired_count = nonlocal_blocks['expired']

    n_trades = len(trades)
    n_win = sum(1 for t in trades if t['pnl_usd'] > 0)
    n_loss = n_trades - n_win
    wr = (n_win / n_trades * 100) if n_trades else 0
    total_pnl = sum(t['pnl_usd'] for t in trades)
    total_r = sum(t['r_mult'] for t in trades)
    avg_r = total_r / n_trades if n_trades else 0
    roi = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100

    return {
        'trades': trades, 'n_trades': n_trades, 'n_win': n_win, 'n_loss': n_loss,
        'wr': wr, 'total_pnl': total_pnl, 'total_r': total_r, 'avg_r': avg_r,
        'final_balance': balance, 'roi': roi,
        'blocked_by_slot': blocked_by_slot, 'blocked_by_margin': blocked_by_margin,
        'blocked_by_min_order': blocked_by_min_order, 'blocked_by_invalid_sl': blocked_by_invalid_sl,
        'expired_count': expired_count,
    }


# ============================================================
# SIMULASI PER-KOIN (independen, bukan gabungan)
# ============================================================
# Selain simulasi GABUNGAN (1 balance dipakai bersama semua koin -- lebih
# realistis krn simulasikan 1 akun beneran), kita juga jalankan simulasi
# PER-KOIN: tiap koin dapat modal awal sendiri (INITIAL_BALANCE), balance &
# slot terpisah dari koin lain. Ini menjawab pertanyaan "kalau CUMA trading
# koin ini sendirian, berapa trade & WR-nya" -- tanpa pengaruh rebutan
# margin/slot dari koin lain.

def run_per_coin_backtests(coins: dict, m5_data: dict) -> dict:
    """Return dict: symbol -> hasil run_combined_backtest (dgn 1 koin saja)."""
    results = {}
    for symbol, cp in coins.items():
        m5 = m5_data.get(symbol)
        if m5 is None:
            continue
        results[symbol] = run_combined_backtest({symbol: cp}, {symbol: m5})
    return results


# ============================================================
# BREAKDOWN PER SIMBOL
# ============================================================

def per_symbol_breakdown(trades):
    by_symbol = {}
    for t in trades:
        s = t['symbol']
        d = by_symbol.setdefault(s, {'n': 0, 'win': 0, 'total_r': 0.0, 'total_pnl': 0.0})
        d['n'] += 1
        if t['pnl_usd'] > 0:
            d['win'] += 1
        d['total_r'] += t['r_mult']
        d['total_pnl'] += t['pnl_usd']
    rows = []
    for s, d in by_symbol.items():
        wr = d['win'] / d['n'] * 100 if d['n'] else 0
        rows.append({'symbol': s, 'n': d['n'], 'win': d['win'], 'wr': wr,
                     'total_r': d['total_r'], 'total_pnl': d['total_pnl']})
    rows.sort(key=lambda r: -r['total_r'])
    return rows


def per_kind_breakdown(trades):
    """Breakdown performa per JENIS level: SNR_SUPPORT / SNR_RESISTANCE."""
    by_kind = {}
    for t in trades:
        k = t.get('kind', '?')
        d = by_kind.setdefault(k, {'n': 0, 'win': 0, 'total_r': 0.0, 'total_pnl': 0.0})
        d['n'] += 1
        if t['pnl_usd'] > 0:
            d['win'] += 1
        d['total_r'] += t['r_mult']
        d['total_pnl'] += t['pnl_usd']
    rows = []
    for k, d in by_kind.items():
        wr = d['win'] / d['n'] * 100 if d['n'] else 0
        rows.append({'kind': k, 'n': d['n'], 'win': d['win'], 'wr': wr,
                     'total_r': d['total_r'], 'total_pnl': d['total_pnl']})
    order = {'SNR_SUPPORT': 0, 'SNR_RESISTANCE': 1}
    rows.sort(key=lambda r: order.get(r['kind'], 99))
    return rows


def monthly_breakdown(trades, initial_balance):
    """Breakdown balance & pertumbuhan PER BULAN (basis waktu WIB, sesuai
    exit_ts tiap trade -- trade dihitung masuk bulan closed-nya, bukan
    bulan open-nya). Tiap baris: balance awal bulan (= balance akhir bulan
    sebelumnya, atau initial_balance utk bulan pertama), balance akhir
    bulan, profit $ bulan itu, pertumbuhan % bulan itu (relatif thd balance
    AWAL bulan itu -- ini yg dibandingkan dgn "profit 10%/bulan" ala
    investasi), jumlah trade & WR bulan itu. Bulan tanpa trade SAMA SEKALI
    tidak dimasukkan (balance tidak berubah, tidak relevan ditampilkan).
    Di akhir, hitung rata-rata pertumbuhan %/bulan (mean sederhana antar
    bulan yang ADA trade -- bukan CAGR, supaya konsisten dgn cara umum
    orang menyebut "rata-rata X%/bulan" meski fluktuatif)."""
    if not trades:
        return [], 0.0
    trades_sorted = sorted(trades, key=lambda t: t['exit_ts'])
    by_month = {}   # 'YYYY-MM' -> list of trades
    for t in trades_sorted:
        dt = datetime.fromtimestamp(int(t['exit_ts']) / 1000, tz=timezone.utc) + timedelta(hours=7)
        key = dt.strftime('%Y-%m')
        by_month.setdefault(key, []).append(t)

    rows = []
    running_balance = initial_balance
    for month_key in sorted(by_month.keys()):
        month_trades = by_month[month_key]
        start_balance = running_balance
        end_balance = month_trades[-1]['balance_after']
        profit_usd = end_balance - start_balance
        growth_pct = (profit_usd / start_balance * 100) if start_balance > 0 else 0.0
        n = len(month_trades)
        win = sum(1 for t in month_trades if t['pnl_usd'] > 0)
        wr = win / n * 100 if n else 0
        rows.append({
            'month': month_key, 'start_balance': start_balance, 'end_balance': end_balance,
            'profit_usd': profit_usd, 'growth_pct': growth_pct, 'n_trades': n, 'wr': wr,
        })
        running_balance = end_balance

    avg_growth_pct = sum(r['growth_pct'] for r in rows) / len(rows) if rows else 0.0
    return rows, avg_growth_pct


# ============================================================
# BACKGROUND WORKER
# ============================================================

def _run():
    global _phase, _results, _kind_results, _per_coin_results, _all_trades, _combined_result
    global _monthly_results, _avg_monthly_growth
    try:
        _log_msg(f"🚀 Mulai backtest SNR (Support & Resistance + EMA{EMA_FAST}/{EMA_SLOW} cross) — {len(SYMBOLS)} koin, {BACKTEST_START_DATE} s/d {BACKTEST_END_DATE}")
        _log_msg(f"   Syarat: level c1-c2 + cross awal di c2/c3/c4 -> tunggu level TERSENTUH -> tunggu EMA{EMA_FAST}/{EMA_SLOW} cross searah LAGI  "
                  f"filter wick={'ON' if USE_WICK_FILTER else 'OFF'}  -> cari level S/R BARU di {ENTRY_LOOKBACK} candle sebelum cross  Entry=LIMIT di level baru itu (armed dlm radius {APPROACH_PCT*100:.1f}%)  "
                  f"SL={SL_PCT*100:.2f}% dari entry  "
                  f"Trailing: aktif di {TRAIL_ACTIVATE_R:.1f}R, jarak {TRAIL_STOP_R:.1f}R dari extreme (SL & trail bersamaan -> SL)")

        coins = {}
        m5_data = {}
        for symbol in SYMBOLS:
            _log_msg(f"📊 {symbol}: fetch H1...")
            df_h1 = fetch_bybit_h1(symbol)
            if df_h1.empty:
                _log_msg(f"   ⚠ {symbol}: data H1 kosong, skip.")
                continue
            _log_msg(f"📊 {symbol}: fetch M5...")
            df_m5 = fetch_bybit_m5(symbol)
            cp = prepare_coin(symbol, df_h1)
            coins[symbol] = cp
            m5_data[symbol] = prepare_m5(df_m5)
            n_by_kind = {}
            for ev in cp['events']:
                n_by_kind[ev['kind']] = n_by_kind.get(ev['kind'], 0) + 1
            kind_str = ', '.join(f"{k}:{v}" for k, v in sorted(n_by_kind.items()))
            _log_msg(f"   ✅ {symbol}: {cp['n']} candle H1, {len(cp['events'])} level terdeteksi ({kind_str}).")

        _log_msg(f"🧮 Menjalankan simulasi gabungan ({len(coins)} koin)...")
        result = run_combined_backtest(coins, m5_data)

        _log_msg(f"🧮 Menjalankan simulasi PER-KOIN (independen, {len(coins)} koin)...")
        per_coin_raw = run_per_coin_backtests(coins, m5_data)
        per_coin_rows = []
        for symbol, r in per_coin_raw.items():
            per_coin_rows.append({
                'symbol': symbol, 'n_trades': r['n_trades'], 'n_win': r['n_win'],
                'wr': r['wr'], 'total_r': r['total_r'], 'final_balance': r['final_balance'],
                'roi': r['roi'],
            })
        per_coin_rows.sort(key=lambda r: -r['total_r'])

        with _lock:
            _all_trades[:] = result['trades']
            _combined_result.update({
                'n_trades': result['n_trades'], 'n_win': result['n_win'], 'n_loss': result['n_loss'],
                'wr': result['wr'], 'total_pnl': result['total_pnl'], 'roi': result['roi'],
                'total_r': result['total_r'], 'avg_r': result['avg_r'],
                'final_balance': result['final_balance'],
                'blocked_by_slot': result['blocked_by_slot'],
                'blocked_by_margin': result['blocked_by_margin'],
                'blocked_by_min_order': result['blocked_by_min_order'],
                'blocked_by_invalid_sl': result['blocked_by_invalid_sl'],
                'expired_count': result['expired_count'],
            })
            _results[:] = per_symbol_breakdown(result['trades'])
            _kind_results[:] = per_kind_breakdown(result['trades'])
            _per_coin_results[:] = per_coin_rows
            monthly_rows, avg_growth = monthly_breakdown(result['trades'], INITIAL_BALANCE)
            _monthly_results[:] = monthly_rows
            _avg_monthly_growth = avg_growth
            _phase = 'done'

        _log_msg(f"✅ SELESAI. {result['n_trades']} trade, WR {result['wr']:.1f}%, "
                  f"Total R {result['total_r']:.2f}, Balance akhir ${result['final_balance']:.2f} "
                  f"(ROI {result['roi']:+.1f}%)")
    except Exception as e:
        import traceback
        _log_msg(f"❌ ERROR: {e}")
        _log_msg(traceback.format_exc())
        with _lock:
            _phase = 'error'


# ============================================================
# DASHBOARD HTML
# ============================================================

def _fmt_max_concurrent():
    return 'Tanpa batas' if MAX_CONCURRENT == float('inf') else str(MAX_CONCURRENT)


def _render_html() -> bytes:
    with _lock:
        phase = _phase
        cr = dict(_combined_result)
        results_cp = list(_results)
        kind_cp = list(_kind_results)
        per_coin_cp = list(_per_coin_results)
        monthly_cp = list(_monthly_results)
        avg_monthly_growth_cp = _avg_monthly_growth
        log_cp = list(_log[-300:])

    log_html = '\n'.join(l for l in log_cp)

    if phase == 'running':
        status_html = '<div class="status running">⏳ Sedang berjalan...</div>'
    elif phase == 'error':
        status_html = '<div class="status error">❌ Terjadi error — lihat log di bawah.</div>'
    else:
        status_html = '<div class="status done">✅ Selesai</div>'

    rows_html = ''
    for r in results_cp:
        cls = 'pos' if r['total_r'] >= 0 else 'neg'
        rows_html += f'''<tr>
            <td>{r['symbol']}</td><td>{r['n']}</td><td>{r['win']}</td>
            <td>{r['wr']:.1f}%</td><td class="{cls}">{r['total_r']:+.2f}</td>
            <td class="{cls}">${r['total_pnl']:+.2f}</td></tr>'''

    kind_rows_html = ''
    for r in kind_cp:
        cls = 'pos' if r['total_r'] >= 0 else 'neg'
        kind_rows_html += f'''<tr>
            <td>{r['kind']}</td><td>{r['n']}</td><td>{r['win']}</td>
            <td>{r['wr']:.1f}%</td><td class="{cls}">{r['total_r']:+.2f}</td>
            <td class="{cls}">${r['total_pnl']:+.2f}</td></tr>'''

    per_coin_rows_html = ''
    for r in per_coin_cp:
        cls = 'pos' if r['total_r'] >= 0 else 'neg'
        per_coin_rows_html += f'''<tr>
            <td>{r['symbol']}</td><td>{r['n_trades']}</td><td>{r['n_win']}</td>
            <td>{r['wr']:.1f}%</td><td class="{cls}">{r['total_r']:+.2f}</td>
            <td>${r['final_balance']:.2f}</td>
            <td class="{cls}">{r['roi']:+.1f}%</td></tr>'''

    monthly_rows_html = ''
    for r in monthly_cp:
        cls = 'pos' if r['profit_usd'] >= 0 else 'neg'
        monthly_rows_html += f'''<tr>
            <td>{r['month']}</td><td>{r['n_trades']}</td><td>{r['wr']:.1f}%</td>
            <td>${r['start_balance']:.2f}</td><td>${r['end_balance']:.2f}</td>
            <td class="{cls}">${r['profit_usd']:+.2f}</td>
            <td class="{cls}">{r['growth_pct']:+.1f}%</td></tr>'''
    avg_growth_cls = 'pos' if avg_monthly_growth_cp >= 0 else 'neg'

    return f'''<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>Backtest SNR</title>
<style>
  body {{ font-family: -apple-system, Arial, sans-serif; background:#0f1117; color:#e6e6e6; margin:0; padding:20px; }}
  h1 {{ font-size:20px; }}
  h2 {{ font-size:16px; margin-top:28px; }}
  .status {{ padding:10px 14px; border-radius:8px; margin-bottom:16px; font-weight:600; }}
  .status.running {{ background:#3a2f00; color:#ffd866; }}
  .status.done {{ background:#0f3a1e; color:#7ee787; }}
  .status.error {{ background:#3a0f0f; color:#ff7b72; }}
  .cards {{ display:flex; flex-wrap:wrap; gap:12px; margin-bottom:20px; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:10px; padding:14px 18px; min-width:140px; }}
  .card .label {{ font-size:12px; color:#8b949e; }}
  .card .value {{ font-size:22px; font-weight:700; margin-top:4px; }}
  table {{ border-collapse: collapse; width:100%; font-size:13px; margin-bottom: 10px; }}
  th, td {{ border:1px solid #30363d; padding:6px 10px; text-align:right; }}
  th {{ background:#161b22; color:#8b949e; }}
  td:first-child, th:first-child {{ text-align:left; }}
  .pos {{ color:#7ee787; }}
  .neg {{ color:#ff7b72; }}
  .note {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:12px 16px; font-size:13px; color:#c9d1d9; margin-top:16px; line-height:1.6; }}
  .log {{ background:#0d1117; border:1px solid #30363d; border-radius:8px; padding:12px; font-size:12px; font-family:monospace; max-height:400px; overflow-y:auto; white-space:pre-wrap; }}
  a {{ color:#58a6ff; }}
</style>
</head>
<body>
  <h1>📊 Backtest SNR (Support & Resistance Murni)</h1>
  {status_html}

  <div class="cards">
    <div class="card"><div class="label">Total Trade</div><div class="value">{cr['n_trades']}</div></div>
    <div class="card"><div class="label">Win Rate</div><div class="value">{cr['wr']:.1f}%</div></div>
    <div class="card"><div class="label">Total R</div><div class="value {'pos' if cr['total_r']>=0 else 'neg'}">{cr['total_r']:+.2f}</div></div>
    <div class="card"><div class="label">Avg R/Trade</div><div class="value {'pos' if cr['avg_r']>=0 else 'neg'}">{cr['avg_r']:+.2f}</div></div>
    <div class="card"><div class="label">Balance Akhir</div><div class="value">${cr['final_balance']:.2f}</div></div>
    <div class="card"><div class="label">ROI</div><div class="value {'pos' if cr['roi']>=0 else 'neg'}">{cr['roi']:+.1f}%</div></div>
  </div>

  <div class="note">
    💡 <b>Support & Resistance + sentuhan + EMA cross ulang</b>, basis body candle H1:
    <br>• Level terbentuk dari c1+c2 (2 candle berlawanan arah), c3 bersih (wick tidak boleh
    menyentuh level), dan salah satu dari c2/c3/c4 wajib jadi PENYEBAB cross
    EMA{EMA_FAST}/EMA{EMA_SLOW} searah (Support → GOLDEN CROSS, Resistance → DEATH CROSS).
    <br>• <b>Sentuhan</b>: candle PERTAMA setelah c3 yang wick/body-nya menyentuh ATAU melebihi
    level (persis di level juga valid). Tidak ada syarat arah candle.
    <br>• <b>Filter dorongan kuat</b>: ujung wick TERPANJANG antara c1 dan c2 (wick bawah utk Support,
    wick atas utk Resistance) jadi batas. Mulai candle sentuhan sampai candle cross, kalau <b>body</b>
    candle melewati ujung wick itu → level gagal. Kalau hanya <b>wick</b> yang melewati → boleh.
    <br>• <b>Cross ulang</b>: tiap EMA{EMA_FAST}/EMA{EMA_SLOW} cross searah (golden untuk Support, death untuk
    Resistance) mengecek ke BELAKANG: ada level searah yang sudah terbentuk &amp; sudah tersentuh di candle
    sebelum cross, dan belum dipakai? Ada → sinyal (dipakai level terbaru, 1 cross = maks 1 entry).
    <br>• <b>Level baru</b>: pada candle cross, cari support (golden) / resistance (death) BARU di
    <b>{ENTRY_LOOKBACK}</b> candle H1 sebelum candle cross (pola c1+c2, c3 bersih, tanpa syarat EMA;
    bukan level lama yang tadi tersentuh). Tidak ada → sinyal tidak ada.
    <br>• <b>ENTRY</b>: LIMIT di <b>level baru</b> itu, dipasang begitu candle cross closed.
    Limit baru RESMI ARMED begitu harga M5 masuk radius
    <b>{APPROACH_PCT*100:.1f}%</b> dari entry_price, lalu ditunggu sampai TERSENTUH (fill).
    Kalau menjauh lagi &gt;{APPROACH_PCT*100:.1f}% sebelum tersentuh, limit disarm (balik
    menunggu, tetap hidup).
    <br>• <b>KADALUARSA</b>: kalau dalam <b>{EXPIRE_CANDLES}</b> candle H1 setelah candle cross closed
    limit tidak PERNAH tersentuh → setup GUGUR, dibuang permanen. Tiap level HANYA dipakai 1x.
    <br>• <b>SL</b> tepat <b>{SL_PCT*100:.2f}%</b> dari entry (=1R).
    <b>Trailing stop</b>: aktif begitu profit capai <b>{TRAIL_ACTIVATE_R:.1f}R</b>, lalu SL mengikuti
    <b>{TRAIL_STOP_R:.1f}R</b> di belakang harga tertinggi/terendah yang pernah dicapai (dipantau M5).
    Kalau SL dan trailing tersentuh di candle M5 yang sama, yang dihitung <b>SL</b>. Level MATI
    setelah 1x terisi (menang/kalah).
    <br>⚙️ Risk {RISK_PCT*100:.0f}% dari balance (compounding). Slot maksimum: {_fmt_max_concurrent()}.
    Sinyal terblokir — slot: {cr.get('blocked_by_slot',0)}, margin: {cr.get('blocked_by_margin',0)},
    min order: {cr.get('blocked_by_min_order',0)}. Kadaluarsa (limit tak tersentuh
    {EXPIRE_CANDLES} candle): {cr.get('expired_count',0)}.
    <br>Unduh semua trade: <a href="/trades.csv">/trades.csv</a> &nbsp;|&nbsp;
    Log mentah: <a href="/logs">/logs</a>
  </div>

  <h2>Pertumbuhan Balance per Bulan (simulasi gabungan, 1 balance bersama)</h2>
  <div class="note">
    Rata-rata pertumbuhan: <b class="{avg_growth_cls}">{avg_monthly_growth_cp:+.1f}% / bulan</b>
    (mean sederhana antar bulan yang ada trade, bukan CAGR -- fluktuatif per bulan, lihat tabel di bawah).
  </div>
  <table>
    <tr><th>Bulan</th><th>N Trade</th><th>WR%</th><th>Balance Awal</th><th>Balance Akhir</th>
        <th>Profit</th><th>Pertumbuhan%</th></tr>
    {monthly_rows_html}
  </table>

  <h2>Ringkasan per Jenis Level (simulasi gabungan)</h2>
  <table>
    <tr><th>Jenis</th><th>N Trade</th><th>Win</th><th>WR%</th><th>Total R</th><th>Total PnL</th></tr>
    {kind_rows_html}
  </table>

  <h2>Kontribusi per Koin (simulasi gabungan, 1 balance bersama)</h2>
  <table>
    <tr><th>Symbol</th><th>N Trade</th><th>Win</th><th>WR%</th><th>Total R</th><th>Total PnL</th></tr>
    {rows_html}
  </table>

  <h2>Simulasi Per-Koin (independen, modal awal sendiri-sendiri)</h2>
  <table>
    <tr><th>Symbol</th><th>N Trade</th><th>Win</th><th>WR%</th><th>Total R</th><th>Balance Akhir</th><th>ROI</th></tr>
    {per_coin_rows_html}
  </table>

  <h2>Log Progress</h2>
  <div class="log" id="log">{log_html}</div>
  <script>var e=document.getElementById('log');if(e)e.scrollTop=e.scrollHeight;</script>
</body>
</html>'''.encode('utf-8')


def _trades_csv() -> bytes:
    with _lock:
        trades_cp = list(_all_trades)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[
        'symbol', 'kind', 'direction', 'level', 'src_level', 'src_wick_tip',
        'src_level_formed_wib', 'level_formed_wib', 'touch_wib', 'cross_wib', 'entry_wib', 'exit_wib',
        'entry', 'sl', 'exit', 'reason', 'r_mult',
        'pnl_usd', 'balance_after',
        'src_level_formed_ts', 'level_formed_ts', 'touch_ts', 'cross_ts', 'entry_ts', 'exit_ts'],
        extrasaction='ignore')
    writer.writeheader()
    for t in trades_cp:
        writer.writerow(t)
    return buf.getvalue().encode('utf-8')


# ============================================================
# HTTP HANDLER
# ============================================================

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/trades.csv':
            body = _trades_csv()
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="trades.csv"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == '/logs':
            with _lock:
                body = '\n'.join(_log).encode('utf-8')
            ctype = 'text/plain; charset=utf-8'
        else:
            body = _render_html()
            ctype = 'text/html; charset=utf-8'

        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == '__main__':
    threading.Thread(target=_run, daemon=True).start()
    server = HTTPServer(('0.0.0.0', PORT), _Handler)
    print(f"🌐 Server running on port {PORT}", flush=True)
    server.serve_forever()
