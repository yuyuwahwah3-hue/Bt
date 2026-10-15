"""
backtest_web.py — Backtest strategi Support & Resistance + EMA4/10 cross + TEST1/TEST2 engulfing
================================================================================================
Strategi disamakan PERSIS dengan bot_ema_flip.py (H1 untuk sinyal, M5 untuk simulasi
fill/SL/trailing supaya urutan kejadian dalam candle H1 lebih akurat).

RINGKASAN STRATEGI
------------------
1) DETEKSI LEVEL (H1, basis body candle):
   - Support   : c1 bearish (close<open) lalu c2 bullish (close>open).
     Resistance: c1 bullish lalu c2 bearish. Level = close[c1].
   - KANAN: 1 candle setelah c2 (c3) -- wick-nya tidak boleh menyentuh level sama
     sekali (Support: low c3 > level; Resistance: high c3 < level).
   - KIRI: NONAKTIF (N_LEFT=0). Bisa dinyalakan lewat env N_LEFT=1 (candle 1 sebelum
     c1 tidak boleh melebihi level).
     TANPA syarat wick c1/c2.
   - SYARAT EMA CROSS: salah satu dari candle c2, c3, atau c4 WAJIB jadi penyebab
     cross EMA4/EMA10 (dari close H1) yang searah -- Support -> GOLDEN CROSS,
     Resistance -> DEATH CROSS. Kalau tidak ada satupun di c2-c4, level gugur.

2) TEST1 + TEST2 (engulfing), setelah level terbentuk:
   - TEST1: candle pertama SETELAH c3 yang wick/body-nya menyentuh ATAU melebihi
     level. Tidak ada syarat arah candle TEST1 sendiri.
   - FILTER: candle H1 TEPAT SEBELUM TEST1 harus BERLAWANAN arah dgn setup -- Long: close <
     open (bearish); Short: close > open (bullish). Doji (close == open) tidak lolos.
     Kalau gagal, level gugur.
   - TEST2: candle TEPAT SETELAH TEST1, harus ENGULFING -- Support: ujung body TEST2
     > high TEST1; Resistance: ujung body TEST2 < low TEST1. Plus body TEST2 harus
     lebih besar dari body TEST1 DAN lebih besar dari TOTAL wick TEST2 sendiri.
     Sisi BERLAWANAN tidak boleh melebihi TEST1 (wick maupun body): Long -> low TEST2
     tidak boleh di bawah low TEST1; Short -> high TEST2 tidak boleh di atas high TEST1.
     Kalau gagal, level gugur (hanya dicoba sekali).

3) ENTRY -- setelah candle H1 TEST2 closed, MONITORING M5 dengan RSI(RSI_PERIOD=5).
   Candle M5 pertama yang dinilai = yang open-nya tepat saat H1 TEST2 closed (mis. H1
   14:00 closed -> M5 15:00, bukan 14:55); tiap candle dinilai saat CLOSED.
   AREA ENTRY: batas = ujung candle H1 SEBELUM TEST1 (Long -> HIGH-nya, Short -> LOW-nya).
   Long : hanya BODY candle M5 yang sepenuhnya di BAWAH batas (max(open,close) < batas)
          yang boleh dipakai. Body di atas batas / menembus batas (di tengah) -> tidak
          dipakai, tunggu candle berikutnya. Short: kebalikannya (min(open,close) > batas).
          Ujung body TEPAT di batas = DI LUAR area. Yang dihitung hanya BODY: wick yang
          keluar area tidak masalah selama body-nya di dalam area.
   RSI: Long -> RSI < 20; Short -> RSI > 50. Candle yang RSI-nya cocok TAPI body di luar
   area tidak dipakai; terus menunggu. Begitu body di area DAN RSI cocok -> ENTRY MARKET
   (di close candle M5 itu, posisi aktif mulai candle M5 berikutnya). Dipantau terus sampai
   entry atau kadaluarsa EXPIRE_CANDLES (4) candle H1 setelah TEST2. Tiap level 1x.
   Sinyal searah baru dibuang kalau masih ada sinyal menunggu / posisi aktif searah
   di koin yang sama.

4) STOP LOSS = PERGERAKAN TERJAUH + SL_BUFFER_PCT (0.25%). Pergerakan terjauh = low
   terendah (Long) / high tertinggi (Short) yang dicapai sejak candle M5 PERTAMA yang
   masuk area entry sampai candle entry (inklusif), sebelum RSI valid dan entry. SL =
   ekstrem tsb digeser 0.25% menjauh (Long: x0.9975, Short: x1.0025). Jarak entry-SL = 1R
   untuk ukuran posisi (risiko RISK_PCT dari balance).
   TRAILING STOP: aktif di TRAIL_ACTIVATE_R (3R), SL mengikuti TRAIL_STOP_R (1R) di
   belakang harga ekstrem (dipantau M5). Kalau di candle M5 yg sama harga menyentuh
   SL dan trailing aktif/naik sekaligus -> yg dihitung SL (SL dicek duluan).

Deploy ke Railway:
  Start command -> python backtest_web.py
  Buka domain Railway -> lihat progress & hasil di browser (auto-refresh)

Periode default: 1 tahun ke belakang dari hari script dijalankan (bisa
di-override lewat env BACKTEST_START_DATE / BACKTEST_END_DATE).

trades.csv ringkas (waktu WIB, UTC+7), kolom: Koin, Dir, Hasil ('Trail +1.6R'
atau 'SL'), Level, Terbentuk, Test1, Test2, Entry, Entry time.
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
RISK_PCT         = float(os.environ.get('RISK_PCT', '0.05'))          # risk per trade dari balance (compound)
FEE_ENTRY_PCT    = float(os.environ.get('FEE_ENTRY_PCT', '0.001'))
FEE_EXIT_PCT     = float(os.environ.get('FEE_EXIT_PCT', str(0.001 * 3)))

EMA_FAST         = int(os.environ.get('EMA_FAST', 4))
EMA_SLOW         = int(os.environ.get('EMA_SLOW', 10))
EXPIRE_CANDLES   = int(os.environ.get('EXPIRE_CANDLES', 4))          # entry kadaluarsa N candle H1 setelah TEST2 closed
SL_BUFFER_PCT    = float(os.environ.get('SL_BUFFER_PCT', '0.0025'))  # SL = pergerakan terjauh di area entry + 0.25%
RSI_PERIOD       = int(os.environ.get('RSI_PERIOD', 5))              # RSI M5
RSI_LONG_MAX     = float(os.environ.get('RSI_LONG_MAX', '20'))       # Long entry kalau RSI < 20
RSI_SHORT_MIN    = float(os.environ.get('RSI_SHORT_MIN', '50'))      # Short entry kalau RSI > 50
TRAIL_ACTIVATE_R = float(os.environ.get('TRAIL_ACTIVATE_R', '3.0'))  # trailing aktif begitu profit capai 3R (sama dgn bot)
TRAIL_STOP_R     = float(os.environ.get('TRAIL_STOP_R', '1.0'))      # setelah aktif, SL mengikuti 1R di belakang harga tertinggi/terendah

LEVERAGE           = float(os.environ.get('LEVERAGE', '50'))
MARGIN_USAGE_CAP    = float(os.environ.get('MARGIN_USAGE_CAP', '0.90'))

MAX_CONCURRENT_RAW = os.environ.get('MAX_CONCURRENT', '10').strip().lower()
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

# Semua koin yang dipakai backtest.
# CATATAN: daftar ini dipilih dari strategi lama -- jalankan ulang backtest dgn
# strategi sekarang lalu filter ulang kalau perlu.
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
    'blocked_by_slot': 0, 'blocked_by_margin': 0, 'blocked_by_min_order': 0,
    'blocked_by_invalid_sl': 0, 'blocked_by_dup': 0,
    'no_test1_count': 0, 'prev_fail_count': 0, 'test2_fail_count': 0, 'dup_signal_count': 0, 'expired_count': 0,
    'pending_count': 0, 'levels_count': 0, 'filled_count': 0,
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
# DETEKSI LEVEL SUPPORT / RESISTANCE + EMA CROSS (H1) -- sama dgn bot
# ============================================================

N_RIGHT  = 1       # candle kanan (c3) yang wajib bersih (wick tidak boleh menyentuh level)
N_LEFT   = int(os.environ.get('N_LEFT', 0))   # candle kiri (sebelum c1) yang wajib bersih; 0 = NONAKTIF (hanya kanan)
WICK_EPS = 1e-9
H1_MS    = 3600 * 1000
M5_MS    = 5 * 60 * 1000


def find_levels(df):
    """Deteksi level Support & Resistance dari candle H1 (basis body candle).
    TANPA syarat wick c1/c2.
    Syarat kiri : N_LEFT candle SEBELUM c1 tidak boleh melebihi level -- default
                  N_LEFT=0 = NONAKTIF (Support: low > level; Resistance: high < level).
    Syarat kanan: N_RIGHT candle setelah c1,c2 (c3) -- wick tidak boleh menyentuh level.
    Syarat EMA CROSS di RENTANG c2-c4: cukup SALAH SATU dari c2, c3, ATAU c4 yang
    menjadi penyebab cross EMA_FAST/EMA_SLOW (dari close H1) yang searah --
    Support -> GOLDEN CROSS, Resistance -> DEATH CROSS.
    Return list dict: {'type','level','patokan','c1','c2','c_right'}."""
    o = df['open'].values; h = df['high'].values; l = df['low'].values; c = df['close'].values
    n = len(df)
    ema_fast = pd.Series(c).ewm(span=EMA_FAST, adjust=False).mean().values
    ema_slow = pd.Series(c).ewm(span=EMA_SLOW, adjust=False).mean().values

    def golden_cross_at(j):   # cross TEPAT di index j (dibanding j-1)
        return ema_fast[j - 1] <= ema_slow[j - 1] and ema_fast[j] > ema_slow[j]

    def death_cross_at(j):
        return ema_fast[j - 1] >= ema_slow[j - 1] and ema_fast[j] < ema_slow[j]

    levels = []
    for i in range(N_LEFT, n - 3):   # perlu c0 (i-1) .. c4 (i+3) semua ada di dalam data
        golden_cross_c2_c4 = any(golden_cross_at(j) for j in (i + 1, i + 2, i + 3))
        death_cross_c2_c4  = any(death_cross_at(j) for j in (i + 1, i + 2, i + 3))
        if c[i] < o[i] and c[i + 1] > o[i + 1]:          # bearish lalu bullish -> support
            S = c[i]
            right_ok = all(l[i + 2 + k] > S + 1e-9 for k in range(N_RIGHT))
            left_ok = all(l[i - 1 - k] > S + 1e-9 for k in range(N_LEFT))
            if right_ok and left_ok and golden_cross_c2_c4:
                levels.append({'type': 'support', 'level': float(S), 'patokan': float(S),
                               'c1': i, 'c2': i + 1, 'c_right': [i + 2 + k for k in range(N_RIGHT)]})
        if c[i] > o[i] and c[i + 1] < o[i + 1]:          # bullish lalu bearish -> resistance
            R = c[i]
            right_ok = all(h[i + 2 + k] < R - 1e-9 for k in range(N_RIGHT))
            left_ok = all(h[i - 1 - k] < R - 1e-9 for k in range(N_LEFT))
            if right_ok and left_ok and death_cross_c2_c4:
                levels.append({'type': 'resistance', 'level': float(R), 'patokan': float(R),
                               'c1': i, 'c2': i + 1, 'c_right': [i + 2 + k for k in range(N_RIGHT)]})
    return levels


def _test_candidates(df, stats):
    """TEST1 + TEST2 (engulfing) utk tiap level. Return list kandidat sinyal
    (belum disimulasikan fill-nya), urut ready_ts. ready_ts = open ts candle TEST2;
    sinyal baru bisa diproses setelah candle itu closed (ready_ts + 1 jam)."""
    o = df['open'].values; h = df['high'].values; l = df['low'].values; c = df['close'].values
    ts = df['ts'].values
    n = len(df)
    cands = []

    for lv in find_levels(df):
        stats['levels'] += 1
        level = lv['level']; ty = lv['type']; patokan = lv['patokan']
        last_right_i = lv['c_right'][-1]

        # TEST1: candle pertama setelah c3 yang menyentuh/melebihi patokan (k < n-1 supaya TEST2 ada)
        start = last_right_i + 1
        if start >= n - 1:
            stats['no_test1'] += 1
            continue
        if ty == 'support':
            seg = l[start:n - 1] <= patokan + WICK_EPS
        else:
            seg = h[start:n - 1] >= patokan - WICK_EPS
        if not seg.any():
            stats['no_test1'] += 1
            continue
        test1_i = start + int(seg.argmax())
        t2 = test1_i + 1

        # FILTER: candle H1 sebelum TEST1 harus berlawanan arah dgn setup
        # (Long -> bearish: close < open; Short -> bullish: close > open). Doji gugur.
        pv = test1_i - 1
        prev_ok = (c[pv] < o[pv]) if ty == 'support' else (c[pv] > o[pv])
        if not prev_ok:
            stats['prev_fail'] += 1
            continue

        # TEST2: engulfing. Sisi BERLAWANAN tidak boleh melebihi TEST1 (wick/body):
        # Long -> low TEST2 tidak boleh di bawah low TEST1; Short -> high TEST2 tidak
        # boleh di atas high TEST1 (sama persis masih lolos).
        if ty == 'support':
            engulf_ok = max(o[t2], c[t2]) > h[test1_i] + WICK_EPS
            opposite_ok = l[t2] >= l[test1_i] - WICK_EPS
        else:
            engulf_ok = min(o[t2], c[t2]) < l[test1_i] - WICK_EPS
            opposite_ok = h[t2] <= h[test1_i] + WICK_EPS
        body_size_t1 = abs(c[test1_i] - o[test1_i])
        body_size_t2 = abs(c[t2] - o[t2])
        wick_total_t2 = (h[t2] - max(o[t2], c[t2])) + (min(o[t2], c[t2]) - l[t2])
        if not (engulf_ok and opposite_ok
                and body_size_t2 > body_size_t1 + WICK_EPS
                and body_size_t2 > wick_total_t2 + WICK_EPS):
            stats['test2_fail'] += 1
            continue

        direction = 'Long' if ty == 'support' else 'Short'
        # batas AREA ENTRY = ujung candle H1 sebelum TEST1 (Long -> high, Short -> low)
        zone_edge = float(h[test1_i - 1]) if direction == 'Long' else float(l[test1_i - 1])
        cands.append({
            'kind': 'SNR_SUPPORT' if ty == 'support' else 'SNR_RESISTANCE',
            'type': ty, 'level': level, 'direction': direction, 'zone_edge': zone_edge,
            'ready_ts': int(ts[t2]), 'test1_ts': int(ts[test1_i]), 'c1_ts': int(ts[lv['c1']]),
        })
    cands.sort(key=lambda e: e['ready_ts'])
    return cands


def _rsi_entry(m5, ev):
    """Monitoring M5 dgn RSI + AREA ENTRY setelah candle H1 TEST2 closed.
    - Candle M5 pertama yang dinilai = open-nya tepat saat H1 TEST2 closed
      (ready_ts + 1 jam); dinilai saat candle itu CLOSED (RSI dari close-nya).
    - AREA ENTRY (batas ev['zone_edge'] = ujung candle H1 sebelum TEST1):
        Long : body M5 sepenuhnya di bawah batas  -> max(open, close) < batas
        Short: body M5 sepenuhnya di atas batas   -> min(open, close) > batas
      Body di luar area, menembus batas, atau ujung body TEPAT di batas -> candle
      tidak dipakai. Hanya BODY yang dihitung; wick boleh keluar area.
    - Long: RSI < RSI_LONG_MAX; Short: RSI > RSI_SHORT_MIN. Entry = candle PERTAMA yang
      body-nya di area DAN RSI cocok -> ENTRY MARKET di close candle itu; posisi aktif
      mulai candle M5 berikutnya (fill_ts).
    - SL = pergerakan terjauh + SL_BUFFER_PCT: low terendah (Long) / high tertinggi
      (Short) dari candle M5 pertama yang masuk area sampai candle entry (inklusif).
    - Dipantau terus sampai entry atau kadaluarsa: EXPIRE_CANDLES candle H1 setelah
      TEST2 (candle M5 dgn open >= ready_ts + (EXPIRE_CANDLES + 1) jam tidak dihitung).
    Return (status, signal_idx, entry_price, end_ts, sl_price):
      status 'fill' | 'expired' | 'pending'."""
    TS, O, H, L, C, RSI = m5['TS'], m5['O'], m5['H'], m5['L'], m5['C'], m5['RSI']
    d = ev['direction']
    edge = ev['zone_edge']
    start = ev['ready_ts'] + H1_MS
    end_ts = ev['ready_ts'] + (EXPIRE_CANDLES + 1) * H1_MS
    a = int(np.searchsorted(TS, start, side='left'))
    b = int(np.searchsorted(TS, end_ts, side='left'))
    if b > a:
        if d == 'Long':
            in_zone = np.maximum(O[a:b], C[a:b]) < edge
            rsi_ok = RSI[a:b] < RSI_LONG_MAX            # NaN -> False
        else:
            in_zone = np.minimum(O[a:b], C[a:b]) > edge
            rsi_ok = RSI[a:b] > RSI_SHORT_MIN
        valid = in_zone & rsi_ok
        if valid.any():
            k = int(valid.argmax())                      # entry (relatif terhadap a)
            first_zone = int(in_zone.argmax())           # candle pertama yang masuk area (<= k)
            idx = a + k
            if idx + 1 >= len(TS):
                return 'pending', None, None, end_ts, None   # tidak ada candle setelah sinyal
            if d == 'Long':
                extreme = float(L[a + first_zone: idx + 1].min())
                sl = extreme * (1 - SL_BUFFER_PCT)
            else:
                extreme = float(H[a + first_zone: idx + 1].max())
                sl = extreme * (1 + SL_BUFFER_PCT)
            return 'fill', idx, float(C[idx]), end_ts, sl
    if len(TS) == 0 or int(TS[-1]) + M5_MS < end_ts:
        return 'pending', None, None, end_ts, None
    return 'expired', None, None, end_ts, None


def detect_snr_events(df, m5):
    """Level -> TEST1/TEST2 -> monitoring M5 (area entry + RSI) -> entry market,
    SL = pergerakan terjauh di area + SL_BUFFER_PCT.
    Dedup seperti bot: sinyal searah di koin yang sama dibuang kalau masih ada
    sinyal menunggu (belum entry/kadaluarsa) saat TEST2-nya closed. Posisi aktif
    searah ditangani di simulasi (open_trade).
    Return (events, stats). events urut fill_ts, dict:
    {'kind','type','level','direction','entry_price','sl_price','fill_ts',
     'test1_ts','test2_ts','c1_ts'}  (fill_ts = open candle M5 SETELAH candle sinyal)"""
    stats = {'levels': 0, 'entry': 0, 'no_test1': 0, 'prev_fail': 0, 'test2_fail': 0, 'dup': 0,
             'expired': 0, 'pending': 0}
    events = []
    cands = _test_candidates(df, stats)
    if m5 is None:
        stats['pending'] = len(cands)
        return events, stats

    busy_until = {'Long': 0, 'Short': 0}
    for ev in cands:
        d = ev['direction']
        if ev['ready_ts'] + H1_MS < busy_until[d]:
            stats['dup'] += 1      # masih ada sinyal searah menunggu -> dibuang
            continue
        status, idx, price, end_ts, sl = _rsi_entry(m5, ev)
        if status == 'expired':
            stats['expired'] += 1
            busy_until[d] = end_ts
            continue
        if status != 'fill':
            stats['pending'] += 1
            busy_until[d] = end_ts
            continue
        fill_ts = int(m5['TS'][idx + 1])
        busy_until[d] = fill_ts

        stats['entry'] += 1
        events.append({
            'kind': ev['kind'], 'type': ev['type'], 'level': ev['level'], 'direction': d,
            'entry_price': price, 'sl_price': sl, 'fill_ts': fill_ts,
            'test1_ts': ev['test1_ts'], 'test2_ts': ev['ready_ts'], 'c1_ts': ev['c1_ts'],
        })

    events.sort(key=lambda e: e['fill_ts'])
    return events, stats


def detect_all_events(df, m5):
    """Support & Resistance + EMA cross + TEST1/TEST2 + RSI M5 -- SNR_SUPPORT (Long) dan
    SNR_RESISTANCE (Short). Dedup: (kind, level, fill_ts) sama persis = 1 sinyal."""
    events, stats = detect_snr_events(df, m5)
    seen = set()
    deduped = []
    for e in events:
        dedup_key = (e['kind'], round(e['level'], 10), e['fill_ts'])
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        deduped.append(e)
    deduped.sort(key=lambda e: e['fill_ts'])
    return deduped, stats


# ============================================================
# STRUKTUR M5 UTK LOOKUP CEPAT
# ============================================================

def _rsi_wilder(close, period):
    """RSI Wilder (RMA) -- sama dgn RSI standar TradingView/Bybit. NaN sebelum cukup data."""
    n = len(close)
    rsi = np.full(n, np.nan)
    if n <= period:
        return rsi
    diff = np.diff(close)
    gain = np.where(diff > 0, diff, 0.0)
    loss = np.where(diff < 0, -diff, 0.0)
    avg_g = float(gain[:period].mean())
    avg_l = float(loss[:period].mean())

    def _val(g, l):
        if l == 0:
            return 50.0 if g == 0 else 100.0
        return 100.0 - 100.0 / (1.0 + g / l)

    rsi[period] = _val(avg_g, avg_l)
    for i in range(period + 1, n):
        avg_g = (avg_g * (period - 1) + gain[i - 1]) / period
        avg_l = (avg_l * (period - 1) + loss[i - 1]) / period
        rsi[i] = _val(avg_g, avg_l)
    return rsi


def prepare_m5(df_m5):
    if df_m5 is None or df_m5.empty:
        return None
    df_m5 = df_m5.sort_values('ts').reset_index(drop=True)
    close = df_m5['close'].values.astype(float)
    return {
        'TS': df_m5['ts'].values.astype(np.int64),
        'O': df_m5['open'].values, 'H': df_m5['high'].values,
        'L': df_m5['low'].values, 'C': df_m5['close'].values,
        'RSI': _rsi_wilder(close, RSI_PERIOD),
    }


# ============================================================
# PERSIAPAN PER-KOIN
# ============================================================

def prepare_coin(symbol, df, m5):
    df = df.sort_values('ts').reset_index(drop=True)
    events, stats = detect_all_events(df, m5)
    return {
        'symbol': symbol,
        'TS': df['ts'].values.astype(np.int64),
        'events': events,   # list limit entry yang terfill, urut fill_ts
        'stats': stats,
        'n': len(df),
    }


# ============================================================
# SIMULASI GABUNGAN (semua koin, 1 balance, 1 pool slot)
# ============================================================
#
# Tiap event dari detect_all_events() SUDAH final: entry market sudah terjadi di fill_ts
# (entry_price = close candle M5 sinyal RSI di area entry, sl_price = pergerakan terjauh + 0.25%). Simulasi membuka
# posisi saat waktu (M5) sampai di fill_ts event itu. Kalau slot/margin/min order
# tidak cukup, atau sudah ada posisi searah di koin yang sama, setup hangus.

def run_combined_backtest(coins: dict, m5_data: dict) -> dict:
    balance = INITIAL_BALANCE
    active_positions = {}     # key(symbol,direction+level) -> {...}
    trades = []

    def _akey(symbol, direction, level, kind):
        return f"{symbol}|{direction}|{kind}|{level:.10f}"

    total_margin_used = 0.0   # dijaga incremental

    def _slots_used():
        return len(active_positions)

    positions_by_symbol = {}   # symbol -> set(keys)

    pending_fills = {
        symbol: sorted([(ev['fill_ts'], idx) for idx, ev in enumerate(cp['events'])])
        for symbol, cp in coins.items()
    }

    def open_trade(symbol, ev, entry_price, entry_ts):
        nonlocal balance, total_margin_used
        direction = ev['direction']
        sl = ev['sl_price']
        dist = abs(entry_price - sl)   # = 1R

        # bot: tiap arah per koin hanya boleh 1 posisi aktif
        for k in positions_by_symbol.get(symbol, ()):
            if active_positions[k]['direction'] == direction:
                return None, 'dup'

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
            'kind': ev['kind'], 'margin': margin_needed,
            'c1_ts': ev['c1_ts'], 'test1_ts': ev['test1_ts'], 'test2_ts': ev['test2_ts'],
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
            'test1_ts': pos['test1_ts'], 'test2_ts': pos['test2_ts'],
            'entry_wib': _fmt_wib(pos['entry_ts']),
            'exit_wib': _fmt_wib(exit_ts),
        })

    def process_symbol_tick(symbol, j):
        """Proses SEMUA hal (exit posisi & fill limit) untuk 1 simbol di index candle j."""
        m5 = m5_data[symbol]
        now_ts = int(m5['TS'][j])
        hi, lo = m5['H'][j], m5['L'][j]

        # 1) exit posisi aktif simbol ini (trailing stop: aktif di TRAIL_ACTIVATE_R,
        #    SL mengikuti TRAIL_STOP_R di belakang harga ekstrem).
        #    ATURAN SIMULTAN: kalau di candle M5 yg SAMA harga menyentuh SL
        #    (level SL SEBELUM candle ini) dan sekaligus trailing akan aktif /
        #    naik, maka yg dihitung = SL.
        keys = positions_by_symbol.get(symbol)
        if keys:
            for key in list(keys):
                pos = active_positions[key]
                direction = pos['direction']
                entry, dist = pos['entry'], pos['dist']
                reason_stop = 'TRAIL' if pos['trail_active'] else 'SL'

                if direction == 'Long':
                    if lo <= pos['sl'] + 1e-12:
                        close_trade(key, pos['sl'], reason_stop, now_ts)
                        continue
                    if hi > pos['extreme']:
                        pos['extreme'] = hi
                    profit_r = (pos['extreme'] - entry) / dist
                    if not pos['trail_active'] and profit_r >= TRAIL_ACTIVATE_R:
                        pos['trail_active'] = True
                    if pos['trail_active']:
                        new_sl = pos['extreme'] - TRAIL_STOP_R * dist
                        if new_sl > pos['sl']:
                            pos['sl'] = new_sl   # SL cuma boleh naik
                            if lo <= pos['sl'] + 1e-12:
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

        # 2) entry market simbol ini terjadi di candle ini (open candle = harga entry).
        #    Slot/margin/min order tidak cukup -> setup hangus.
        #    Kalau candle fill yang sama juga sudah menyentuh SL -> langsung kena SL.
        plist = pending_fills.get(symbol)
        while plist and plist[0][0] <= now_ts:
            _, idx = plist.pop(0)
            ev = coins[symbol]['events'][idx]
            opened_key, block_reason = open_trade(symbol, ev, ev['entry_price'], now_ts)
            if opened_key is None:
                nonlocal_blocks[block_reason] += 1
            else:
                pos = active_positions[opened_key]
                if (pos['direction'] == 'Long' and lo <= pos['sl'] + 1e-12) or \
                   (pos['direction'] == 'Short' and hi >= pos['sl'] - 1e-12):
                    close_trade(opened_key, pos['sl'], 'SL', now_ts)

    nonlocal_blocks = {'slot': 0, 'margin': 0, 'min_order': 0, 'invalid_sl': 0, 'dup': 0}

    # ── TIMELINE EFISIEN via K-WAY MERGE (pointer index) ──
    import heapq

    ptr = {symbol: 0 for symbol in coins}
    m5_ts_arr = {symbol: m5['TS'] for symbol, m5 in m5_data.items() if m5 is not None}
    m5_len = {symbol: len(arr) for symbol, arr in m5_ts_arr.items()}

    def _advance_ptr_to(symbol, target_ts):
        arr = m5_ts_arr.get(symbol)
        if arr is None:
            return
        i = ptr[symbol]
        n = m5_len[symbol]
        while i < n and arr[i] < target_ts:
            i += 1
        ptr[symbol] = i

    heap = []

    for symbol, cp in coins.items():
        if not cp['events'] or symbol not in m5_ts_arr:
            continue
        _advance_ptr_to(symbol, pending_fills[symbol][0][0])   # candle M5 fill pertama
        if ptr[symbol] < m5_len[symbol]:
            heapq.heappush(heap, (int(m5_ts_arr[symbol][ptr[symbol]]), symbol))

    while heap:
        now_ts, symbol = heapq.heappop(heap)
        i = ptr[symbol]
        if i >= m5_len[symbol] or m5_ts_arr[symbol][i] != now_ts:
            continue

        process_symbol_tick(symbol, i)

        if positions_by_symbol.get(symbol):
            ptr[symbol] = i + 1
        else:
            plist = pending_fills.get(symbol)
            if plist:
                _advance_ptr_to(symbol, plist[0][0])
            else:
                ptr[symbol] = m5_len[symbol]

        if ptr[symbol] < m5_len[symbol]:
            heapq.heappush(heap, (int(m5_ts_arr[symbol][ptr[symbol]]), symbol))

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
        'blocked_by_slot': nonlocal_blocks['slot'], 'blocked_by_margin': nonlocal_blocks['margin'],
        'blocked_by_min_order': nonlocal_blocks['min_order'],
        'blocked_by_invalid_sl': nonlocal_blocks['invalid_sl'],
        'blocked_by_dup': nonlocal_blocks['dup'],
    }


# ============================================================
# SIMULASI PER-KOIN (independen, bukan gabungan)
# ============================================================

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
    exit_ts tiap trade). Bulan tanpa trade tidak dimasukkan. Di akhir, hitung
    rata-rata pertumbuhan %/bulan (mean sederhana antar bulan yang ADA trade)."""
    if not trades:
        return [], 0.0
    trades_sorted = sorted(trades, key=lambda t: t['exit_ts'])
    by_month = {}
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
        _log_msg(f"🚀 Mulai backtest SNR (S&R + EMA{EMA_FAST}/{EMA_SLOW} cross + TEST1/TEST2 engulfing) — {len(SYMBOLS)} koin, {BACKTEST_START_DATE} s/d {BACKTEST_END_DATE}")
        _log_msg(f"   Syarat: level c1-c2 (c3 bersih) + EMA cross searah di c2-c4 -> TEST1 (sentuh level; candle H1 sebelumnya harus berlawanan arah) -> TEST2 engulfing "
                  f"(body>body TEST1, body>total wick, sisi berlawanan tidak melebihi TEST1) -> monitoring M5 (body di area entry = ujung candle H1 sebelum TEST1) RSI{RSI_PERIOD} "
                  f"(Long<{RSI_LONG_MAX:g}, Short>{RSI_SHORT_MIN:g}) -> entry market, kadaluarsa {EXPIRE_CANDLES} candle H1  "
                  f"SL = pergerakan terjauh di area + {SL_BUFFER_PCT*100:.2f}%  "
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
            m5_data[symbol] = prepare_m5(df_m5)
            cp = prepare_coin(symbol, df_h1, m5_data[symbol])
            coins[symbol] = cp
            n_by_kind = {}
            for ev in cp['events']:
                n_by_kind[ev['kind']] = n_by_kind.get(ev['kind'], 0) + 1
            kind_str = ', '.join(f"{k}:{v}" for k, v in sorted(n_by_kind.items()))
            st = cp['stats']
            _log_msg(f"   ✅ {symbol}: {cp['n']} candle H1, {st['levels']} level, {st['entry']} entry market ({kind_str or '-'}), "
                      f"{st['no_test1']} tanpa TEST1, {st['prev_fail']} gagal filter candle sebelum TEST1, {st['test2_fail']} gagal TEST2, {st['dup']} dibuang (sinyal searah masih menunggu), "
                      f"{st['expired']} kadaluarsa tanpa sinyal RSI, {st['pending']} belum selesai (data habis).")

        _log_msg(f"🧮 Menjalankan simulasi gabungan ({len(coins)} koin)...")
        result = run_combined_backtest(coins, m5_data)
        sum_st = lambda k: sum(cp['stats'][k] for cp in coins.values())

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
                'blocked_by_dup': result['blocked_by_dup'],
                'levels_count': sum_st('levels'),
                'filled_count': sum_st('entry'),
                'no_test1_count': sum_st('no_test1'),
                'prev_fail_count': sum_st('prev_fail'),
                'test2_fail_count': sum_st('test2_fail'),
                'dup_signal_count': sum_st('dup'),
                'expired_count': sum_st('expired'),
                'pending_count': sum_st('pending'),
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
  <h1>📊 Backtest SNR (S&R + EMA{EMA_FAST}/{EMA_SLOW} cross + TEST1/TEST2 + RSI{RSI_PERIOD} M5)</h1>
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
    💡 <b>Strategi sama dengan bot (S&R + EMA cross + TEST1/TEST2 engulfing)</b>:
    <br>• <b>Level</b> (H1): c1+c2 berlawanan arah, level = close c1. Candle c3 (kanan) tidak boleh menyentuh/melebihi level (wick pun tidak). Syarat kiri nonaktif.
    Wajib ada <b>EMA{EMA_FAST}/{EMA_SLOW} cross searah</b> di salah satu c2, c3, atau c4 (Support → golden cross, Resistance → death cross).
    <br>• <b>TEST1</b>: candle pertama setelah c3 yang wick/body-nya menyentuh/melebihi level.
    Candle H1 <b>tepat sebelum TEST1</b> harus berlawanan arah (Long: close &lt; open; Short: close &gt; open), kalau tidak level gugur.
    <b>TEST2</b>: candle tepat sesudahnya harus <b>engulfing</b> (Support: ujung body &gt; high TEST1; Resistance: ujung body &lt; low TEST1),
    body TEST2 &gt; body TEST1, body TEST2 &gt; total wick TEST2, dan sisi berlawanan tidak boleh melebihi TEST1
    (Long: low TEST2 tidak di bawah low TEST1; Short: high TEST2 tidak di atas high TEST1). Gagal → level gugur.
    <br>• <b>ENTRY</b>: setelah candle H1 TEST2 closed, monitoring <b>M5</b> dimulai dari candle M5 yang open-nya tepat saat H1 closed
    (dinilai saat M5 closed). <b>Area entry</b>: batas = ujung candle H1 sebelum TEST1 (Long → high, Short → low).
    Long: <b>body</b> M5 harus sepenuhnya di bawah batas; Short: sepenuhnya di atas batas. Body di luar area, menembus batas, atau ujung body tepat di batas → tidak dipakai (hanya body yang dihitung, wick boleh keluar area).
    <b>RSI{RSI_PERIOD}</b> M5: Long → RSI &lt; {RSI_LONG_MAX:g}, Short → RSI &gt; {RSI_SHORT_MIN:g}.
    Begitu body di area <b>dan</b> RSI cocok → <b>entry market</b> (di close candle itu). Dipantau terus sampai entry atau kadaluarsa
    <b>{EXPIRE_CANDLES} candle H1</b> setelah TEST2. Tiap level dipakai 1x; sinyal searah dibuang kalau masih ada
    sinyal menunggu / posisi aktif searah di koin yang sama.
    <br>• <b>SL</b> = pergerakan terjauh di area entry (low terendah Long / high tertinggi Short sejak candle pertama masuk area
    sampai candle entry) + <b>{SL_BUFFER_PCT*100:.2f}%</b> (= 1R untuk ukuran posisi).
    <b>Trailing stop</b>: aktif di <b>{TRAIL_ACTIVATE_R:.1f}R</b>, SL mengikuti <b>{TRAIL_STOP_R:.1f}R</b> di belakang harga ekstrem (dipantau M5).
    Kalau SL dan trailing tersentuh di candle M5 yang sama, yang dihitung <b>SL</b>.
    <br>⚙️ Risk {RISK_PCT*100:.0f}% dari balance (compounding). Slot maksimum: {_fmt_max_concurrent()}.
    Level terdeteksi: {cr.get('levels_count',0)}. Entry market: {cr.get('filled_count',0)}.
    Gugur — tanpa TEST1: {cr.get('no_test1_count',0)}, gagal filter candle sebelum TEST1: {cr.get('prev_fail_count',0)}, gagal TEST2: {cr.get('test2_fail_count',0)},
    sinyal searah masih menunggu: {cr.get('dup_signal_count',0)}, kadaluarsa tanpa sinyal RSI: {cr.get('expired_count',0)},
    belum selesai (data habis): {cr.get('pending_count',0)}.
    Sinyal terblokir saat entry — slot: {cr.get('blocked_by_slot',0)}, margin: {cr.get('blocked_by_margin',0)},
    min order: {cr.get('blocked_by_min_order',0)}, posisi searah masih aktif: {cr.get('blocked_by_dup',0)}.
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


def _fmt_wib_short(ts_ms):
    """'2026-07-07 12:40' (WIB, tanpa akhiran)."""
    return _fmt_wib(ts_ms).replace(' WIB', '')


def _fmt_price(x):
    return f"{float(x):.10g}"


def _hasil_str(t):
    """Trailing -> 'Trail +1.6R' (R bersih setelah fee); SL -> cukup 'SL'."""
    if t['reason'] == 'TRAIL':
        return f"Trail {t['r_mult']:+.1f}R"
    return 'SL'


CSV_COLUMNS = ['Koin', 'Dir', 'Hasil', 'Level', 'Terbentuk', 'Test1', 'Test2', 'Entry', 'Entry time']


def _trades_csv() -> bytes:
    """trades.csv ringkas, waktu dalam WIB (UTC+7). Test1 / Test2 = waktu OPEN candle
    H1 TEST1 / TEST2; Entry = harga market (close candle M5 sinyal RSI);
    Entry time = waktu OPEN candle M5 saat posisi aktif (candle setelah sinyal)."""
    with _lock:
        trades_cp = list(_all_trades)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for t in trades_cp:
        writer.writerow([
            t['symbol'], t['direction'].upper(), _hasil_str(t), _fmt_price(t['level']),
            _fmt_wib_short(t['level_formed_ts']), _fmt_wib_short(t['test1_ts']),
            _fmt_wib_short(t['test2_ts']), _fmt_price(t['entry']),
            _fmt_wib_short(t['entry_ts']),
        ])
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
