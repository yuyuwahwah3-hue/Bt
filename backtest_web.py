"""
backtest_web.py — Backtest strategi Support & Resistance + monitoring M5, H1/M5
================================================================================
Level Support/Resistance AWAL dari candle H1: c1,c2 berlawanan arah, 1 candle
SEBELUM c1 dan 1 candle SESUDAH c2 tidak boleh melebihi level. TANPA EMA sama
sekali. Setelah level tersentuh, harga DIPANTAU di M5:
body M5 harus menembus level KELUAR (candle break-out harus sebuah FVG, kalau
bukan -> setup gugur), lalu body M5 harus menutup SELURUH FVG itu (MASUK; kalau
hanya close di atas level tanpa menutup FVG -> gugur).
Begitu body M5 break MASUK -> pasang LIMIT entry di angka level itu, fill saat
harga retest level. SL = ujung wick terpanjang c1/c2. Tidak ada EMA, tidak ada
level kedua, tidak ada filter wick.

RINGKASAN STRATEGI
-------------------
1) DETEKSI LEVEL (H1) -- tanpa EMA:
   Support: candle c1 bearish (close<open) lalu c2 bullish (close>open).
            Level = close[c1].
   Resistance: candle c1 bullish (close>open) lalu c2 bearish (close<open).
            Level = close[c1].
   Valid kalau:
            - SESUDAH c2: N_RIGHT candle (default 1, c3 saja) tidak boleh melebihi
              level -- Support: low c3 > level; Resistance: high c3 < level
              (WICK-nya tidak boleh menyentuh level sama sekali, sama dengan level
              juga tidak lolos).
            - SEBELUM c1: N_LEFT candle (default 1, c0 saja) juga tidak boleh
              melebihi level -- Support: low c0 > level; Resistance: high c0 < level.
   Level SIAP begitu c3 closed -- monitoring M5 mulai dari situ (tanpa lookahead).

2) MONITORING M5 (mulai setelah level siap; level tersentuh otomatis tercakup
   karena body yang menembus level pasti menyentuhnya):
   Support:
     a) KELUAR : body M5 break PERTAMA DI BAWAH level (close M5 < level). Wick tidak
                 dihitung. Candle break (k) HARUS sebuah FVG yang melingkupi level:
                   - candle SEBELUMNYA (k-1): ujung wick BAWAH (low) di ATAS level
                   - candle SESUDAHNYA (k+1): ujung wick ATAS (high) di BAWAH level
                 Kalau body break pertama itu BUKAN FVG -> setup langsung GUGUR dan
                 tidak dipantau lagi. FVG baru terkonfirmasi saat candle k+1 closed.
     b) MASUK  : setelah (a), candle PERTAMA yang close-nya kembali DI ATAS level HARUS
                 sekaligus close DI ATAS low candle k-1 = ujung atas FVG (close M5 >
                 low[k-1]; harus MELEBIHI, sama tidak cukup) -> body menutup SELURUH
                 FVG. Kalau candle pertama itu hanya close di atas level tapi tidak
                 melebihi ujung FVG -> setup langsung GUGUR (tidak ditunggu candle
                 berikutnya, tidak dipantau lagi).
     c) GUGUR  : setelah (a), kalau harga M5 turun sampai LEVEL_FAIL_PCT (2%)
                 di bawah level (low <= level x 0.98) SEBELUM atau di candle yang
                 sama dengan (b) -> level gugur, tidak ada entry.
   Resistance: kebalikannya (KELUAR = body break pertama close M5 > level dengan FVG:
   high candle k-1 di BAWAH level dan low candle k+1 di ATAS level; MASUK = candle
   pertama yang close di BAWAH level harus sekaligus close di BAWAH high[k-1], kalau
   tidak -> gugur; GUGUR 2% kalau high >= level x 1.02).
   Level yang belum pernah break body tetap dipantau (tidak ada batas waktu).

3) ENTRY -- begitu (b) MASUK terjadi (candle M5 closed): pasang LIMIT di ANGKA LEVEL
   yang tadi ditembus. Support -> Long (buy limit), Resistance -> Short (sell
   limit). Limit FILL di candle M5 pertama SETELAH candle MASUK yang harganya
   menyentuh level (Long: low <= level; Short: high >= level), fill di harga level.
   Wick cukup untuk fill. Limit KADALUARSA kalau tidak terfill dalam
   LIMIT_EXPIRE_HOURS (default 4 jam) sejak candle MASUK closed -> setup gugur.
   Level MATI setelah 1x entry (terfill atau gugur). Kalau slot/margin/min order tidak cukup saat fill,
   setup hangus.

4) STOP LOSS -- TIDAK fix. SL = ujung WICK TERPANJANG antara c1 dan c2 (H1):
   Support: low dari wick bawah terpanjang; Resistance: high dari wick atas
   terpanjang. Panjang wick diukur dari ujung body ke ujung wick. Hanya wick yang
   ujungnya melewati level ke arah SL yang dihitung (SL tidak boleh di sisi yang
   salah dari entry); kalau c1 maupun c2 tidak punya, level dilewati (tidak ada
   SL valid). Kalau candle fill juga sudah menyentuh SL -> langsung kena SL.
   Ukuran posisi = risiko RISK_PCT / jarak entry-SL (jarak itu = 1R).
   TRAILING STOP: aktif di TRAIL_ACTIVATE_R (2R), SL mengikuti TRAIL_STOP_R (1R)
   di belakang harga ekstrem (dipantau M5). Kalau di candle M5 yg sama harga
   menyentuh SL dan trailing sekaligus aktif/naik -> yg dihitung SL (SL dicek
   duluan).

Deploy ke Railway:
  Start command -> python backtest_web.py
  Buka domain Railway -> lihat progress & hasil di browser (auto-refresh)

Periode default: 1 tahun ke belakang dari hari script dijalankan (bisa
di-override lewat env BACKTEST_START_DATE / BACKTEST_END_DATE).

trades.csv ringkas (waktu WIB, UTC+7), kolom: Koin, Dir, Hasil ('Trail +1.6R'
atau 'SL'), Level, Terbentuk, Break out, Break in, Entry, Entry time.
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

LIMIT_EXPIRE_HOURS = float(os.environ.get('LIMIT_EXPIRE_HOURS', '4'))   # limit tidak terfill dalam N jam sejak candle MASUK closed -> gugur
LEVEL_FAIL_PCT   = float(os.environ.get('LEVEL_FAIL_PCT', '0.02'))   # setelah body M5 break KELUAR, kalau harga sampai 2% dari level -> level gugur
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
# CATATAN: daftar ini dipilih dari strategi lama (EMA cross + TEST1/TEST2) -- jalankan
# ulang backtest dgn strategi sekarang lalu filter ulang kalau perlu.
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
    'failed_count': 0, 'expired_count': 0, 'nofvg_count': 0, 'partial_count': 0,
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

N_RIGHT = 1   # jumlah candle SETELAH c2 yang tidak boleh melebihi level -- cukup c3 saja
N_LEFT = 1    # jumlah candle SEBELUM c1 yang tidak boleh melebihi level -- cukup c0 saja

def _longest_wick_tip(o, h, l, c, i, ty, level):
    """Ujung wick TERPANJANG antara c1 (index i) dan c2 (i+1) = harga SL.
    Support   : wick bawah, panjang = min(open,close) - low, ujung = low.
    Resistance: wick atas,  panjang = high - max(open,close), ujung = high.
    Hanya wick yang ujungnya benar-benar melewati level ke arah SL (support: low <
    level; resistance: high > level) yang dihitung -- SL tidak boleh di sisi yang
    salah dari entry. Seri -> c1. Return None kalau tidak ada yg valid."""
    best_len, best_tip = None, None
    for idx in (i, i + 1):
        if ty == 'support':
            if not (l[idx] < level - 1e-12):
                continue
            wl, tip = min(o[idx], c[idx]) - l[idx], l[idx]
        else:
            if not (h[idx] > level + 1e-12):
                continue
            wl, tip = h[idx] - max(o[idx], c[idx]), h[idx]
        if best_len is None or wl > best_len:
            best_len, best_tip = wl, tip
    return None if best_tip is None else float(best_tip)


def find_levels(df):
    """Deteksi level Support & Resistance dari candle H1 (TANPA EMA).
    Support   : c1 bearish lalu c2 bullish, level = close[c1].
    Resistance: c1 bullish lalu c2 bearish, level = close[c1].
    Syarat kanan: N_RIGHT candle SETELAH c2 (c3) tidak boleh melebihi level --
                  support: low > level; resistance: high < level (WICK-nya tidak
                  boleh menyentuh level sama sekali, sama dengan level juga gagal).
    Syarat kiri : N_LEFT candle SEBELUM c1 (c0) juga tidak boleh melebihi level --
                  support: low > level; resistance: high < level.
    c1/c2 boleh sama sekali tidak punya wick.
    'confirm_i' = index candle H1 TERAKHIR yang dibutuhkan untuk memastikan level
                  valid = c3. Level siap (tanpa lookahead) begitu candle ini closed.
    'wick_tip' = ujung wick terpanjang c1/c2 (harga SL), None kalau tidak ada yg valid.
    Return list dict: {'type', 'level', 'c1', 'c2', 'c_left', 'c_right', 'confirm_i', 'wick_tip'}."""
    o = df['open'].values; h = df['high'].values; l = df['low'].values; c = df['close'].values
    n = len(df)

    levels = []
    for i in range(N_LEFT, n - 1 - N_RIGHT):   # perlu c0 (i-1) .. c3 (i+2) semua ada di dalam data
        c_left = [i - 1 - k for k in range(N_LEFT)]
        c_right = [i + 2 + k for k in range(N_RIGHT)]
        if c[i] < o[i] and c[i + 1] > o[i + 1]:          # bearish lalu bullish -> support
            S = c[i]
            if all(l[j] > S + 1e-9 for j in c_left) and all(l[j] > S + 1e-9 for j in c_right):
                levels.append({'type': 'support', 'level': float(S), 'c1': i, 'c2': i + 1,
                               'c_left': c_left, 'c_right': c_right, 'confirm_i': c_right[-1],
                               'wick_tip': _longest_wick_tip(o, h, l, c, i, 'support', float(S))})
        if c[i] > o[i] and c[i + 1] < o[i + 1]:          # bullish lalu bearish -> resistance
            R = c[i]
            if all(h[j] < R - 1e-9 for j in c_left) and all(h[j] < R - 1e-9 for j in c_right):
                levels.append({'type': 'resistance', 'level': float(R), 'c1': i, 'c2': i + 1,
                               'c_left': c_left, 'c_right': c_right, 'confirm_i': c_right[-1],
                               'wick_tip': _longest_wick_tip(o, h, l, c, i, 'resistance', float(R))})
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

def _m5_break_entry(m5, ty, level, start_ts):
    """Monitoring M5 satu level mulai start_ts (tanpa lookahead, semua pakai close candle).
    Support : KELUAR = body break PERTAMA close < level. Candle itu (k) harus FVG yang
              melingkupi level: low[k-1] > level DAN high[k+1] < level. Bukan FVG ->
              'nofvg' (setup gugur, tidak dipantau lagi).
              MASUK = candle PERTAMA setelah k yang close-nya > level; candle itu harus
              sekaligus close > low[k-1] (ujung atas FVG, harus melebihi). Kalau hanya
              close > level tapi <= low[k-1] -> 'partial' (gugur, tidak dipantau lagi).
              GUGUR = low <= level x (1 - LEVEL_FAIL_PCT) (setelah/di candle keluar).
    Resistance: kebalikannya (KELUAR close > level dgn high[k-1] < level & low[k+1] >
              level; MASUK = candle pertama close < level yg juga close < high[k-1];
              GUGUR high >= level x (1 + LEVEL_FAIL_PCT)).
    Urutan waktu menentukan: gugur 2% yang terjadi lebih dulu atau di candle yang sama
    dgn candle MASUK/partial dihitung 'failed' (konservatif).
    Return (status, k_out, m_in):
      'none'    -- belum pernah break body
      'waiting' -- break body pertama terjadi di candle terakhir data (FVG belum
                   bisa dikonfirmasi) ATAU sudah keluar, belum ada close balik di sisi level
      'nofvg'   -- break body pertama bukan FVG -> gugur
      'partial' -- close balik pertama di sisi level tidak menutup seluruh FVG -> gugur
      'failed'  -- gugur (2%) sebelum masuk
      'entry'   -- masuk di index m_in (k_out = index candle keluar)"""
    TS, H, L, C = m5['TS'], m5['H'], m5['L'], m5['C']
    n5 = len(TS)
    s0 = int(np.searchsorted(TS, start_ts, side='left'))
    if s0 >= n5:
        return 'none', None, None
    if ty == 'support':
        out_mask = C[s0:] < level
    else:
        out_mask = C[s0:] > level
    if not out_mask.any():
        return 'none', None, None
    k = s0 + int(out_mask.argmax())          # body break PERTAMA
    if k == 0:
        return 'nofvg', k, None              # tidak ada candle sebelumnya utk dicek FVG
    if k >= n5 - 1:
        return 'waiting', k, None            # candle k+1 belum ada -> FVG belum terkonfirmasi

    if ty == 'support':
        is_fvg = L[k - 1] > level and H[k + 1] < level
        in_ref = float(L[k - 1])             # ujung bawah candle sebelum break-out = ujung atas FVG
    else:
        is_fvg = H[k - 1] < level and L[k + 1] > level
        in_ref = float(H[k - 1])             # ujung atas candle sebelum break-out = ujung bawah FVG
    if not is_fvg:
        return 'nofvg', k, None

    if ty == 'support':
        fail_mask = L[k:] <= level * (1 - LEVEL_FAIL_PCT)
        back_mask = C[k + 1:] > level        # close balik di sisi level (percobaan MASUK pertama)
    else:
        fail_mask = H[k:] >= level * (1 + LEVEL_FAIL_PCT)
        back_mask = C[k + 1:] < level
    g = (k + int(fail_mask.argmax())) if fail_mask.any() else None
    m0 = (k + 1 + int(back_mask.argmax())) if back_mask.any() else None
    if m0 is None:
        return ('failed' if g is not None else 'waiting'), k, None
    if g is not None and g <= m0:
        return 'failed', k, None
    closes_fvg = (C[m0] > in_ref) if ty == 'support' else (C[m0] < in_ref)
    if closes_fvg:
        return 'entry', k, m0
    return 'partial', k, None


def _m5_limit_fill(m5, ty, level, m_in):
    """Limit di angka level dipasang setelah candle MASUK (index m_in) closed.
    Fill di candle M5 pertama SETELAH m_in yang menyentuh level (wick cukup):
    Long (support): low <= level; Short (resistance): high >= level.
    KADALUARSA: kalau tidak terfill dalam LIMIT_EXPIRE_HOURS (default 4 jam) sejak
    candle MASUK closed -> batal. Candle yang open-nya >= waktu kadaluarsa tidak
    dihitung (dengan 4 jam & M5 = 48 candle setelah candle MASUK).
    Return (status, index):
      ('fill', idx)    -- terfill di candle idx
      ('expired', None)-- jendela waktu sudah lewat (ada data sesudahnya), tidak terfill
      ('pending', None)-- data habis sebelum jendela selesai, belum terfill"""
    TS = m5['TS']
    n5 = len(TS)
    expire_ts = int(TS[m_in]) + 5 * 60 * 1000 + int(LIMIT_EXPIRE_HOURS * 3600 * 1000)
    end = int(np.searchsorted(TS, expire_ts, side='left'))   # eksklusif
    if ty == 'support':
        mask = m5['L'][m_in + 1:end] <= level
    else:
        mask = m5['H'][m_in + 1:end] >= level
    if mask.any():
        return 'fill', m_in + 1 + int(mask.argmax())
    return ('expired' if end < n5 else 'pending'), None


def detect_snr_events(df, m5):
    """Level Support & Resistance AWAL (find_levels) -> monitoring M5 -> LIMIT entry.

    Per level: mulai dari candle M5 pertama setelah level siap (candle confirm_i
    closed), cari body M5 KELUAR (harus FVG yang melingkupi level, kalau bukan -> gugur)
    lalu body M5 MASUK (close balik pertama harus menutup seluruh FVG, kalau tidak ->
    gugur) -- lihat _m5_break_entry.
    Begitu MASUK: LIMIT di angka level, fill saat harga retest level
    (_m5_limit_fill). SL = ujung wick terpanjang c1/c2 (bukan persen tetap).
    Gugur kalau setelah keluar harga sampai LEVEL_FAIL_PCT (2%) dari level, atau
    limit tidak terfill dalam LIMIT_EXPIRE_HOURS (4 jam) sejak candle MASUK closed.

    Return (events, stats):
    events: list dict {'kind', 'type', 'level', 'direction', 'entry_price' (= level),
            'sl_price' (= wick_tip), 'fill_ts' (open ts candle M5 saat limit fill),
            'in_ts' (open ts candle M5 MASUK), 'out_ts', 'confirm_ts', 'c1_ts',
            'c1', 'c2'} urut fill_ts
    stats : {'levels', 'entry', 'nofvg', 'partial', 'failed', 'expired', 'pending', 'no_sl'}"""
    ts = df['ts'].values
    levels = find_levels(df)
    stats = {'levels': len(levels), 'entry': 0, 'nofvg': 0, 'partial': 0, 'failed': 0, 'expired': 0, 'pending': 0, 'no_sl': 0}
    events = []
    if m5 is None:
        stats['pending'] = len(levels)
        return events, stats

    for lv in levels:
        ty = lv['type']
        level = lv['level']
        if lv['wick_tip'] is None:
            stats['no_sl'] += 1   # c1 dan c2 tidak punya wick yg valid utk SL
            continue
        start_ts = int(ts[lv['confirm_i']]) + 3600 * 1000   # candle confirm closed
        status, k, m = _m5_break_entry(m5, ty, level, start_ts)
        if status == 'nofvg':
            stats['nofvg'] += 1   # break body pertama bukan FVG -> gugur
            continue
        if status == 'partial':
            stats['partial'] += 1   # close balik pertama tidak menutup seluruh FVG -> gugur
            continue
        if status == 'failed':
            stats['failed'] += 1
            continue
        if status != 'entry':
            stats['pending'] += 1
            continue
        fstatus, f = _m5_limit_fill(m5, ty, level, m)
        if fstatus == 'expired':
            stats['expired'] += 1   # limit tidak terfill dalam LIMIT_EXPIRE_HOURS -> gugur
            continue
        if fstatus != 'fill':
            stats['pending'] += 1   # data habis sebelum jendela limit selesai
            continue
        stats['entry'] += 1

        events.append({
            'kind': 'SNR_SUPPORT' if ty == 'support' else 'SNR_RESISTANCE',
            'type': ty, 'level': level,
            'direction': 'Long' if ty == 'support' else 'Short',
            'entry_price': level, 'sl_price': lv['wick_tip'],
            'fill_ts': int(m5['TS'][f]),
            'in_ts': int(m5['TS'][m]),
            'out_ts': int(m5['TS'][k]),
            'confirm_ts': int(ts[lv['confirm_i']]),
            'c1_ts': int(ts[lv['c1']]),
            'c1': lv['c1'], 'c2': lv['c2'],
        })

    events.sort(key=lambda e: e['fill_ts'])
    return events, stats


def detect_all_events(df, m5):
    """Support & Resistance + monitoring M5 + limit di level -- SNR_SUPPORT (Long) dan
    SNR_RESISTANCE (Short). Dedup: 2 event dgn (kind, level, fill_ts) SAMA
    PERSIS dianggap 1 sinyal yg sama. Return (events, stats)."""
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

def prepare_coin(symbol, df, m5):
    df = df.sort_values('ts').reset_index(drop=True)
    events, stats = detect_all_events(df, m5)
    return {
        'symbol': symbol,
        'TS': df['ts'].values.astype(np.int64),
        'events': events,   # list limit entry (break keluar -> masuk -> limit di level terisi), urut fill_ts
        'stats': stats,
        'n': len(df),
    }


# ============================================================
# SIMULASI GABUNGAN (semua koin, 1 balance, 1 pool slot)
# ============================================================
#
# Tiap event dari detect_all_events() SUDAH final: level sudah break keluar lalu
# MASUK di M5, limit di angka level sudah dipasang dan sudah tersentuh di fill_ts
# (entry_price = level, sl_price = ujung wick terpanjang c1/c2). Simulasi cukup
# membuka posisi saat waktu (M5) sampai di fill_ts event itu. Kalau slot/margin/
# min order tidak cukup, setup hangus.

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

    # pending_fills: event limit entry (limit di level tersentuh di fill_ts), urut asc per simbol.
    pending_fills = {
        symbol: sorted([(ev['fill_ts'], idx) for idx, ev in enumerate(cp['events'])])
        for symbol, cp in coins.items()
    }

    def open_trade(symbol, ev, entry_price, entry_ts):
        nonlocal balance, total_margin_used
        direction = ev['direction']
        sl = ev['sl_price']
        dist = abs(entry_price - sl)   # = 1R (jarak entry ke SL = ujung wick terpanjang c1/c2)

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
            'c1_ts': ev['c1_ts'], 'out_ts': ev['out_ts'], 'in_ts': ev['in_ts'],
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
            'out_ts': pos['out_ts'],
            'out_wib': _fmt_wib(pos['out_ts']),
            'in_ts': pos['in_ts'],
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

        # 2) event limit entry simbol ini: limit di angka level tersentuh di candle ini
        #    (fill di harga level). Slot/margin/min order tidak cukup -> setup hangus.
        #    Kalau candle fill yang sama juga sudah menyentuh SL (SL = ujung wick, lebih
        #    jauh dari level, jadi harga pasti lewat level dulu) -> langsung kena SL.
        #    Kalau tidak, SL/trailing dicek mulai candle M5 BERIKUTNYA.
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

    nonlocal_blocks = {'slot': 0, 'margin': 0, 'min_order': 0, 'invalid_sl': 0}

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
        _advance_ptr_to(symbol, pending_fills[symbol][0][0])   # candle M5 fill pertama
        if ptr[symbol] < m5_len[symbol]:
            heapq.heappush(heap, (int(m5_ts_arr[symbol][ptr[symbol]]), symbol))

    while heap:
        now_ts, symbol = heapq.heappop(heap)
        i = ptr[symbol]
        if i >= m5_len[symbol] or m5_ts_arr[symbol][i] != now_ts:
            continue   # stale entry (seharusnya tidak terjadi, safety check)

        process_symbol_tick(symbol, i)

        # advance pointer: kalau simbol masih punya posisi aktif (butuh dipantau tiap
        # candle), lanjut candle BERIKUTNYA (i+1, O(1)). Kalau tidak, lompat ke candle
        # fill event pending berikutnya (hemat banyak tick).
        if positions_by_symbol.get(symbol):
            ptr[symbol] = i + 1
        else:
            plist = pending_fills.get(symbol)
            if plist:
                _advance_ptr_to(symbol, plist[0][0])
            else:
                ptr[symbol] = m5_len[symbol]   # tidak ada event pending lagi -> selesai

        if ptr[symbol] < m5_len[symbol]:
            heapq.heappush(heap, (int(m5_ts_arr[symbol][ptr[symbol]]), symbol))

    blocked_by_slot = nonlocal_blocks['slot']
    blocked_by_margin = nonlocal_blocks['margin']
    blocked_by_min_order = nonlocal_blocks['min_order']
    blocked_by_invalid_sl = nonlocal_blocks['invalid_sl']

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
        _log_msg(f"🚀 Mulai backtest SNR (Support & Resistance + monitoring M5) — {len(SYMBOLS)} koin, {BACKTEST_START_DATE} s/d {BACKTEST_END_DATE}")
        _log_msg(f"   Syarat: level awal c1-c2 (candle sebelum c1 & sesudah c2 tidak melebihi level, tanpa EMA) -> monitoring M5: body M5 KELUAR level harus FVG (bukan FVG -> gugur) lalu body M5 MASUK harus menutup seluruh FVG (tidak menutup -> gugur) -> pasang LIMIT di angka level (batal kalau tak terisi {LIMIT_EXPIRE_HOURS:g} jam)  "
                  f"(gugur kalau setelah keluar harga {LEVEL_FAIL_PCT*100:.1f}% dari level)  "
                  f"SL = ujung wick terpanjang c1/c2  "
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
            _log_msg(f"   ✅ {symbol}: {cp['n']} candle H1, {st['levels']} level, {st['entry']} limit terisi ({kind_str or '-'}), "
                      f"{st['nofvg']} gugur tanpa FVG, {st['partial']} gugur break-in tak menutup FVG, {st['failed']} gugur (>{LEVEL_FAIL_PCT*100:.0f}%), {st['expired']} limit kadaluarsa ({LIMIT_EXPIRE_HOURS:g} jam), "
                      f"{st['pending']} belum terisi (data habis), {st['no_sl']} tanpa SL valid.")

        _log_msg(f"🧮 Menjalankan simulasi gabungan ({len(coins)} koin)...")
        result = run_combined_backtest(coins, m5_data)
        failed_total = sum(cp['stats']['failed'] for cp in coins.values())
        expired_total = sum(cp['stats']['expired'] for cp in coins.values())
        nofvg_total = sum(cp['stats']['nofvg'] for cp in coins.values())
        partial_total = sum(cp['stats']['partial'] for cp in coins.values())

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
                'failed_count': failed_total,
                'expired_count': expired_total,
                'nofvg_count': nofvg_total,
                'partial_count': partial_total,
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
    💡 <b>Support & Resistance awal + monitoring M5</b>, basis body candle:
    <br>• Level terbentuk dari c1+c2 (2 candle berlawanan arah, H1), <b>tanpa EMA</b>. Candle
    <b>sebelum c1</b> dan candle <b>sesudah c2</b> tidak boleh melebihi level (Support: low-nya di
    atas level; Resistance: high-nya di bawah level; sama dengan level juga tidak lolos).
    <br>• <b>Monitoring M5</b> (setelah level siap): body M5 PERTAMA yang menembus level
    <b>KELUAR</b> (Support: close di bawah level; Resistance: close di atas) harus sebuah <b>FVG</b>
    yang melingkupi level (Support: low candle sebelumnya di atas level &amp; high candle sesudahnya
    di bawah level; Resistance: kebalikannya). Kalau bukan FVG → setup langsung <b>gugur</b>, tidak
    dipantau lagi. Lalu candle M5 <b>PERTAMA</b> yang close-nya kembali ke sisi level harus <b>MASUK</b> dengan
    <b>menutup seluruh FVG</b> (Support: close di atas low candle sebelum break-out; Resistance: close di bawah
    high candle sebelum break-out; harus melebihi, sama tidak cukup). Kalau candle pertama itu hanya close
    melewati level tanpa menutup FVG → setup langsung <b>gugur</b>. Wick tidak dihitung.
    <br>• <b>GUGUR</b>: setelah KELUAR, kalau harga sampai <b>{LEVEL_FAIL_PCT*100:.1f}%</b> dari level
    (Support: low ≤ level −{LEVEL_FAIL_PCT*100:.1f}%; Resistance: high ≥ level +{LEVEL_FAIL_PCT*100:.1f}%)
    sebelum MASUK → level gugur. Level yang belum pernah KELUAR tetap dipantau.
    <br>• <b>ENTRY</b>: begitu body M5 MASUK → pasang <b>LIMIT</b> di <b>angka level</b> yang tadi
    ditembus (Support → Long, Resistance → Short). Limit terisi saat harga M5 retest level
    (wick cukup). Kalau tidak terisi dalam <b>{LIMIT_EXPIRE_HOURS:g} jam</b> sejak candle MASUK closed →
    limit batal, setup gugur. Tiap level HANYA dipakai 1x.
    <br>• <b>SL</b> = <b>ujung wick terpanjang</b> antara c1 dan c2 (Support: low wick bawah,
    Resistance: high wick atas), bukan persen tetap. Jarak entry–SL = 1R untuk ukuran posisi.
    <b>Trailing stop</b>: aktif begitu profit capai <b>{TRAIL_ACTIVATE_R:.1f}R</b>, lalu SL mengikuti
    <b>{TRAIL_STOP_R:.1f}R</b> di belakang harga tertinggi/terendah yang pernah dicapai (dipantau M5).
    Kalau SL dan trailing tersentuh di candle M5 yang sama, yang dihitung <b>SL</b>.
    <br>⚙️ Risk {RISK_PCT*100:.0f}% dari balance (compounding). Slot maksimum: {_fmt_max_concurrent()}.
    Sinyal terblokir — slot: {cr.get('blocked_by_slot',0)}, margin: {cr.get('blocked_by_margin',0)},
    min order: {cr.get('blocked_by_min_order',0)}. Gugur karena break-out bukan FVG: {cr.get('nofvg_count',0)}. Gugur karena break-in tidak menutup FVG: {cr.get('partial_count',0)}. Level gugur (M5 keluar lalu
    {LEVEL_FAIL_PCT*100:.1f}% dari level): {cr.get('failed_count',0)}. Limit kadaluarsa
    (tak terisi {LIMIT_EXPIRE_HOURS:g} jam): {cr.get('expired_count',0)}.
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


CSV_COLUMNS = ['Koin', 'Dir', 'Hasil', 'Level', 'Terbentuk', 'Break out', 'Break in', 'Entry', 'Entry time']


def _trades_csv() -> bytes:
    """trades.csv ringkas, waktu dalam WIB (UTC+7). Break out / Break in = waktu
    OPEN candle M5 yang body-nya menembus level; Entry = angka level (limit);
    Entry time = waktu OPEN candle M5 tempat limit terisi."""
    with _lock:
        trades_cp = list(_all_trades)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for t in trades_cp:
        writer.writerow([
            t['symbol'], t['direction'].upper(), _hasil_str(t), _fmt_price(t['level']),
            _fmt_wib_short(t['level_formed_ts']), _fmt_wib_short(t['out_ts']),
            _fmt_wib_short(t['in_ts']), _fmt_price(t['entry']), _fmt_wib_short(t['entry_ts']),
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
