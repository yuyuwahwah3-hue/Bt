"""
backtest_web.py — DEFAULT: STRATEGY=multi_acuan  (S&R H1 berFVG -> break-out/break-in -> BOS -> multi-acuan -> MSS M5)
Strategi lama (EMA21/30 + IFVG limit + target level lawan) tetap ada: set env STRATEGY=ifvg_ema.
Aturan lengkap strategi baru ada di komentar blok "STRATEGI BARU" (cari 'STRATEGI BARU' di file ini).
Env strategi baru: NS_SWING_EXT=5, NS_LOOKBACK=20, NS_MSS_SWING=1, NS_SL_BUF=0.002, NS_RANGE_MODE=melebar|tetap, NS_ACUAN_MODE=swing_ditembus|swing_antara|level, NS_ENTRY_MODE=limit_fvg|market, NS_FVG_EDGE=near|mid|far, NS_MAX_ENTRY=5 (0 = tak terbatas).
Env umum: RISK_PCT, INITIAL_BALANCE, FEE_ENTRY_PCT, FEE_EXIT_PCT, LEVERAGE, MAX_CONCURRENT, BACKTEST_START_DATE/END_DATE.
Endpoint: /  (dashboard) | /trades.csv | /setups.txt (format txt per entry: Coin, Dir, Hasil, Level, Tanggal level, Acuan, P, P tanggal, Swing, Swing tanggal, Mss tanggal) | /logs
=======================================================================================================================
(Dokumentasi STRATEGI LAMA, hanya berlaku kalau STRATEGY=ifvg_ema)
backtest_web.py — Backtest S&R H1 + FVG H1 + bias EMA21/30 + konfirmasi Inverted FVG (M5), exit TARGET level lawan
===================================================================================================================
RINGKASAN STRATEGI (Long; Short = cermin)
1) LEVEL H1 (basis body): c1 bearish, c2 bullish, level = close[c1]; c3 tidak boleh menyentuh level (low[c3] > level).
   Tanpa EMA cross, tanpa TEST1/TEST2, tanpa RSI.
2) FVG H1 searah di C1..C4 (env FVG_LAST_C): 3 candle berurutan (a,b,k=a+2; k <= c1+3) dgn gap low[k] > high[a] dan close[k] > high[b]
   (arah candle bebas). Level AKTIF saat candle k closed. Level gugur kalau sejak c4 s/d candle k harga menyentuh level.
3) Filter level-berulang: kalau di antara c3 s/d candle penutup FVG muncul lagi pasangan 2-candle searah (bearish->bullish
   utk Long) -> level awal tidak valid.
4) Filter bias EMA21 vs EMA30 (H1, hanya level awal; target exit TIDAK difilter): Long valid kalau EMA21 > EMA30, Short kalau
   EMA21 < EMA30. Level dgn candle FVG ke-k < EMA_WARMUP (90) dibuang (EMA belum stabil di awal data).
5) Level searah baru menggantikan level lama (tanpa expired).
6) KONFIRMASI M5 (Inverted FVG): setelah level aktif -> tunggu SENTUH (low M5 <= level) -> FVG M5 searah (x1,x2,x3; x2
   bullish; low[x3] > high[x1]) yang 'mengambil' gap FVG M5 tidak searah (y1,y2,y3; y2 bearish; high[y3] < low[y1]) yg
   selesai sebelum x1 (mundur maks IFVG_LOOKBACK=48 candle dari candle sentuh; bagian overlap belum tersentuh). IFVG =
   overlap; ENTRY LIMIT di tepi atas overlap. SL = low terdalam sejak candle sentuh s/d x3 (tanpa buffer). IFVG baru
   sebelum fill -> limit dipindah. Fill = candle M5 pertama dgn low <= limit, harga fill = min(limit, open).
7) EXIT: tanpa trailing. TARGET = ujung wick level LAWAN terbaru (H1 + FVG H1, tanpa EMA) yang belum tersentuh dan
   berada di atas entry (Long: resistance, wick = high tertinggi c1/c2; Short: support, wick = low terendah). Target
   berubah dinamis saat level lawan baru terbentuk / target lama tersentuh. SL dicek duluan dalam candle yang sama;
   TP tidak dicek di candle fill. Posisi yg belum selesai di akhir data tidak dihitung.
Fee: masuk FEE_ENTRY_PCT (limit), keluar FEE_EXIT_PCT. Risk RISK_PCT dari balance (compound), 1 posisi per arah per koin.

Deploy ke Railway: Start command -> python backtest_web.py ; buka domain Railway untuk lihat progress & hasil.
Periode default: 1 tahun ke belakang (override: BACKTEST_START_DATE / BACKTEST_END_DATE).
Env tambahan: FVG_LAST_C=4, EMA_BIAS_FILTER=1/0, BIAS_EMA_FAST=21, BIAS_EMA_SLOW=30, EMA_WARMUP=90, IFVG_LOOKBACK=48.
trades.csv (WIB): Koin, Dir, Hasil ('TP +x.xR' / 'SL'), Level, Terbentuk, Touch, IFVG, Limit, SL, Fill time, Exit, Exit time.
"""
import os, threading, time, io, csv
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import bisect
import numpy as np
import pandas as pd
from pybit.unified_trading import HTTP

# ============================================================
# CONFIG (override via environment variable kalau perlu)
# ============================================================
PORT             = int(os.environ.get('PORT', 8080))
INITIAL_BALANCE  = float(os.environ.get('INITIAL_BALANCE', '1000.0'))   # modal awal, 1 akun bersama
RISK_PCT         = float(os.environ.get('RISK_PCT', '0.01'))          # risk per trade dari balance (compound)
FEE_ENTRY_PCT    = float(os.environ.get('FEE_ENTRY_PCT', '0.0006'))
FEE_EXIT_PCT     = float(os.environ.get('FEE_EXIT_PCT', str(0.0006 * 3)))

EMA_BIAS_FILTER  = os.environ.get('EMA_BIAS_FILTER', '1').strip() not in ('0', 'false', 'False', '')
BIAS_EMA_FAST    = int(os.environ.get('BIAS_EMA_FAST', 21))
BIAS_EMA_SLOW    = int(os.environ.get('BIAS_EMA_SLOW', 30))
EMA_WARMUP       = int(os.environ.get('EMA_WARMUP', 90))             # level dgn candle FVG ke-k < ini dibuang
FVG_LAST_C       = int(os.environ.get('FVG_LAST_C', 4))              # FVG H1 dicari di C1..C{FVG_LAST_C}
IFVG_LOOKBACK    = int(os.environ.get('IFVG_LOOKBACK', 48))          # FVG tidak searah maks N candle M5 sebelum sentuh

# --- STRATEGI: 'multi_acuan' (baru, default) | 'ifvg_ema' (strategi lama EMA21/30 + IFVG limit + target level lawan) ---
STRATEGY         = os.environ.get('STRATEGY', 'multi_acuan').strip().lower()
NS_SWING_EXT     = int(os.environ.get('NS_SWING_EXT', 5))            # titik terjauh harus swing: N candle H1 kiri & kanan tidak melebihi
NS_LOOKBACK      = int(os.environ.get('NS_LOOKBACK', 20))            # FVG lama boleh dari N candle M5 sebelum acuan tersentuh
NS_MSS_SWING     = int(os.environ.get('NS_MSS_SWING', 1))            # swing fractal M5 utk MSS: N candle kiri & kanan (default 1)
NS_SW_EQUAL      = os.environ.get('NS_SW_EQUAL', 'merge').strip().lower()   # swing fractal M5, angka sama pada candle berurutan: 'merge' (default, dijadikan SATU KESATUAN: candle sebelum & sesudah kesatuan tidak boleh melebihi/sama) | 'invalid' (sama = bukan swing) | 'valid' (sama boleh, cara lama)
NS_H1_EQUAL      = os.environ.get('NS_H1_EQUAL', 'merge').strip().lower()   # P & swing H1, angka sama pada candle berurutan: 'merge' (default, satu kesatuan) | 'invalid' (sama = tidak valid)
NS_MSS_REF       = os.environ.get('NS_MSS_REF', 'range').strip().lower()   # 'range' (default): swing = tertinggi/terendah antara P lama & P baru, dihitung ulang tiap P bergeser | 'range_max' (swing tak pernah turun) | 'fractal' (lama)
NS_P_EQUAL       = os.environ.get('NS_P_EQUAL', 'merge').strip().lower()   # P M5, angka sama pada candle berurutan: 'merge' (default, candle yg sama dijadikan SATU KESATUAN: candle sebelum & sesudah kesatuan tidak boleh melebihi/sama, candle sama berikutnya menyambung) | 'invalid' (sama dgn tetangga = P tidak valid) | 'valid' (sama boleh)
NS_ENTRY_MODE    = os.environ.get('NS_ENTRY_MODE', 'limit_fvg').strip().lower()   # 'limit_fvg' (default): limit di FVG terdekat dgn MSS | 'market': entry market di close candle MSS (cara lama)
NS_FVG_EDGE      = os.environ.get('NS_FVG_EDGE', 'near').strip().lower()     # titik limit di FVG: 'near' (tepi terdekat ke harga sekarang, default) | 'mid' | 'far'
NS_MAX_ENTRY     = int(os.environ.get('NS_MAX_ENTRY', 5))           # maksimal entry per BOS (setelah SL boleh entry lagi di acuan lain, sampai batas ini); 0 = tak terbatas
NS_ACUAN_MODE    = os.environ.get('NS_ACUAN_MODE', 'swing_ditembus').strip().lower()   # acuan H1: 'swing_ditembus' (default: Long: P = LOW valid, swing = HIGH; Short: P = HIGH, swing = LOW; titik acuan = swing, aktif setelah close H1 menembusnya) | 'swing_antara' (peran dibalik: Long: P = HIGH, swing = LOW; aktif saat P valid confirmed) | 'level' (cara lama: level baru berFVG)
NS_H1_P_EXCEED   = os.environ.get('NS_H1_P_EXCEED', '1').strip() not in ('0', 'false', 'False', '')   # P H1 baru HARUS melebihi P valid sebelumnya (lebih ekstrem, sama persis tidak); P pertama tidak punya swing. 0 = semua ekstrem valid dianggap P (cara sebelumnya)
NS_SL_BUF        = float(os.environ.get('NS_SL_BUF', '0.00'))       # buffer SL di luar ujung FVG (0.002 = 0.2%)
NS_RANGE_MELEBAR = os.environ.get('NS_RANGE_MODE', 'melebar').strip().lower() != 'tetap'   # range acuan melebar mengikuti titik terjauh berjalan

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
    return (d - timedelta(days=655)).strftime('%Y-%m-%d')

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
_setups_all = []      # info per setup (strategi baru) untuk /setups.txt
_ns_stats = {}        # total statistik strategi baru (semua koin)
_monthly_results = []   # list dict: {month, start_balance, end_balance, profit_usd, growth_pct, n_trades, wr}
_avg_monthly_growth = 0.0   # rata-rata growth_pct antar bulan yg ada trade
_combined_result = {
    'n_trades': 0, 'n_win': 0, 'n_loss': 0, 'wr': 0, 'total_pnl': 0, 'roi': 0,
    'total_r': 0, 'avg_r': 0, 'final_balance': INITIAL_BALANCE,
    'blocked_by_slot': 0, 'blocked_by_margin': 0, 'blocked_by_min_order': 0,
    'blocked_by_invalid_sl': 0, 'blocked_by_dup': 0,
    'levels_count': 0, 'filled_count': 0, 'aktif_count': 0, 'tanpa_fvg_count': 0, 'disentuh_count': 0,
    'ulang_count': 0, 'warmup_count': 0, 'ema_count': 0, 'no_touch_count': 0, 'no_ifvg_count': 0,
    'no_fill_count': 0, 'sl_invalid_count': 0,
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


# ============================================================
# DETEKSI STRATEGI (H1: level + FVG + bias EMA | M5: Inverted FVG)
# ============================================================

N_RIGHT  = 1       # candle kanan (c3) yang wajib bersih (wick tidak boleh menyentuh level)
WICK_EPS = 1e-9
H1_MS    = 3600 * 1000
M5_MS    = 5 * 60 * 1000
EPS      = 1e-12


def raw_levels(o, h, l, c):
    """Level mentah H1 (basis body): Support = c1 bearish lalu c2 bullish; Resistance = kebalikannya.
    Level = close c1. c3 (kanan) tidak boleh menyentuh level (wick pun tidak). TANPA EMA cross."""
    out = []
    for i in range(0, len(c) - 4):
        if c[i] < o[i] and c[i + 1] > o[i + 1] and l[i + 2] > c[i] + WICK_EPS:
            out.append({'ty': 'support', 'd': 'Long', 'L': float(c[i]), 'c1': i})
        if c[i] > o[i] and c[i + 1] < o[i + 1] and h[i + 2] < c[i] - WICK_EPS:
            out.append({'ty': 'resistance', 'd': 'Short', 'L': float(c[i]), 'c1': i})
    return out


def activate_level(lv, H1, st):
    """Level jadi AKTIF saat FVG H1 searah selesai (candle ke-3 FVG closed).
    FVG H1 di antara C1..C{FVG_LAST_C} (default C1..C4; 3 candle berurutan a,b,k; k <= c1+FVG_LAST_C-1): gap saja, arah candle bebas.
      Long : low(k) > high(a) DAN close(k) > high(b)     Short: high(k) < low(a) DAN close(k) < low(b)
    Level gugur kalau sejak c4 s/d candle penutup FVG harga sudah menyentuh level."""
    o, h, l, c, ts = H1['o'], H1['h'], H1['l'], H1['c'], H1['ts']
    n = len(c); i = lv['c1']; d = lv['d']; L = lv['L']; third = None
    for a_ in range(i, i + FVG_LAST_C - 2):
        k = a_ + 2
        if k > i + FVG_LAST_C - 1 or k >= n:
            continue
        b_ = a_ + 1
        if d == 'Long':
            ok = l[k] > h[a_] and c[k] > h[b_]
        else:
            ok = h[k] < l[a_] and c[k] < l[b_]
        if ok:
            third = (a_, k)
            break
    if third is None:
        st['tanpa_fvg'] += 1
        return None
    k = third[1]
    seg = slice(i + 3, k + 1)
    if (d == 'Long' and (l[seg] <= L + WICK_EPS).any()) or (d == 'Short' and (h[seg] >= L - WICK_EPS).any()):
        st['sudah_disentuh'] += 1
        return None
    x = dict(lv)
    x.update(fvg=third, act_ts=int(ts[k] + H1_MS))
    return x


def level_repeated(x, H1):
    """Level (2 candle berlawanan: bearish+bullish utk Long, kebalikannya utk Short) muncul LAGI di antara c3 s/d
    candle penutup FVG -> level awal tidak valid."""
    o, c = H1['o'], H1['c']
    i0 = x['c1']; kk = x['fvg'][1]
    for j in range(i0 + 2, kk):
        if x['d'] == 'Long' and c[j] < o[j] and c[j + 1] > o[j + 1]:
            return True
        if x['d'] == 'Short' and c[j] > o[j] and c[j + 1] < o[j + 1]:
            return True
    return False


def _mirror(M, Lv, d, a, e):
    """Cerminkan M5 ke 'Long' relatif terhadap level (Short dibalik). Slice [a:e]."""
    sl = slice(a, e)
    if d == 'Long':
        return ((M['O'][sl] - Lv) / Lv, (M['H'][sl] - Lv) / Lv, (M['L'][sl] - Lv) / Lv, (M['C'][sl] - Lv) / Lv)
    return (-(M['O'][sl] - Lv) / Lv, -(M['L'][sl] - Lv) / Lv, -(M['H'][sl] - Lv) / Lv, -(M['C'][sl] - Lv) / Lv)


def _price(d, Lv, dev):
    return Lv * (1 + dev) if d == 'Long' else Lv * (1 - dev)


def scan_ifvg(lv, e, M):
    """Monitoring M5 setelah level aktif sampai indeks e (aktivasi level searah berikutnya / data habis).
    Long (Short = cermin):
     1) SENTUH: candle M5 pertama dgn low <= level.
     2) FVG SEARAH (bullish) x1,x2,x3 pada/ setelah sentuh: candle TENGAH x2 bullish, gap low(x3) > high(x1)
        -> zona searah [high(x1), low(x3)].
     3) FVG TIDAK SEARAH (bearish) y1,y2,y3: candle TENGAH y2 bearish, gap high(y3) < low(y1) -> zona [high(y3), low(y1)].
        Selesai (y3) SEBELUM x1; boleh sebelum/sesudah sentuh (mundur maks IFVG_LOOKBACK candle dari candle sentuh,
        tidak sebelum level aktif). Bagian overlap belum tersentuh sebelum x1 (high candle di antara <= dasar overlap).
     4) FVG searah 'mengambil' gap tidak searah = zona saling overlap. IFVG = overlap; ENTRY limit di tepi ATAS
        overlap = min(atas zona tidak searah, atas zona searah) (Short: tepi bawah). Dipasang saat x3 closed.
     5) SL = kedalaman terjauh sejak sentuh s/d x3 (low terendah / high tertinggi), tanpa tambahan.
     6) IFVG baru sebelum limit lama terisi -> limit pindah ke IFVG baru. Tanpa expired.
    Return (status, info). status: 'fill' | 'no_touch' | 'no_ifvg' | 'no_fill' | 'sl_invalid'."""
    TS = M['TS']; d, Lv = lv['d'], lv['L']
    a = int(np.searchsorted(TS, lv['act_ts'], side='left'))
    if a >= e:
        return 'no_touch', None
    o_, h_, l_, c_ = _mirror(M, Lv, d, a, e)
    m = len(o_)
    tch = np.flatnonzero(l_ <= EPS)
    if len(tch) == 0:
        return 'no_touch', None
    k = int(tch[0])
    qs = np.arange(k + 2, m)
    qs = qs[(c_[qs - 1] > o_[qs - 1]) & (l_[qs] > h_[qs - 2])]          # kandidat FVG searah (x3 = q)
    bear_ok = np.zeros(m, dtype=bool)
    if m > 2:
        bear_ok[2:] = (c_[1:m - 1] < o_[1:m - 1]) & (h_[2:] < l_[:m - 2])   # kandidat FVG tidak searah (y3 = index)
    bear_idx = np.flatnonzero(bear_ok)
    lo_lim = max(0, k - IFVG_LOOKBACK)
    pend = None                                                         # IFVG yang limit-nya sedang menunggu

    def _fill_in(f, last_idx):
        """Cari candle pertama di (f.q+1 .. last_idx) yang low <= entry. Return rel idx atau None."""
        s0 = f['q'] + 1
        if s0 > last_idx:
            return None
        hit = l_[s0:last_idx + 1] <= f['ent'] + EPS
        return s0 + int(hit.argmax()) if hit.any() else None

    def _result(f, r):
        fp = min(f['ent'], float(o_[r]))
        if fp <= f['sl']:
            return 'sl_invalid', None
        z_lo = max(f['bb'], f['ub'])
        return 'fill', dict(touch=a + k, fill=a + r, x1=a + f['x1'], x3=a + f['q'], y1=a + f['y1'], y3=a + f['y3'],
                            n_ifvg=f['n'], entry=_price(d, Lv, fp), sl=_price(d, Lv, f['sl']),
                            ent_lvl=_price(d, Lv, f['ent']), z_lo=_price(d, Lv, z_lo),
                            bear=(_price(d, Lv, f['bb']), _price(d, Lv, f['bt'])),
                            bull=(_price(d, Lv, f['ub']), _price(d, Lv, f['ut'])))

    n_ifvg = 0
    for q in qs:
        q = int(q); x1 = q - 2
        if x1 < k:
            continue
        ub, ut = float(h_[x1]), float(l_[q])
        lo2 = lo_lim + 2
        if x1 - 1 < lo2:
            continue
        sel = bear_idx[(bear_idx >= lo2) & (bear_idx <= x1 - 1)]
        if len(sel) == 0:
            continue
        bb = h_[sel]; bt = l_[sel - 2]
        ok = (ub < bt) & (ut > bb)
        if not ok.any():
            continue
        seg_h = h_[lo2:x1]
        rm = np.maximum.accumulate(seg_h[::-1])[::-1]                   # rm[j-lo2] = max h_[j:x1]
        mx = np.full(len(sel), -np.inf)
        mm = sel + 1 < x1
        mx[mm] = rm[sel[mm] + 1 - lo2]
        ok &= (mx <= np.maximum(bb, ub) + EPS)
        if not ok.any():
            continue
        j = int(np.flatnonzero(ok)[-1])                                 # FVG tidak searah terbaru yg valid
        y3 = int(sel[j]); y1 = y3 - 2
        n_ifvg += 1
        f = dict(q=q, x1=x1, y1=y1, y3=y3, ent=min(float(bt[j]), ut), sl=float(l_[k:q + 1].min()),
                 bb=float(bb[j]), bt=float(bt[j]), ub=ub, ut=ut, n=n_ifvg)
        if pend is not None:                                            # limit lama masih hidup sampai candle q (inklusif)
            r = _fill_in(pend, q)
            if r is not None:
                return _result(pend, r)
        pend = f
    if pend is None:
        return 'no_ifvg', None
    r = _fill_in(pend, m - 1)
    if r is not None:
        return _result(pend, r)
    return 'no_fill', None


def opposite_levels(H1, M, d, raw, st0):
    """Level LAWAN arah utk TARGET exit = level H1 + FVG H1 (valid sama dgn level awal), TANPA filter EMA.
    Long -> RESISTANCE; Short -> SUPPORT. Aktif saat candle penutup FVG closed.
    Wick target: Long -> high tertinggi c1/c2 resistance; Short -> low terendah c1/c2 support.
    return list (act_ts, first_touch_ts, level, wick_target, c1_ts) urut waktu; first_touch_ts = open ts candle M5 pertama
    (sejak aktif) yang mencapai ujung wick (10**18 kalau belum pernah)."""
    want = 'Short' if d == 'Long' else 'Long'
    TS, HH, LL = M['TS'], M['H'], M['L']
    h, l, ts = H1['h'], H1['l'], H1['ts']
    out = []
    for lv in raw:
        if lv['d'] != want:
            continue
        x = activate_level(lv, H1, st0)
        if x is None or level_repeated(x, H1):
            continue
        i = x['c1']
        T = float(max(h[i], h[i + 1])) if want == 'Short' else float(min(l[i], l[i + 1]))
        kn = int(np.searchsorted(TS, x['act_ts'], side='left'))
        if kn >= len(TS):
            continue
        hit = (HH[kn:] >= T - EPS) if d == 'Long' else (LL[kn:] <= T + EPS)
        ft = int(TS[kn + int(hit.argmax())]) if hit.any() else 10 ** 18
        out.append((int(x['act_ts']), ft, float(x['L']), T, int(ts[i])))
    out.sort()
    return out


def detect_snr_events(df, m5):
    """Level H1 (c1/c2 + c3 bersih) + FVG H1 C1..C5 + level tidak diulang sebelum FVG + bias EMA21/EMA30
    -> monitoring M5 (sentuh -> Inverted FVG) -> LIMIT di IFVG, SL kedalaman terjauh sejak sentuh.
    Exit: TARGET = ujung wick level lawan (H1 + FVG) terbaru yg belum tercapai (lihat run_combined_backtest).
    Return (events, stats). events urut fill_ts, dict:
    {'kind','type','level','direction','entry_price','sl_price','fill_ts','test1_ts','test2_ts','c1_ts','targets','meta'}"""
    stats = {'levels': 0, 'tanpa_fvg': 0, 'sudah_disentuh': 0, 'ulang': 0, 'warmup': 0, 'ema_ditolak': 0, 'aktif': 0,
             'no_touch': 0, 'no_ifvg': 0, 'no_fill': 0, 'sl_invalid': 0, 'entry': 0}
    events = []
    if m5 is None or df is None or len(df) < 10:
        return events, stats
    H1 = {k: df[v].values.astype(float) for k, v in (('o', 'open'), ('h', 'high'), ('l', 'low'), ('c', 'close'))}
    H1['ts'] = df['ts'].values.astype(np.int64)
    raw = raw_levels(H1['o'], H1['h'], H1['l'], H1['c'])
    stats['levels'] = len(raw)
    acts = [x for x in (activate_level(lv, H1, stats) for lv in raw) if x is not None]
    keep = []
    for x in acts:
        if level_repeated(x, H1):
            stats['ulang'] += 1
        else:
            keep.append(x)
    acts = keep
    if EMA_BIAS_FILTER:
        cs = pd.Series(H1['c'])
        ea = cs.ewm(span=BIAS_EMA_FAST, adjust=False).mean().values
        eb = cs.ewm(span=BIAS_EMA_SLOW, adjust=False).mean().values
        keep = []
        for x in acts:
            kk = x['fvg'][1]
            if kk < EMA_WARMUP:
                stats['warmup'] += 1
                continue
            if (x['d'] == 'Long') == bool(ea[kk] > eb[kk]):
                keep.append(x)
            else:
                stats['ema_ditolak'] += 1
        acts = keep
    stats['aktif'] = len(acts)

    st0 = {'tanpa_fvg': 0, 'sudah_disentuh': 0}
    OPP = {d: opposite_levels(H1, m5, d, raw, st0) for d in ('Long', 'Short')}
    TS = m5['TS']
    for d in ('Long', 'Short'):
        lst = sorted([x for x in acts if x['d'] == d], key=lambda x: x['act_ts'])
        for j, lv in enumerate(lst):
            e = int(np.searchsorted(TS, lst[j + 1]['act_ts'], side='left')) if j + 1 < len(lst) else len(TS)
            status, inf = scan_ifvg(lv, e, m5)
            if status != 'fill':
                stats[status] += 1
                continue
            stats['entry'] += 1
            f = inf['fill']; fv = lv['fvg']
            events.append({
                'kind': 'SNR_SUPPORT' if d == 'Long' else 'SNR_RESISTANCE', 'type': lv['ty'], 'level': lv['L'],
                'direction': d, 'entry_price': inf['entry'], 'sl_price': inf['sl'], 'fill_ts': int(TS[f]),
                'test1_ts': int(TS[inf['touch']]), 'test2_ts': int(TS[inf['x3']]), 'c1_ts': int(H1['ts'][lv['c1']]),
                'targets': OPP[d],
                'meta': {
                    'fvg_h1': (int(H1['ts'][fv[0]]), int(H1['ts'][fv[1]])), 'level_aktif': int(lv['act_ts']),
                    'touch_ts': int(TS[inf['touch']]), 'y1_ts': int(TS[inf['y1']]), 'y3_ts': int(TS[inf['y3']]),
                    'x1_ts': int(TS[inf['x1']]), 'x3_ts': int(TS[inf['x3']]), 'n_ifvg': inf['n_ifvg'],
                    'bear': inf['bear'], 'bull': inf['bull'], 'ifvg_entry': inf['ent_lvl'], 'ifvg_low': inf['z_lo'],
                },
            })
    events.sort(key=lambda e: e['fill_ts'])
    return events, stats


def detect_all_events(df, m5):
    """Dedup: (kind, level, fill_ts) sama persis = 1 sinyal."""
    events, stats = detect_snr_events(df, m5)
    seen = set(); deduped = []
    for e in events:
        key = (e['kind'], round(e['level'], 10), e['fill_ts'])
        if key in seen:
            continue
        seen.add(key); deduped.append(e)
    deduped.sort(key=lambda e: e['fill_ts'])
    return deduped, stats


# ============================================================
# STRATEGI BARU (STRATEGY=multi_acuan): S&R H1 berFVG -> break-out/break-in -> BOS -> multi-acuan -> IFVG M5
# ============================================================
#
# RINGKASAN (Long; Short = cermin)
# 1) LEVEL H1: c1 bearish + c2 bullish. Level = ujung wick TERENDAH antara c1 dan c2. c3 dan c4 tidak boleh menembus level
#    (low > level) dan harus ada FVG bullish di c1-c3 (low c3 > high c1) atau c2-c4 (low c4 > high c2). Short = cermin
#    (ujung wick TERTINGGI c1/c2).
# 2) BREAK-OUT = candle H1 pertama setelah c4 yang CLOSE menembus level keluar. Kalau sebuah candle hanya menyentuh level
#    dengan wick, candle berikutnya WAJIB break (close), kalau tidak setup batal. BREAK-IN = candle H1 pertama setelah break-out
#    yang CLOSE kembali ke dalam level (aturan wick yang sama).
# 3) TITIK TERJAUH = high tertinggi (Long) / low terendah (Short) antara c1 sampai sebelum break-out. Harus swing: NS_SWING_EXT (5)
#    candle H1 di kiri dan kanan tidak boleh melebihinya (sama persis boleh).
# 4) FVG H1 (searah arah trade) harus ada di 1 candle sebelum s/d 2 candle sesudah break-in; kalau tidak ada, cari FVG
#    (arah break-out) di 2 candle sebelum s/d 1 candle sesudah break-out. Tidak ada -> setup dibuang.
# 5) BOS = candle H1 pertama setelah break-in yang CLOSE melewati titik terjauh. Sebelum BOS: close H1 menembus level lagi
#    (breakout ulang) atau sweep wick titik terjauh lalu menyentuh level lagi -> setup batal.
# 6) ACUAN = semua level baru berFVG sejenis (definisi level no.1) yang terbentuk setelah break-in dan berada di range harga
#    antara level yang dibreak dan titik terjauh (NS_RANGE_MODE=melebar: batas ikut melebar mengikuti titik terjauh berjalan
#    sampai level baru itu selesai; =tetap: batas = titik terjauh saat BOS). Acuan aktif setelah candle ke-4 level barunya closed.
#    SEMUA acuan dipakai (tidak perlu di-break). Acuan aktif setelah candle ke-4 level barunya closed.
# 7) MONITORING M5 (tanpa expired): acuan TERSENTUH (wick boleh) -> titik BOS (high tertinggi / low terendah sejak BOS) DIKUNCI.
#    Lalu tunggu MSS (Market Structure Shift, SMC) di M5: Long = setelah low terendah sejak sentuh (P), candle M5 pertama yang CLOSE di atas
#    swing high terakhir sebelum P (fractal NS_MSS_SWING=2 kiri-kanan, boleh dari NS_LOOKBACK=20 candle sebelum sentuh). Short = kebalikannya.
#    Low/high baru memindahkan P. ENTRY: NS_ENTRY_MODE=limit_fvg (default) limit di FVG terdekat MSS | market = close candle MSS. SL = ujung MSS (low terendah P / high tertinggi P) +NS_SL_BUF. TP = titik BOS terkunci.
# 8) Setelah SL tunggu acuan lain yang belum tersentuh. Setup SELESAI kalau TP; BOS USAI kalau setelah acuan tersentuh harga kembali ke titik
#    BOS terkunci sebelum MSS/entry; setup MATI kalau (setelah BOS) close H1 menembus titik terjauh antara break-out dan break-in LEVEL UTAMA
#    (Long: low terendah bo..bi; Short: high tertinggi bo..bi).
# Catatan: urutan trade dalam 1 setup dihitung dari harga saja (tanpa balance); simulasi gabungan di bawah tetap menerapkan
# slot/margin/min-order/1-posisi-per-arah, jadi entry yg terblokir cuma dilewati.

def ns_levels(H1):
    o, h, l, c = H1['o'], H1['h'], H1['l'], H1['c']
    out = []
    for i in range(0, len(c) - 4):
        if c[i] < o[i] and c[i + 1] > o[i + 1]:
            lvl = min(l[i], l[i + 1])
            if l[i + 2] > lvl and l[i + 3] > lvl and (l[i + 2] > h[i] or l[i + 3] > h[i + 1]):
                fzs = ([l[i + 2]] if l[i + 2] > h[i] else []) + ([l[i + 3]] if l[i + 3] > h[i + 1] else [])
                out.append({'ty': 'support', 'd': 'Long', 'L': float(lvl), 'c1': i, 'c4': i + 3, 'fz': float(max(fzs))})
        if c[i] > o[i] and c[i + 1] < o[i + 1]:
            lvl = max(h[i], h[i + 1])
            if h[i + 2] < lvl and h[i + 3] < lvl and (h[i + 2] < l[i] or h[i + 3] < l[i + 1]):
                fzs = ([h[i + 2]] if h[i + 2] < l[i] else []) + ([h[i + 3]] if h[i + 3] < l[i + 1] else [])
                out.append({'ty': 'resistance', 'd': 'Short', 'L': float(lvl), 'c1': i, 'c4': i + 3, 'fz': float(min(fzs))})
    return out


def _ns_first_break(start, n, tf_arr, bf_arr):
    """Aturan wick: candle pertama yang menyentuh level (tf). Kalau close-nya sudah break -> itu candle break.
    Kalau cuma wick, candle berikutnya wajib break; kalau tidak -> -1 (batal). None = tidak pernah menyentuh."""
    if start >= n:
        return None
    seg = tf_arr[start:]
    if not seg.any():
        return None
    k = start + int(seg.argmax())
    if bf_arr[k]:
        return k
    if k + 1 < n and bf_arr[k + 1]:
        return k + 1
    return -1


def ns_setups(H1, M5, LV, st):
    """Level -> break-out -> break-in -> (swing titik terjauh, FVG) -> BOS. Return list setup."""
    o, h, l, c, ts = H1['o'], H1['h'], H1['l'], H1['c'], H1['ts']
    n = len(c); ts5, hi5, lo5, n5 = M5['TS'], M5['H'], M5['L'], len(M5['TS'])
    SE = NS_SWING_EXT
    out = []

    def fvg(k, kind):
        if k < 0 or k + 2 >= n:
            return False
        return bool(l[k + 2] > h[k]) if kind == 'bull' else bool(h[k + 2] < l[k])

    for lv in LV:
        sup = lv['ty'] == 'support'; lvl = lv['L']; i = lv['c1']; r = lv['c4']
        if sup:
            tf_out, bf_out = (l <= lvl), (c < lvl)
            tf_in, bf_in = (h >= lvl), (c > lvl)
        else:
            tf_out, bf_out = (h >= lvl), (c > lvl)
            tf_in, bf_in = (l <= lvl), (c < lvl)
        bo = _ns_first_break(r + 1, n, tf_out, bf_out)
        if bo is None:
            continue
        if bo == -1:
            st['wick_batal_keluar'] += 1; continue
        bi = _ns_first_break(bo + 1, n, tf_in, bf_in)
        if bi is None:
            continue
        if bi == -1:
            st['wick_batal_masuk'] += 1; continue
        st['break_out_in'] += 1
        # titik terjauh harus swing SE kiri / SE kanan
        xr = i + int(np.argmax(h[i:bo])) if sup else i + int(np.argmin(l[i:bo]))
        if xr - SE < 0 or xr + SE >= n:
            st['terjauh_bukan_swing'] += 1; continue
        if sup:
            okx = bool((h[xr - SE:xr] <= h[xr]).all() and (h[xr + 1:xr + SE + 1] <= h[xr]).all())
        else:
            okx = bool((l[xr - SE:xr] >= l[xr]).all() and (l[xr + 1:xr + SE + 1] >= l[xr]).all())
        if not okx:
            st['terjauh_bukan_swing'] += 1; continue
        vf0 = int(ts[xr + SE]) + H1_MS
        # FVG di sekitar break-in (arah trade) / break-out (arah berlawanan)
        kin = 'bull' if sup else 'bear'; kout = 'bear' if sup else 'bull'
        if bi + 2 >= n:
            continue
        if fvg(bi - 1, kin):
            vf = int(ts[bi + 1]) + H1_MS
        elif fvg(bi, kin):
            vf = int(ts[bi + 2]) + H1_MS
        elif fvg(bo - 2, kout) or fvg(bo - 1, kout):
            vf = int(ts[bi + 2]) + H1_MS
        else:
            st['no_fvg'] += 1; continue
        ext = float(h[i:bo].max()) if sup else float(l[i:bo].min())
        # titik terjauh ANTARA break-out dan break-in (low terendah utk Long / high tertinggi utk Short), inklusif bo..bi.
        # Dipakai sebagai batas gugur setelah BOS: close H1 menembus titik ini -> setup mati.
        dz = float(l[bo:bi + 1].min()) if sup else float(h[bo:bi + 1].max())
        beyond = (lambda p: p > ext) if sup else (lambda p: p < ext)
        bos = None; cancelled = None; swept = False
        if beyond(c[bi]):
            bos = bi
        else:
            for k in range(bi + 1, n):
                a = int(np.searchsorted(ts5, ts[k]))
                for j in range(a, min(a + 12, n5)):
                    if swept and ((lo5[j] <= lvl) if sup else (hi5[j] >= lvl)):
                        cancelled = 'sweep'; break
                    if (hi5[j] > ext) if sup else (lo5[j] < ext):
                        swept = True
                if cancelled:
                    break
                if (c[k] < lvl) if sup else (c[k] > lvl):          # breakout ulang (close H1 menembus level lagi)
                    cancelled = 'rebreak'; break
                if beyond(c[k]):
                    bos = k; break
        if bos is None:
            st['batal_sebelum_bos' if cancelled else 'tanpa_bos'] += 1
            continue
        st['bos'] += 1
        out.append({'sup': sup, 'ty': lv['ty'], 'd': lv['d'], 'L': lvl, 'i': i, 'bo': bo, 'bi': bi, 'bos': bos,
                    'ext': ext, 'dz': dz, 'vf': max(vf, vf0)})
    return out


def _ns_overlap(pl, ph, pj, k, zl, zh, L5, H5):
    """Gap FVG lama [pl,ph] boleh menyusut oleh candle di antara FVG lama (pj) dan c1 FVG baru (k), asal tidak hilang seluruhnya.
    True kalau zona FVG baru [zl,zh] menimpa sisa gap."""
    iv = [(pl, ph)]
    for q in range(pj + 1, k):
        c_, d_ = L5[q], H5[q]; nv = []
        for a_, b_ in iv:
            if d_ <= a_ or c_ >= b_:
                nv.append((a_, b_))
            else:
                if c_ > a_:
                    nv.append((a_, c_))
                if d_ < b_:
                    nv.append((d_, b_))
        iv = nv
        if not iv:
            return False
    return any(max(a_, zl) < min(b_, zh) for a_, b_ in iv)


def _ns_fractals(arr, lo, hi, N, sup, mode):
    """Swing fractal pada arr[lo:hi] (Long: high, Short: low), N candle kiri & kanan harus STRIKT kurang ekstrem.
    mode 'merge': candle berurutan dgn angka sama = SATU KESATUAN (kesatuan valid kalau N candle sebelum dan N candle sesudah kesatuan
      strikt tidak melebihi/sama). mode 'invalid': angka sama dgn tetangga = bukan swing. mode 'valid': sama boleh (cara lama).
    Return (list idx candle TERAKHIR tiap swing (menentukan kapan swing terkonfirmasi), dict {idx terakhir: idx candle PERTAMA kesatuan})."""
    m = hi - lo
    if m < 2 * N + 1:
        return [], {}
    seg = arr[lo:hi]
    if mode == 'valid':
        w = np.lib.stride_tricks.sliding_window_view(seg, 2 * N + 1)
        c_ = w[:, N]
        m_ = (c_ >= w.max(axis=1)) if sup else (c_ <= w.min(axis=1))
        ids = (np.flatnonzero(m_) + lo + N).tolist()
        return ids, {i: i for i in ids}
    chg = np.flatnonzero(seg[1:] != seg[:-1]) + 1
    starts = np.concatenate(([0], chg)); ends = np.concatenate((chg - 1, [m - 1]))
    if mode != 'merge':
        keep = ends == starts
        starts, ends = starts[keep], ends[keep]
    ok = (starts - N >= 0) & (ends + N < m)
    starts, ends = starts[ok], ends[ok]
    v = seg[starts]
    good = np.ones(len(v), dtype=bool)
    for kk in range(1, N + 1):
        if sup:
            good &= (seg[starts - kk] < v) & (seg[ends + kk] < v)
        else:
            good &= (seg[starts - kk] > v) & (seg[ends + kk] > v)
    st = (starts[good] + lo).tolist(); en = (ends[good] + lo).tolist()
    return en, dict(zip(en, st))


def _ns_find_mss(sup, tj0, stop, H5, L5, C5):
    """Market Structure Shift M5 setelah acuan tersentuh di candle tj0 (SMC).
    Long : harga turun ke acuan, low terendah = P. MSS = candle M5 pertama yang CLOSE di atas swing high referensi.
    Short: kebalikannya (P = high tertinggi, referensi = swing low).
    P VALID: candle ekstrem (Long: low terendah sejak sentuh; Short: high tertinggi) yang tidak dilampaui oleh 1 candle di kirinya
      dan 1 candle di kanannya. Angka SAMA PERSIS dgn tetangga kiri/kanan juga TIDAK valid (NS_P_EQUAL='invalid', default): P harus
      benar-benar lebih dalam dari kedua tetangganya (Long: low-nya < low kiri DAN < low kanan). P baru dianggap valid setelah candle
      kanannya closed; kalau candle kanan membuat ekstrem yang lebih jauh, kandidat lama gugur (bukan P) dan kandidat baru yang dipakai;
      kalau candle kanan sama persis, kandidat itu tidak valid sampai ada ekstrem baru yang lebih dalam.
      MSS hanya dicek terhadap P yang valid.
    Swing referensi:
     - Selama baru ada 1 P valid: swing fractal (NS_MSS_SWING kiri-kanan, default 1; angka sama dgn tetangga = BUKAN swing, NS_SW_EQUAL='invalid';
       boleh dari NS_LOOKBACK candle sebelum sentuh) terakhir sebelum P.
     - NS_MSS_REF='range' (default): tiap P valid baru muncul, swing = HIGH TERTINGGI (Short: LOW TERENDAH) antara P valid lama s/d
       P valid baru (inklusif). Dihitung ulang di SETIAP pergeseran P (tidak kumulatif).
     - NS_MSS_REF='range_max': sama, tapi swing hanya naik (Long) / turun (Short), tidak pernah diturunkan (tertinggi sejak P pertama).
     - NS_MSS_REF='fractal' (cara lama): referensi selalu swing fractal terakhir sebelum P.
    Return (idx candle MSS, idx P valid, idx candle swing yang ditembus) / None."""
    N = NS_MSS_SWING
    hi = min(stop, len(H5))
    lo = max(tj0 - NS_LOOKBACK, 0)
    arr = H5 if sup else L5
    use_range = (NS_MSS_REF != 'fractal')
    sw, sw_first = _ns_fractals(arr, lo, hi, N, sup, NS_SW_EQUAL)
    if not sw and not use_range:
        return None
    more = (lambda a, b: L5[a] < L5[b]) if sup else (lambda a, b: H5[a] > H5[b])        # a lebih ekstrem dari b
    strict = (NS_P_EQUAL == 'invalid')
    if strict:      # tetangga b harus benar-benar tidak melampaui a: sama persis = tidak valid
        ok_nb = (lambda a, b: L5[b] > L5[a]) if sup else (lambda a, b: H5[b] < H5[a])
    else:           # sama dianggap boleh
        ok_nb = (lambda a, b: L5[b] >= L5[a]) if sup else (lambda a, b: H5[b] <= H5[a])
    merge = (NS_P_EQUAL == 'merge')
    ext5 = L5 if sup else H5
    cand = tj0; cand_last = tj0
    if merge:       # kesatuan angka sama: ikut melebar ke kiri (candle sebelum sentuh yg angkanya sama), lalu tetangga kiri harus STRIKT kurang ekstrem
        while cand - 1 >= 0 and ext5[cand - 1] == ext5[tj0]:
            cand -= 1
        left_ok = (cand - 1 < 0) or ((ext5[cand - 1] > ext5[cand]) if sup else (ext5[cand - 1] < ext5[cand]))
    else:
        left_ok = (tj0 - 1 < 0) or ok_nb(tj0, tj0 - 1)
    Pv = None; Pv_last = None; rref = None
    for t in range(tj0 + 1, hi):
        if more(t, cand):                               # ekstrem baru -> kandidat lama gugur
            cand = t; cand_last = t; left_ok = True
            continue
        if merge and t == cand_last + 1 and ext5[t] == ext5[cand]:
            cand_last = t                               # angka sama menyambung -> satu kesatuan, tunggu candle berikutnya
            continue
        # candle t tidak melampaui cand: cek sisi kanan (candle tepat setelah cand/kesatuan), lalu cand = P valid kalau sisi kirinya juga aman
        if (not merge) and t == cand + 1 and not ok_nb(cand, t):    # sama persis dgn candle kanan -> cand tidak valid
            left_ok = False
        if Pv != cand and left_ok:
            if use_range and Pv is not None:
                seg = H5[Pv_last:cand + 1] if sup else L5[Pv_last:cand + 1]
                cx = Pv_last + int(seg.argmax() if sup else seg.argmin())
                if NS_MSS_REF == 'range_max' and rref is not None and ((H5[cx] <= H5[rref]) if sup else (L5[cx] >= L5[rref])):
                    pass            # 'range_max': swing hanya naik/turun, tidak pernah diturunkan
                else:
                    rref = cx       # 'range': swing dihitung ulang di SETIAP pergeseran P valid
            Pv = cand; Pv_last = cand_last
        if Pv != cand:
            continue
        if use_range and rref is not None:
            ref = rref
        else:
            k = bisect.bisect_right(sw, min(cand - 1, t - N)) - 1
            if k < 0:
                continue
            ref = sw_first[sw[k]]
        if (C5[t] > H5[ref]) if sup else (C5[t] < L5[ref]):
            return t, cand, ref
    return None


def _ns_find_fvg(sup, P, q, stop, X):
    """FVG (3 candle M5: c1,c2,c3) di dalam pergerakan MSS, untuk entry limit.
    Long : FVG bullish (low c3 > high c1), zona [high c1, low c3]. Short: FVG bearish (high c3 < low c1), zona [high c3, low c1].
    Kandidat: c1 >= P (mulai dari P) dan c3 <= q (candle MSS). Diambil yang PALING DEKAT dgn break MSS (c3 terbesar).
    Kalau belum ada FVG sampai candle MSS, tunggu 1 candle lagi: candle setelah MSS (q+1) boleh jadi c3 (c1=q-1, c2=q).
    Return (idx c3, batas bawah zona, batas atas zona) / None."""
    flag = X['bull'] if sup else X['bear']
    H5, L5 = X['H5'], X['L5']
    k = None
    for kk in range(q, P + 1, -1):                 # c3 = kk, c1 = kk-2 >= P
        if flag[kk]:
            k = kk; break
    if k is None:
        kk = q + 1
        if kk < min(stop, len(H5)) and flag[kk]:   # tunggu 1 candle lagi setelah MSS
            k = kk
    if k is None:
        return None
    if sup:
        return k, float(H5[k - 2]), float(L5[k])
    return k, float(H5[k]), float(L5[k - 2])


def _ns_h1_swings(sup, bi, hh, hl, hc, mode):
    """Acuan H1 dgn metode 'P dan swing' (seperti M5). Mulai SETELAH break-in (candle bi).
    Mode 'swing_antara' (default): Long: P = HIGH valid, swing = LOW; Short: P = LOW valid, swing = HIGH. P valid = candle H1 yang high-nya (Long)
      LEBIH TINGGI dari candle sebelum dan sesudahnya; Short: low-nya lebih rendah. Angka SAMA pada candle berurutan dijadikan SATU KESATUAN
      (NS_H1_EQUAL='merge', default): kesatuan itu valid kalau candle sebelum kesatuan dan candle sesudah kesatuan strikt tidak melebihi/sama.
      NS_H1_EQUAL='invalid': angka sama dgn tetangga = tidak valid.
      Mode 'swing_ditembus' membalik peran: Long: P = low, swing = high (Short sebaliknya).
    P baru juga HARUS melebihi P valid sebelumnya (Long/swing_antara: high lebih tinggi; Short: low lebih rendah; sama persis tidak) -- ekstrem valid yang tidak melebihi
      P sebelumnya bukan P. P valid pertama belum punya swing. NS_H1_P_EXCEED=0 mematikan syarat ini.
    Swing = ekstrem lawan P (Long/swing_antara: LOW terendah) di antara P valid lama dan P valid baru (candle P-nya sendiri tidak ikut); swing juga harus valid
      (strikt lebih ekstrem dari candle sebelum & sesudahnya, kesatuan angka sama dihitung satu).
    Titik acuan = swing. 'swing_ditembus': acuan baru aktif setelah candle H1 pertama (sesudah P valid baru confirmed) yang CLOSE menembus swing
      (Long: close > swing high, Short: close < swing low). 'swing_antara': aktif begitu P valid baru confirmed (candle sesudah kesatuan P closed).
    Return list (idx H1 candle swing [pertama di kesatuan], idx H1 candle saat acuan aktif)."""
    n = len(hc)
    p_low = (sup if mode == 'swing_ditembus' else (not sup))      # P = low?
    pv, sv = (hl, hh) if p_low else (hh, hl)
    merge = (NS_H1_EQUAL == 'merge')
    # a "tidak melebihi" b: P low -> low tetangga lebih tinggi; P high -> high tetangga lebih rendah (strikt)
    below_p = (lambda a, b: a > b) if p_low else (lambda a, b: a < b)
    below_s = (lambda a, b: a < b) if p_low else (lambda a, b: a > b)    # swing: high -> tetangga lebih rendah; low -> tetangga lebih tinggi
    out = []; prev = None                                          # prev = (idx pertama, idx terakhir) kesatuan P valid terakhir
    k = bi + 1
    while k < n - 1:
        if pv[k - 1] == pv[k]:                                     # kesatuan dimulai sebelum k (sebelum/di bi) -> lewati
            k += 1; continue
        f = l = k
        if merge:
            while l + 1 < n and pv[l + 1] == pv[f]:
                l += 1
        k = l + 1 if merge else k + 1
        if l + 1 >= n:
            break
        if not (below_p(pv[f - 1], pv[f]) and below_p(pv[l + 1], pv[f])):
            continue                                              # bukan P valid
        if NS_H1_P_EXCEED:
            if prev is None:                                      # P valid pertama: belum ada P sebelumnya -> belum ada swing
                prev = (f, l)
                continue
            if not (pv[f] < pv[prev[0]] if p_low else pv[f] > pv[prev[0]]):
                continue                                          # tidak melebihi P sebelumnya -> bukan P
            a0 = prev[1] + 1
        else:
            a0 = bi if prev is None else prev[1] + 1              # (cara sebelumnya) P lama pertama = candle bi
        prev = (f, l)                                             # candle P lama & P baru tidak ikut dihitung (swing di antara keduanya)
        seg = sv[a0:f]
        if len(seg) == 0:
            continue
        sidx = a0 + int(seg.argmax() if p_low else seg.argmin())
        sf = sl = sidx
        if merge:                                                 # kesatuan angka sama pada swing
            while sf - 1 >= 0 and sv[sf - 1] == sv[sidx]:
                sf -= 1
            while sl + 1 < n and sv[sl + 1] == sv[sidx]:
                sl += 1
        if sf < 1 or sl + 1 >= n:
            continue
        if not (below_s(sv[sf - 1], sv[sf]) and below_s(sv[sl + 1], sv[sf])):
            continue                                              # swing tidak valid (tetangga melebihi/sama)
        if mode == 'swing_ditembus':
            tail = hc[l + 1:] > sv[sf] if p_low else hc[l + 1:] < sv[sf]
            if not tail.any():
                continue
            live = l + 1 + int(tail.argmax())
        else:
            live = l + 1
        out.append((sf, live))
    return out


def ns_chain(S, LV, X):
    """Satu setup (sudah BOS) -> urutan trade (harga saja) + status akhir.
    X = array konteks M5/H1 (lihat ns_detect_events). Return (trades, status)."""
    sup = S['sup']; lvl = S['L']; i = S['i']; bi = S['bi']; bos = S['bos']; ext = S['ext']; dz = S['dz']
    ts5, H5, L5, C5, O5 = X['ts5'], X['H5'], X['L5'], X['C5'], X['O5']
    hts, hh, hl, hc, lm5 = X['hts'], X['hh'], X['hl'], X['hc'], X['lm5']
    bull, bear = X['bull'], X['bear']
    n5 = len(ts5)
    seen0 = max(int(hts[bos]) + H1_MS, S['vf'])
    s0 = int(np.searchsorted(ts5, seen0))
    # --- semua level acuan dalam range ---
    acs = []; ac_time = {}
    if NS_ACUAN_MODE in ('swing_ditembus', 'swing_antara'):
        # acuan = swing H1 (metode P & swing seperti M5)
        sv_ = hl if (NS_ACUAN_MODE == 'swing_ditembus') != sup else hh        # array swing: Long/ditembus -> high, Short/ditembus -> low
        for sidx, live in _ns_h1_swings(sup, bi, hh, hl, X['hc'], NS_ACUAN_MODE):
            A = float(sv_[sidx])
            if NS_RANGE_MELEBAR:
                rext = float(hh[i:live + 1].max()) if sup else float(hl[i:live + 1].min())
            else:
                rext = ext
            if ((lvl <= A <= rext) if sup else (rext <= A <= lvl)):
                av = max(int(hts[live]) + H1_MS, seen0)
                acs.append((int(np.searchsorted(ts5, av)), A)); ac_time.setdefault(A, int(hts[sidx]))
    else:
        for x in LV:
            if x['ty'] != S['ty'] or x['c1'] <= bi:
                continue
            c4 = x['c4']
            if NS_RANGE_MELEBAR:
                rext = float(hh[i:c4 + 1].max()) if sup else float(hl[i:c4 + 1].min())
            else:
                rext = ext
            if ((lvl <= x['L'] <= rext) if sup else (rext <= x['L'] <= lvl)):
                av = max(int(hts[c4]) + H1_MS, seen0)
                acs.append((int(np.searchsorted(ts5, av)), x['L']))
    acs.sort()
    if not acs:
        return [], 'tanpa_acuan'
    if s0 >= n5:
        return [], 'pending'
    a_bos = int(np.searchsorted(ts5, hts[bos]))
    dead_arr = (hc < dz) if sup else (hc > dz)                # close H1 menembus titik terjauh antara break-out & break-in
    dm5 = lm5[np.flatnonzero(dead_arr)]
    def next_death(j):
        p = int(np.searchsorted(dm5, j))
        return int(dm5[p]) if p < len(dm5) else n5

    # --- monitoring M5: acuan tersentuh (wick boleh) -> titik BOS dikunci -> tunggu MSS M5 -> ENTRY MARKET di close candle MSS ---
    trades = []; used = set(); locked = None; j = s0; status = 'pending'
    while True:
        dead_idx = next_death(j)
        end = min(dead_idx, n5)
        tl = None
        if locked is not None:
            e2 = min(dead_idx + 1, n5)
            if j < e2:
                seg = (H5[j:e2] >= locked) if sup else (L5[j:e2] <= locked)
                if seg.any():
                    tl = j + int(seg.argmax())
        best_t = None
        for av, A in acs:
            if A in used or av >= end:
                continue
            s_ = max(j, av)
            lim = end if best_t is None else min(end, best_t + 1)
            if s_ >= lim:
                continue
            seg = (L5[s_:lim] <= A) if sup else (H5[s_:lim] >= A)
            if seg.any():
                t = s_ + int(seg.argmax())
                if best_t is None or t < best_t:
                    best_t = t
        if tl is not None and (best_t is None or tl <= best_t):
            status = 'bos_selesai'; break
        if best_t is None:
            status = 'level_ditembus_lagi' if dead_idx < n5 else 'pending'; break
        touched = [A for av, A in acs if A not in used and av <= best_t and ((L5[best_t] <= A) if sup else (H5[best_t] >= A))]
        for A in touched:
            used.add(A)
        tj0 = best_t
        BE = locked if locked is not None else float(H5[a_bos:tj0 + 1].max() if sup else L5[a_bos:tj0 + 1].min())
        if locked is None:
            locked = BE
        q0 = tj0 + 1
        cancel_idx = n5
        if q0 < n5:
            seg = (H5[q0:] >= BE) if sup else (L5[q0:] <= BE)
            if seg.any():
                cancel_idx = q0 + int(seg.argmax())
        dead2 = next_death(q0)
        stop = min(cancel_idx, dead2)
        trig = None; no_fvg = False
        mss = _ns_find_mss(sup, tj0, stop, H5, L5, C5)
        if mss is not None:
            q, P, ref = mss
            if sup:
                slv = float(L5[P]) * (1 - NS_SL_BUF)
            else:
                slv = float(H5[P]) * (1 + NS_SL_BUF)
            if NS_ENTRY_MODE == 'market':
                entry = float(C5[q])
                ok = (slv < entry < BE) if sup else (BE < entry < slv)
                if ok:
                    trig = (q, entry, slv, P, ref, None, None)
            else:
                # LIMIT di FVG terdekat dgn MSS (c3 terbesar <= q; kalau belum ada, tunggu candle q+1).
                # Limit dipasang setelah candle c3 closed (po), terfill di candle pertama setelahnya yang wick-nya menyentuh harga limit;
                # batal kalau harga lebih dulu sampai titik BOS / level mati (stop).
                fvg = _ns_find_fvg(sup, P, q, stop, X)
                if fvg is None:
                    no_fvg = (q + 1 < min(stop, n5))
                else:
                    k, zl, zh = fvg
                    po = max(q, k)
                    if sup:
                        entry = {'near': zh, 'mid': (zl + zh) / 2, 'far': zl}.get(NS_FVG_EDGE, zh)
                    else:
                        entry = {'near': zl, 'mid': (zl + zh) / 2, 'far': zh}.get(NS_FVG_EDGE, zl)
                    entry = float(entry)
                    ok = (slv < entry < BE) if sup else (BE < entry < slv)
                    e2 = min(stop, n5)
                    if ok and po + 1 < e2:
                        seg = (L5[po + 1:e2] <= entry) if sup else (H5[po + 1:e2] >= entry)
                        if seg.any():
                            trig = (q, entry, slv, P, ref, po + 1 + int(seg.argmax()), fvg)
        if trig is None:
            if no_fvg:
                status = 'tanpa_fvg'
            elif cancel_idx <= dead2 and cancel_idx < n5:
                status = 'gugur_ke_titik_BOS'
            elif dead2 < n5:
                status = 'level_ditembus_lagi'
            else:
                status = 'pending'
            break
        q, entry, slv, P, ref, f_fill, fvg = trig; tp = BE
        if f_fill is None:                      # market: posisi dibuka di open candle q+1, SL & TP dicek mulai q+1
            if q + 1 >= n5:
                status = 'pending'; break
            a_sl = a_tp = q + 1
        else:                                   # limit: SL dicek mulai candle fill, TP baru candle berikutnya (urutan dlm candle fill tak diketahui)
            a_sl, a_tp = f_fill, f_fill + 1
        if sup:
            sg_ = L5[a_sl:] <= slv; tg_ = H5[a_tp:] >= tp
        else:
            sg_ = H5[a_sl:] >= slv; tg_ = L5[a_tp:] <= tp
        si = a_sl + int(sg_.argmax()) if sg_.any() else n5
        ti = a_tp + int(tg_.argmax()) if tg_.any() else n5
        tr = {'touch': tj0, 'q': q, 'entry': entry, 'sl': slv, 'tp': tp,
              'acuan': (max(touched) if sup else min(touched)), 'P': P, 'ref': ref, 'f': f_fill, 'fvg': fvg}
        _a_px = tr['acuan']
        tr['ac_ts'] = ac_time.get(_a_px) if ac_time else next((int(hts[x['c1']]) for x in LV if x['ty'] == S['ty'] and x['c1'] > bi and x['L'] == _a_px), None)
        if si >= n5 and ti >= n5:
            tr.update(why='Open', z=n5 - 1); trades.append(tr); status = 'terbuka'; break
        if si <= ti:
            tr.update(why='SL', z=si); trades.append(tr)
            if NS_MAX_ENTRY and len(trades) >= NS_MAX_ENTRY:
                status = 'maks_entry'; break
            j = si + 1
            if j >= n5:
                status = 'pending'; break
            continue
        tr.update(why='TP', z=ti); trades.append(tr); status = 'selesai_TP'; break
    return trades, status


def ns_detect_events(df, m5):
    """Return (events, stats, setups). events = 1 entry market per trade (urut fill_ts); setups = info per setup (untuk /setups.txt)."""
    stats = {'levels': 0, 'break_out_in': 0, 'wick_batal_keluar': 0, 'wick_batal_masuk': 0, 'terjauh_bukan_swing': 0,
             'no_fvg': 0, 'batal_sebelum_bos': 0, 'tanpa_bos': 0, 'bos': 0, 'tanpa_acuan': 0, 'level_ditembus_lagi': 0,
             'bos_selesai': 0, 'gugur_ke_titik_BOS': 0, 'tanpa_fvg': 0, 'maks_entry': 0, 'selesai_TP': 0, 'pending': 0, 'terbuka': 0, 'entry': 0,
             'acuan_total': 0}
    events, setups = [], []
    if m5 is None or df is None or len(df) < 30:
        return events, stats, setups
    H1 = {k: df[v].values.astype(float) for k, v in (('o', 'open'), ('h', 'high'), ('l', 'low'), ('c', 'close'))}
    H1['ts'] = df['ts'].values.astype(np.int64)
    LV = ns_levels(H1)
    stats['levels'] = len(LV)
    S_list = ns_setups(H1, m5, LV, stats)
    ts5, H5, L5, C5, O5 = m5['TS'], m5['H'], m5['L'], m5['C'], m5['O']
    n5 = len(ts5)
    bull = np.zeros(n5, dtype=bool); bear = np.zeros(n5, dtype=bool)
    if n5 > 2:
        bull[2:] = L5[2:] > H5[:-2]
        bear[2:] = H5[2:] < L5[:-2]
    hts = H1['ts']
    X = {'ts5': ts5, 'H5': H5, 'L5': L5, 'C5': C5, 'O5': O5, 'hts': hts, 'hh': H1['h'], 'hl': H1['l'], 'hc': H1['c'],
         'lm5': np.searchsorted(ts5, hts + H1_MS - M5_MS), 'bull': bull, 'bear': bear}
    seen = set(); bos_cnt = {}
    for S in S_list:
        trades, status = ns_chain(S, LV, X)
        stats[status] = stats.get(status, 0) + 1
        d = S['d']; kind = 'SNR_SUPPORT' if d == 'Long' else 'SNR_RESISTANCE'
        info = {'d': d, 'L': S['L'], 'c1_ts': int(hts[S['i']]), 'bo_ts': int(hts[S['bo']]), 'bi_ts': int(hts[S['bi']]),
                'ext': S['ext'], 'bos_ts': int(hts[S['bos']]), 'status': status, 'trades': []}
        for k_, tr in enumerate(trades):
            _P, _ref = tr['P'], tr['ref']
            key = (d, round(tr['entry'], 10), round(tr['sl'], 10), int(ts5[tr['q']]))
            bk = (d, int(hts[S['bos']]))
            if NS_MAX_ENTRY and key not in seen and bos_cnt.get(bk, 0) >= NS_MAX_ENTRY:
                continue          # 1 BOS maksimal NS_MAX_ENTRY entry (lintas setup kembar yang BOS-nya sama)
            info['trades'].append({'why': tr['why'], 'entry': tr['entry'], 'sl': tr['sl'], 'tp': tr['tp'],
                                   'ac_ts': tr['ac_ts'],
                                   'p': float(H5[_P] if d == 'Short' else L5[_P]), 'p_ts': int(ts5[_P]),
                                   'sw': float(L5[_ref] if d == 'Short' else H5[_ref]), 'sw_ts': int(ts5[_ref]),
                                   'mss_ts': int(ts5[tr['q']]),
                                   'fvg': (None if tr['fvg'] is None else
                                           {'lo': tr['fvg'][1], 'hi': tr['fvg'][2], 'c1_ts': int(ts5[tr['fvg'][0] - 2]), 'c3_ts': int(ts5[tr['fvg'][0]])})})
            if key in seen:           # setup kembar (BOS sama, level beda) -> 1 sinyal
                continue
            seen.add(key); bos_cnt[bk] = bos_cnt.get(bk, 0) + 1
            stats['entry'] += 1
            events.append({
                'kind': kind, 'type': S['ty'], 'level': S['L'], 'direction': d,
                'entry_price': tr['entry'], 'sl_price': tr['sl'],
                'fill_ts': (int(ts5[tr['q']]) + M5_MS) if tr['f'] is None else int(ts5[tr['f']]),
                'test1_ts': int(ts5[tr['touch']]), 'test2_ts': int(ts5[tr['q']]), 'c1_ts': int(hts[S['i']]),
                'targets': [], 'tp_fixed': tr['tp'], 'tp_on_fill': (tr['f'] is None),
                'meta': {'bo_ts': int(hts[S['bo']]), 'bi_ts': int(hts[S['bi']]), 'ext': S['ext'], 'bos_ts': int(hts[S['bos']]),
                         'acuan': tr['acuan'], 'seq': k_ + 1, 'tp': tr['tp'], 'mss_ts': int(ts5[tr['q']])},
            })
        setups.append(info)
    events.sort(key=lambda e: e['fill_ts'])
    return events, stats, setups


# ============================================================
# STRUKTUR M5 UTK LOOKUP CEPAT
# ============================================================

def prepare_m5(df_m5):
    if df_m5 is None or df_m5.empty:
        return None
    df_m5 = df_m5.sort_values('ts').reset_index(drop=True)
    return {
        'TS': df_m5['ts'].values.astype(np.int64),
        'O': df_m5['open'].values.astype(float), 'H': df_m5['high'].values.astype(float),
        'L': df_m5['low'].values.astype(float), 'C': df_m5['close'].values.astype(float),
    }


def prepare_coin(symbol, df, m5):
    df = df.sort_values('ts').reset_index(drop=True)
    setups = []
    if STRATEGY == 'ifvg_ema':
        events, stats = detect_all_events(df, m5)
    else:
        events, stats, setups = ns_detect_events(df, m5)
    return {
        'symbol': symbol,
        'TS': df['ts'].values.astype(np.int64),
        'events': events,   # list entry (limit terfill / market) urut fill_ts
        'stats': stats,
        'setups': setups,   # info per setup (strategi baru) utk /setups.txt
        'n': len(df),
    }


# ============================================================
# SIMULASI GABUNGAN (semua koin, 1 balance, 1 pool slot)
# ============================================================
#
# Tiap event dari detect_all_events() = LIMIT di IFVG yang sudah terfill di fill_ts (entry_price = harga fill,
# sl_price = kedalaman terjauh sejak sentuh). Posisi dibuka saat waktu (M5) sampai di fill_ts. Slot/margin/min order
# tidak cukup, atau sudah ada posisi searah di koin yang sama -> setup hangus.
# EXIT: SL tetap; TARGET dinamis = ujung wick level lawan terbaru (H1+FVG) yang belum tersentuh & lebih jauh dari entry.

def _cand_targets(targets, direction, E, fill_ts):
    """Saring kandidat target sekali saat entry: wick T & level di sisi profit dari entry, dan belum tersentuh
    sebelum candle fill. targets = (act_ts, first_touch_ts, level, T, c1_ts)."""
    out = []
    for act, ft, lvl, T, c1 in targets:
        if ft < fill_ts:
            continue
        if direction == 'Long':
            if lvl > E and T > E:
                out.append((act, ft, T, c1))
        else:
            if lvl < E and T < E:
                out.append((act, ft, T, c1))
    return out


def _current_target(pos, now_ts):
    """Target aktif di candle now_ts: level lawan yang sudah aktif (act<=now), belum tersentuh (ft>=now); pilih c1 TERBARU.
    Strategi baru: TP fixed (titik BOS terkunci) dipakai langsung."""
    if pos.get('tp_fixed') is not None:
        return pos['tp_fixed']
    best = None
    for act, ft, T, c1 in pos['targets']:
        if act <= now_ts <= ft and (best is None or c1 > best[1]):
            best = (T, c1)
    return None if best is None else best[0]


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
            'c1_ts': ev['c1_ts'], 'test1_ts': ev['test1_ts'], 'test2_ts': ev['test2_ts'], 'meta': ev.get('meta', {}),
            'fill_ts': entry_ts, 'targets': _cand_targets(ev['targets'], direction, entry_price, entry_ts),
            'tp_fixed': ev.get('tp_fixed'), 'tp_on_fill': bool(ev.get('tp_on_fill')),
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
            'test1_ts': pos['test1_ts'], 'test2_ts': pos['test2_ts'], 'meta': pos['meta'],
            'entry_wib': _fmt_wib(pos['entry_ts']),
            'exit_wib': _fmt_wib(exit_ts),
        })

    def process_symbol_tick(symbol, j):
        """Proses SEMUA hal (exit posisi & fill limit) untuk 1 simbol di index candle j."""
        m5 = m5_data[symbol]
        now_ts = int(m5['TS'][j])
        hi, lo = m5['H'][j], m5['L'][j]

        # 1) exit posisi aktif simbol ini. Urutan per candle: SL dulu (exit = min/max(SL, open) utk gap), lalu TARGET
        #    (exit = max/min(T, open)). Candle fill (entry_ts == now_ts) tidak dicek TP.
        op = m5['O'][j]
        keys = positions_by_symbol.get(symbol)
        if keys:
            for key in list(keys):
                pos = active_positions[key]
                S = pos['sl']
                if pos['direction'] == 'Long':
                    if lo <= S * (1 + 1e-9):
                        close_trade(key, min(S, op), 'SL', now_ts)
                        continue
                    if pos['entry_ts'] < now_ts or pos.get('tp_on_fill'):
                        T = _current_target(pos, now_ts)
                        if T is not None and hi >= T:
                            close_trade(key, max(T, op), 'TP', now_ts)
                else:  # Short
                    if hi >= S * (1 - 1e-9):
                        close_trade(key, max(S, op), 'SL', now_ts)
                        continue
                    if pos['entry_ts'] < now_ts or pos.get('tp_on_fill'):
                        T = _current_target(pos, now_ts)
                        if T is not None and lo <= T:
                            close_trade(key, min(T, op), 'TP', now_ts)

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
                if (pos['direction'] == 'Long' and lo <= pos['sl'] * (1 + 1e-9)) or \
                   (pos['direction'] == 'Short' and hi >= pos['sl'] * (1 - 1e-9)):
                    close_trade(opened_key, min(pos['sl'], op) if pos['direction'] == 'Long' else max(pos['sl'], op), 'SL', now_ts)

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
        if STRATEGY == 'ifvg_ema':
            _log_msg(f"🚀 Mulai backtest SNR (level H1 + FVG H1 + bias EMA{BIAS_EMA_FAST}/{BIAS_EMA_SLOW} + Inverted FVG M5, exit target level lawan) — {len(SYMBOLS)} koin, {BACKTEST_START_DATE} s/d {BACKTEST_END_DATE}")
            _log_msg(f"   Level c1/c2 (c3 bersih) + FVG H1 di C1..C{FVG_LAST_C} + level tidak berulang sebelum FVG + bias EMA (filter={'ON' if EMA_BIAS_FILTER else 'OFF'}, warm-up {EMA_WARMUP}) "
                      f"-> sentuh -> IFVG M5 (lookback {IFVG_LOOKBACK}) -> LIMIT di IFVG, SL = kedalaman terjauh sejak sentuh, tanpa expired. "
                      f"Exit: TARGET = wick level lawan terbaru yang belum tersentuh (H1+FVG, tanpa EMA); SL dicek duluan.")
        else:
            _log_msg(f"🚀 Mulai backtest STRATEGI BARU (S&R H1 berFVG -> break-out/in -> BOS -> multi-acuan -> MSS M5) — {len(SYMBOLS)} koin, {BACKTEST_START_DATE} s/d {BACKTEST_END_DATE}")
            _log_msg(f"   Swing titik terjauh {NS_SWING_EXT} kiri-kanan, range acuan {'melebar' if NS_RANGE_MELEBAR else 'tetap'}, MSS M5 (swing {NS_MSS_SWING} kiri-kanan, dari {NS_LOOKBACK} candle M5 sebelum sentuh), "
                      f"entry {'LIMIT di FVG terdekat MSS (titik ' + NS_FVG_EDGE + ')' if NS_ENTRY_MODE != 'market' else 'MARKET di close candle MSS'}, SL +{NS_SL_BUF*100:.2f}% di luar ujung MSS, TP = titik BOS terkunci, tanpa expired.")

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
            if STRATEGY != 'ifvg_ema':
                _log_msg(f"   ✅ {symbol}: {cp['n']} candle H1, {st['levels']} level, {st['break_out_in']} break-out+in "
                          f"(batal wick keluar/masuk {st['wick_batal_keluar']}/{st['wick_batal_masuk']}, bukan swing {st['terjauh_bukan_swing']}, tanpa FVG {st['no_fvg']}), "
                          f"{st['bos']} BOS (batal sebelum BOS {st['batal_sebelum_bos']}); setup: TP {st['selesai_TP']}, BOS usai {st['bos_selesai']}, "
                          f"level ditembus lagi {st['level_ditembus_lagi']}, tanpa acuan {st['tanpa_acuan']}, pending {st['pending']}; {st['entry']} entry ({kind_str or '-'}).")
                continue
            _log_msg(f"   ✅ {symbol}: {cp['n']} candle H1, {st['levels']} level, {st['aktif']} aktif, {st['entry']} limit terfill ({kind_str or '-'}); "
                      f"gugur: tanpa FVG {st['tanpa_fvg']}, disentuh sebelum FVG {st['sudah_disentuh']}, level berulang {st['ulang']}, warm-up {st['warmup']}, "
                      f"EMA {st['ema_ditolak']}; M5: tanpa sentuh {st['no_touch']}, tanpa IFVG {st['no_ifvg']}, limit tak terfill {st['no_fill']}, SL tidak valid {st['sl_invalid']}.")

        _log_msg(f"🧮 Menjalankan simulasi gabungan ({len(coins)} koin)...")
        result = run_combined_backtest(coins, m5_data)
        sum_st = lambda k: sum(cp['stats'].get(k, 0) for cp in coins.values())

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
                'aktif_count': sum_st('aktif'), 'tanpa_fvg_count': sum_st('tanpa_fvg'),
                'disentuh_count': sum_st('sudah_disentuh'), 'ulang_count': sum_st('ulang'),
                'warmup_count': sum_st('warmup'), 'ema_count': sum_st('ema_ditolak'),
                'no_touch_count': sum_st('no_touch'), 'no_ifvg_count': sum_st('no_ifvg'),
                'no_fill_count': sum_st('no_fill'), 'sl_invalid_count': sum_st('sl_invalid'),
            })
            _results[:] = per_symbol_breakdown(result['trades'])
            _kind_results[:] = per_kind_breakdown(result['trades'])
            _per_coin_results[:] = per_coin_rows
            _setups_all[:] = [dict(s, symbol=sym) for sym, cp in coins.items() for s in cp.get('setups', [])]
            _ns_stats.clear()
            _ns_stats.update({k: sum_st(k) for k in ('levels', 'break_out_in', 'wick_batal_keluar', 'wick_batal_masuk',
                                                      'terjauh_bukan_swing', 'no_fvg', 'batal_sebelum_bos', 'tanpa_bos', 'bos',
                                                      'tanpa_acuan', 'tanpa_fvg', 'maks_entry', 'level_ditembus_lagi', 'bos_selesai', 'selesai_TP',
                                                      'gugur_ke_titik_BOS', 'pending', 'terbuka', 'entry')})
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
    ns = dict(_ns_stats)
    if STRATEGY == 'ifvg_ema':
        title_html = f"📊 Backtest SNR (H1 level + FVG + EMA{BIAS_EMA_FAST}/{BIAS_EMA_SLOW} bias + IFVG M5, target level lawan)"
        note_html = f'''  <div class="note">
    💡 <b>Strategi (level H1 + FVG H1 + Inverted FVG M5)</b>:
    <br>• <b>Level</b> (H1): c1+c2 berlawanan arah, level = close c1; c3 tidak boleh menyentuh level. Wajib ada <b>FVG H1 searah</b> di C1..C{FVG_LAST_C}
    (3 candle berurutan, gap saja; candle ke-3 close melewati wick candle ke-2). Level aktif saat FVG selesai. Gugur kalau harga sudah menyentuh level
    antara c4 s/d candle penutup FVG, atau kalau pasangan level searah muncul lagi sebelum FVG selesai.
    <br>• <b>Bias EMA{BIAS_EMA_FAST}/{BIAS_EMA_SLOW}</b> (level awal saja): Long hanya kalau EMA{BIAS_EMA_FAST} &gt; EMA{BIAS_EMA_SLOW}, Short kalau &lt;
    (filter {'ON' if EMA_BIAS_FILTER else 'OFF'}, warm-up {EMA_WARMUP} candle). Level searah baru menggantikan yang lama; tanpa expired.
    <br>• <b>Konfirmasi M5 (IFVG)</b>: setelah level disentuh, FVG M5 searah harus 'mengambil' gap FVG M5 tidak searah (boleh terbentuk sampai {IFVG_LOOKBACK} candle
    sebelum sentuh). Entry <b>limit</b> di tepi atas overlap (Short: bawah); IFVG baru sebelum fill memindahkan limit.
    <b>SL</b> = kedalaman terjauh sejak sentuh s/d FVG searah selesai (tanpa buffer).
    <br>• <b>Exit</b>: tanpa trailing. <b>TARGET</b> = ujung wick level lawan (H1+FVG, tanpa EMA) terbaru yang belum tersentuh dan lebih jauh dari entry;
    berubah dinamis. SL dicek duluan dalam candle yang sama; TP tidak dicek di candle fill.
    <br>⚙️ Risk {RISK_PCT*100:.0f}% dari balance (compounding). Slot maksimum: {_fmt_max_concurrent()}.
    Level terdeteksi: {cr.get('levels_count',0)}. Aktif (lolos FVG/filter): {cr.get('aktif_count',0)}. Limit terfill: {cr.get('filled_count',0)}.
    Gugur — tanpa FVG: {cr.get('tanpa_fvg_count',0)}, disentuh sebelum FVG: {cr.get('disentuh_count',0)}, level berulang: {cr.get('ulang_count',0)},
    warm-up: {cr.get('warmup_count',0)}, filter EMA: {cr.get('ema_count',0)}; M5 — tanpa sentuh: {cr.get('no_touch_count',0)},
    tanpa IFVG: {cr.get('no_ifvg_count',0)}, limit tak terfill: {cr.get('no_fill_count',0)}, SL tidak valid: {cr.get('sl_invalid_count',0)}.
    Sinyal terblokir saat entry — slot: {cr.get('blocked_by_slot',0)}, margin: {cr.get('blocked_by_margin',0)},
    min order: {cr.get('blocked_by_min_order',0)}, posisi searah masih aktif: {cr.get('blocked_by_dup',0)}.
    <br>Unduh semua trade: <a href="/trades.csv">/trades.csv</a> &nbsp;|&nbsp;
    Log mentah: <a href="/logs">/logs</a>
  </div>
'''
    else:
        title_html = "📊 Backtest strategi baru (S&amp;R H1 berFVG + BOS + multi-acuan + MSS M5)"
        note_html = f'''  <div class="note">
    💡 <b>Strategi baru (S&amp;R H1 berFVG &rarr; break-out/break-in &rarr; BOS &rarr; multi-acuan &rarr; MSS M5)</b>:
    <br>• <b>Level</b> (H1): c1+c2 berlawanan arah, level = ujung wick terjauh c1/c2; c3 &amp; c4 tidak menembus level; wajib FVG di c1-c3 atau c2-c4.
    <b>Break-out</b> = close H1 menembus keluar, <b>break-in</b> = close H1 kembali (candle yang cuma menyentuh dengan wick wajib diikuti candle break, kalau tidak batal).
    <br>• <b>Titik terjauh</b> (high tertinggi/low terendah c1 s/d sebelum break-out) harus swing {NS_SWING_EXT} kiri-kanan; wajib ada FVG di sekitar break-in (atau break-out);
    <b>BOS</b> = close H1 melewati titik terjauh. Batal kalau sebelum BOS terjadi breakout ulang atau sweep titik terjauh lalu menyentuh level lagi.
    <br>• <b>Acuan</b> = semua level baru berFVG sejenis yang terbentuk setelah break-in dan berada di range harga level dibreak s/d titik terjauh
    (range {'melebar mengikuti titik terjauh berjalan' if NS_RANGE_MELEBAR else 'tetap sampai titik terjauh saat BOS'}). Acuan cukup tersentuh di M5 (wick boleh) &rarr; titik BOS dikunci &rarr; tunggu
    <b>MSS</b> M5 (close menembus swing {NS_MSS_SWING} kiri-kanan terakhir sebelum low/high terjauh sejak sentuh) &rarr; <b>entry {'limit di FVG terdekat MSS (titik ' + NS_FVG_EDGE + ')' if NS_ENTRY_MODE != 'market' else 'market di close candle MSS'}</b>.
    <br>• <b>SL</b> = ujung MSS (low terendah / high tertinggi sejak sentuh) {('+' + format(NS_SL_BUF*100, '.2f') + '%')} ; <b>TP</b> = titik BOS terkunci. Setelah SL tunggu acuan lain; setup selesai kalau TP,
    BOS usai kalau harga kembali ke titik BOS sebelum entry, mati kalau close H1 menembus titik terjauh antara break-out &amp; break-in level utama. <b>Tanpa expired.</b>
    <br>⚙️ Risk {RISK_PCT*100:.0f}% dari balance (compounding). Slot maksimum: {_fmt_max_concurrent()}. Fee masuk {FEE_ENTRY_PCT*100:.3f}% / keluar {FEE_EXIT_PCT*100:.3f}%.
    <br>Level: {ns.get('levels',0)} &bull; break-out+in: {ns.get('break_out_in',0)} (batal wick keluar/masuk: {ns.get('wick_batal_keluar',0)}/{ns.get('wick_batal_masuk',0)};
    bukan swing: {ns.get('terjauh_bukan_swing',0)}; tanpa FVG: {ns.get('no_fvg',0)}) &bull; BOS: {ns.get('bos',0)} (batal sebelum BOS: {ns.get('batal_sebelum_bos',0)}) &bull;
    Setup selesai TP: {ns.get('selesai_TP',0)}, BOS usai: {ns.get('bos_selesai',0)}, level ditembus lagi: {ns.get('level_ditembus_lagi',0)},
    tanpa acuan: {ns.get('tanpa_acuan',0)}, belum selesai: {ns.get('pending',0)+ns.get('terbuka',0)} &bull; Entry: {ns.get('entry',0)}, MSS tanpa FVG: {ns.get('tanpa_fvg',0)}.
    Sinyal terblokir saat entry — slot: {cr.get('blocked_by_slot',0)}, margin: {cr.get('blocked_by_margin',0)},
    min order: {cr.get('blocked_by_min_order',0)}, posisi searah masih aktif: {cr.get('blocked_by_dup',0)}.
    <br>Unduh: <a href="/trades.csv">/trades.csv</a> &nbsp;|&nbsp; <a href="/setups.txt">/setups.txt</a> (hasil per setup, harga saja) &nbsp;|&nbsp;
    Log mentah: <a href="/logs">/logs</a>
  </div>
'''

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
  <h1>{title_html}</h1>
  {status_html}

  <div class="cards">
    <div class="card"><div class="label">Total Trade</div><div class="value">{cr['n_trades']}</div></div>
    <div class="card"><div class="label">Win Rate</div><div class="value">{cr['wr']:.1f}%</div></div>
    <div class="card"><div class="label">Total R</div><div class="value {'pos' if cr['total_r']>=0 else 'neg'}">{cr['total_r']:+.2f}</div></div>
    <div class="card"><div class="label">Avg R/Trade</div><div class="value {'pos' if cr['avg_r']>=0 else 'neg'}">{cr['avg_r']:+.2f}</div></div>
    <div class="card"><div class="label">Balance Akhir</div><div class="value">${cr['final_balance']:.2f}</div></div>
    <div class="card"><div class="label">ROI</div><div class="value {'pos' if cr['roi']>=0 else 'neg'}">{cr['roi']:+.1f}%</div></div>
  </div>

  {note_html}

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
    """TP -> 'TP +1.6R' (R bersih setelah fee); SL -> 'SL' (+ R bersih kalau bukan -1R persis, mis. gap)."""
    if t['reason'] == 'TP':
        return f"TP {t['r_mult']:+.1f}R"
    return f"SL {t['r_mult']:+.1f}R"


CSV_COLUMNS = ['Koin', 'Dir', 'Hasil', 'Level', 'Terbentuk', 'Touch', 'IFVG selesai', 'Limit IFVG', 'Fill', 'SL',
               'Fill time', 'Exit', 'Exit time']
CSV_COLUMNS_NS = ['Koin', 'Dir', 'Hasil', 'Level', 'Terbentuk', 'Break out', 'Break in', 'Terjauh', 'BOS', 'Acuan', 'Touch acuan',
                  'MSS', 'Entry', 'SL', 'TP', 'Entry time', 'Exit', 'Exit time']


def _trades_csv() -> bytes:
    """trades.csv ringkas, waktu WIB (UTC+7). Terbentuk = open candle H1 c1; Touch = candle M5 pertama menyentuh level;
    IFVG selesai = candle M5 x3 (FVG searah selesai); Limit IFVG = harga limit; Fill = harga fill; Exit = harga exit."""
    with _lock:
        trades_cp = list(_all_trades)
    buf = io.StringIO()
    writer = csv.writer(buf)
    if STRATEGY != 'ifvg_ema':
        writer.writerow(CSV_COLUMNS_NS)
        for t in trades_cp:
            mt = t.get('meta') or {}
            writer.writerow([
                t['symbol'], t['direction'].upper(), _hasil_str(t), _fmt_price(t['level']),
                _fmt_wib_short(t['level_formed_ts']), _fmt_wib_short(mt.get('bo_ts')), _fmt_wib_short(mt.get('bi_ts')),
                _fmt_price(mt.get('ext', 0)), _fmt_wib_short(mt.get('bos_ts')), _fmt_price(mt.get('acuan', 0)),
                _fmt_wib_short(t['test1_ts']), _fmt_wib_short(t['test2_ts']), _fmt_price(t['entry']), _fmt_price(t['sl']),
                _fmt_price(mt.get('tp', 0)), _fmt_wib_short(t['entry_ts']), _fmt_price(t['exit']), _fmt_wib_short(t['exit_ts']),
            ])
        return buf.getvalue().encode('utf-8')
    writer.writerow(CSV_COLUMNS)
    for t in trades_cp:
        mt = t.get('meta') or {}
        writer.writerow([
            t['symbol'], t['direction'].upper(), _hasil_str(t), _fmt_price(t['level']),
            _fmt_wib_short(t['level_formed_ts']), _fmt_wib_short(t['test1_ts']), _fmt_wib_short(t['test2_ts']),
            _fmt_price(mt.get('ifvg_entry', t['entry'])), _fmt_price(t['entry']), _fmt_price(t['sl']),
            _fmt_wib_short(t['entry_ts']), _fmt_price(t['exit']), _fmt_wib_short(t['exit_ts']),
        ])
    return buf.getvalue().encode('utf-8')


def _fmt_setup_t(ts_ms):
    """'06-16 7:00' (WIB, jam tanpa nol di depan) -- sama dgn format file setup .txt."""
    dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc) + timedelta(hours=7)
    return f"{dt.month:02d}-{dt.day:02d} {dt.hour}:{dt.minute:02d}"


_SETUP_NOTE = {
    'level_ditembus_lagi': 'level ditembus lagi', 'selesai_TP': '', 'pending': 'acuan belum tersentuh / belum ada entry', 'selesai_SL': '',
    'tanpa_acuan': 'tidak ada level acuan dalam range', 'terbuka': 'posisi masih terbuka',
    'tanpa_fvg': 'MSS terjadi tapi tidak ada FVG di belakang MSS (sampai 1 candle setelahnya)',
    'maks_entry': 'sudah mencapai batas maksimal entry per BOS',
    'gugur_ke_titik_BOS': 'gugur: harga lanjut ke titik BOS', 'bos_selesai': 'BOS usai: harga kembali ke titik BOS setelah acuan tersentuh',
}


def _setups_txt() -> bytes:
    """Satu blok per entry (trade) hasil setup: Coin, Dir, Hasil, Level, Tanggal level, Acuan (waktu candle H1 level acuan),
    P (ujung terjauh M5 setelah acuan tersentuh: high utk Short / low utk Long) + tanggalnya, Swing (swing M5 yang ditembus
    saat MSS) + tanggalnya, Mss tanggal (candle M5 MSS). Hasil = R berdasarkan HARGA saja (TP +x.x / SL -1.0).
    Setup tanpa entry tetap 1 blok: Hasil = keterangan status, field acuan/P/Swing/Mss dikosongkan. Waktu WIB.\n    Mode entry limit (NS_ENTRY_MODE=limit_fvg) menambah 3 baris: FVG (batas bawah - atas zona), FVG tanggal (candle c1), Entry (harga limit)."""
    with _lock:
        rows = list(_setups_all)
    rows.sort(key=lambda r: (r['symbol'], r['bos_ts']))
    out = []
    lim = (NS_ENTRY_MODE != 'market')
    def blok(r, hasil, ac='', p='', pt='', sw='', swt='', mt='', fv='', fvt='', en=''):
        b_ = (f"Coin : {r['symbol'].lower()}\nDir : {r['d'].lower()}\nHasil : {hasil}\nLevel : {_fmt_price(r['L'])}\n"
              f"Tanggal level : {_fmt_setup_t(r['c1_ts'])}\nAcuan : {ac}\nP : {p}\nP tanggal : {pt}\n"
              f"Swing : {sw}\nSwing tanggal : {swt}\nMss tanggal : {mt}\n")
        if lim:     # mode entry limit di FVG: FVG (zona + waktu c1) dan harga limit
            b_ += f"FVG : {fv}\nFVG tanggal : {fvt}\nEntry : {en}\n"
        return b_
    for r in rows:
        trs = r['trades']
        if not trs:
            out.append(blok(r, _SETUP_NOTE.get(r['status'], r['status'])))
            continue
        for t in trs:
            dist = abs(t['entry'] - t['sl'])
            if t['why'] == 'SL':
                hasil = 'SL -1.0'
            elif t['why'] == 'TP':
                hasil = f"TP {abs(t['tp'] - t['entry']) / dist if dist > 0 else 0.0:+.1f}"
            else:
                hasil = 'Open'
            fv = t.get('fvg')
            out.append(blok(r, hasil, _fmt_setup_t(t['ac_ts']) if t.get('ac_ts') else '', _fmt_price(t['p']), _fmt_setup_t(t['p_ts']),
                            _fmt_price(t['sw']), _fmt_setup_t(t['sw_ts']), _fmt_setup_t(t['mss_ts']),
                            (f"{_fmt_price(fv['lo'])} - {_fmt_price(fv['hi'])}" if fv else ''),
                            (_fmt_setup_t(fv['c1_ts']) if fv else ''), _fmt_price(t['entry'])))
    return ('\n'.join(out) if out else 'Belum ada setup (backtest belum selesai atau STRATEGY=ifvg_ema).\n').encode('utf-8')


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

        if path == '/setups.txt':
            body = _setups_txt()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
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
