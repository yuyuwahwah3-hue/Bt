"""
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

EMA_BIAS_FILTER  = os.environ.get('EMA_BIAS_FILTER', '1').strip() not in ('0', 'false', 'False', '')
BIAS_EMA_FAST    = int(os.environ.get('BIAS_EMA_FAST', 21))
BIAS_EMA_SLOW    = int(os.environ.get('BIAS_EMA_SLOW', 30))
EMA_WARMUP       = int(os.environ.get('EMA_WARMUP', 90))             # level dgn candle FVG ke-k < ini dibuang
FVG_LAST_C       = int(os.environ.get('FVG_LAST_C', 4))              # FVG H1 dicari di C1..C{FVG_LAST_C}
IFVG_LOOKBACK    = int(os.environ.get('IFVG_LOOKBACK', 48))          # FVG tidak searah maks N candle M5 sebelum sentuh

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
    """Target aktif di candle now_ts: level lawan yang sudah aktif (act<=now), belum tersentuh (ft>=now); pilih c1 TERBARU."""
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
                    if pos['entry_ts'] < now_ts:
                        T = _current_target(pos, now_ts)
                        if T is not None and hi >= T:
                            close_trade(key, max(T, op), 'TP', now_ts)
                else:  # Short
                    if hi >= S * (1 - 1e-9):
                        close_trade(key, max(S, op), 'SL', now_ts)
                        continue
                    if pos['entry_ts'] < now_ts:
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
        _log_msg(f"🚀 Mulai backtest SNR (level H1 + FVG H1 + bias EMA{BIAS_EMA_FAST}/{BIAS_EMA_SLOW} + Inverted FVG M5, exit target level lawan) — {len(SYMBOLS)} koin, {BACKTEST_START_DATE} s/d {BACKTEST_END_DATE}")
        _log_msg(f"   Level c1/c2 (c3 bersih) + FVG H1 di C1..C{FVG_LAST_C} + level tidak berulang sebelum FVG + bias EMA (filter={'ON' if EMA_BIAS_FILTER else 'OFF'}, warm-up {EMA_WARMUP}) "
                  f"-> sentuh -> IFVG M5 (lookback {IFVG_LOOKBACK}) -> LIMIT di IFVG, SL = kedalaman terjauh sejak sentuh, tanpa expired. "
                  f"Exit: TARGET = wick level lawan terbaru yang belum tersentuh (H1+FVG, tanpa EMA); SL dicek duluan.")

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
            _log_msg(f"   ✅ {symbol}: {cp['n']} candle H1, {st['levels']} level, {st['aktif']} aktif, {st['entry']} limit terfill ({kind_str or '-'}); "
                      f"gugur: tanpa FVG {st['tanpa_fvg']}, disentuh sebelum FVG {st['sudah_disentuh']}, level berulang {st['ulang']}, warm-up {st['warmup']}, "
                      f"EMA {st['ema_ditolak']}; M5: tanpa sentuh {st['no_touch']}, tanpa IFVG {st['no_ifvg']}, limit tak terfill {st['no_fill']}, SL tidak valid {st['sl_invalid']}.")

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
                'aktif_count': sum_st('aktif'), 'tanpa_fvg_count': sum_st('tanpa_fvg'),
                'disentuh_count': sum_st('sudah_disentuh'), 'ulang_count': sum_st('ulang'),
                'warmup_count': sum_st('warmup'), 'ema_count': sum_st('ema_ditolak'),
                'no_touch_count': sum_st('no_touch'), 'no_ifvg_count': sum_st('no_ifvg'),
                'no_fill_count': sum_st('no_fill'), 'sl_invalid_count': sum_st('sl_invalid'),
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
  <h1>📊 Backtest SNR (H1 level + FVG + EMA{BIAS_EMA_FAST}/{BIAS_EMA_SLOW} bias + IFVG M5, target level lawan)</h1>
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


def _trades_csv() -> bytes:
    """trades.csv ringkas, waktu WIB (UTC+7). Terbentuk = open candle H1 c1; Touch = candle M5 pertama menyentuh level;
    IFVG selesai = candle M5 x3 (FVG searah selesai); Limit IFVG = harga limit; Fill = harga fill; Exit = harga exit."""
    with _lock:
        trades_cp = list(_all_trades)
    buf = io.StringIO()
    writer = csv.writer(buf)
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
