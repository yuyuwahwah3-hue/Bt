"""
backtest_web.py — Backtest S&R H1 + FVG H1 + bias EMA21/30 + konfirmasi S&R M5 berFVG -> IFVG, exit TP = pergerakan terjauh
=============================================================================================================
RINGKASAN STRATEGI (Long; Short = cermin)
1) LEVEL H1 (basis body): c1 bearish, c2 bullish, level = close[c1]; c3 tidak boleh menyentuh level (low[c3] > level).
   Tanpa EMA cross, tanpa TEST1/TEST2, tanpa RSI.
2) FVG H1 searah di C1..C4 (env FVG_LAST_C): 3 candle berurutan (a,b,k=a+2; k <= c1+3) dgn gap low[k] > high[a] dan close[k] > high[b]
   (arah candle bebas). Level AKTIF saat candle k closed. Level gugur kalau sejak c4 s/d candle k harga menyentuh level.
3) Filter level-berulang: kalau di antara c3 s/d candle penutup FVG muncul lagi pasangan 2-candle searah (bearish->bullish
   utk Long) -> level awal tidak valid.
4) Filter bias EMA21 vs EMA30 (H1, hanya level awal; target exit TIDAK difilter): Long valid kalau EMA21 > EMA30, Short kalau
   EMA21 < EMA30. BIAS_AT=touch (default): bias dihitung SAAT LEVEL TERSENTUH (candle H1 terakhir yg closed sebelum sentuh), bukan saat level
   terbentuk. Jadi EMA cross di antara level terbentuk s/d tersentuh ikut menentukan: support yg terbentuk saat EMA21 < EMA30 tetap valid
   kalau kemudian EMA21 cross di atas EMA30 dan harga kembali menyentuh level; sebaliknya level yg bias-nya benar saat terbentuk gugur kalau
   bias berbalik sebelum sentuh. BIAS_AT=act = dihitung saat level aktif (candle FVG selesai; versi lama). Warm-up EMA_WARMUP (90) candle.
4b) Filter sentuh EMA21 (LEVEL_EMA_TOUCH=off default | both | any): candle c1 DAN c2 dari level H1 harus menyentuh EMA21 (BIAS_EMA_FAST) candle itu
   (low <= EMA <= high). 'any' = cukup salah satu. Level dgn c1 < EMA_WARMUP dibuang. Hanya level awal; target exit TIDAK difilter.
4d) VALIDASI SWING (SWING_VALID=1 default): setelah level terbentuk, dari c1 mundur (c1, c1-1, c1-2, ...) cari SWING kuat terdekat: Long -> high
   (wick) tertinggi, Short -> low terendah, yg tidak dilampaui candle mana pun dlm SWING_N=5 candle H1 kiri DAN 5 kanan (pivot harus sudah
   terkonfirmasi sebelum level tersentuh). Level VALID hanya kalau swing itu di-BREAK BODY (close H1 melewatinya) SEBELUM level tersentuh.
   Tidak di-break = tidak valid; hanya SWEEP wick (high/low menembus tapi close tidak) = tidak valid. SWING_SWEEP_FAIL=1 (default): kalau candle
   PERTAMA yg menembus swing cuma sweep -> gagal walau kemudian ada break body. Target exit TIDAK difilter.
4f) SLOPE EMA30 SEARAH SAAT SENTUH (SLOPE_FILTER=1 default, SLOPE_LB=10): saat level tersentuh, slope EMA30 H1 harus searah: Long -> EMA30 naik, Short -> turun.
   slope = EMA30[i] - EMA30[i-SLOPE_LB], i = candle H1 terakhir yg sudah closed sebelum candle sentuh (tanpa lookahead). Berbeda dgn bias EMA21/30 yg
   dicek saat level aktif. Hanya level awal; target exit TIDAK difilter.
4e) INDUCEMENT (IDM_FILTER=1 default, IDM_TYPE=same|opposite|any): level H1 harus punya IDM = level TANPA FVG (pola level c1+c2, c3 bersih) di antara c2 level
   s/d CANDLE TERTINGGI (Short: terendah) = puncak pergerakan setelah c2 sebelum harga menyentuh level lagi. Urutan Long: support berFVG -> IDM ->
   candle tertinggi -> sentuh level. IDM boleh terbentuk sebelum ATAU sesudah swing masa lalu di-break (validasi swing 4d terpisah).
4c) Filter TITIK LEVEL vs EMA30 (LEVEL_VS_EMA=off default | c1 | act): Long valid kalau harga level > EMA30, Short kalau level < EMA30.
   c1 = EMA30 di candle c1 (saat level terbentuk); act = EMA30 di candle FVG selesai. Level dgn c1 < EMA_WARMUP dibuang. Target exit TIDAK difilter.
5) LEVEL_MODE=keep (default): SEMUA level disimpan; level yg belum tersentuh tetap aktif walau level searah baru terbentuk, dan bisa
   dipakai kapan pun nanti tersentuh (tanpa expired; tiap level maks 1 trade). LEVEL_MODE=replace: level searah baru menggantikan yg lama.
6) KONFIRMASI M5, ENTRY_MODE=ifvg (default) | m5res | m5sr. m5res (Long, Short = cermin): Setelah level H1 TERSENTUH (low M5 <= level):
   a) RESISTEN M5 berFVG (c1 bullish, c2 bearish, R = close c1, c3 bersih, FVG bearish C1..C4). Dicari mundur maks M5SR_LOOKBACK=48
      candle M5 sebelum candle sentuh (tidak sebelum level H1 aktif) dan seterusnya; yg belum ada ditunggu.
   b) tunggu R di-BREAK BODY: candle M5 pertama setelah FVG selesai DAN setelah/saat sentuh yg CLOSE > R (break sebelum sentuh tidak dihitung).
   c) ENTRY LIMIT di level R (retest): fill = candle M5 pertama setelah candle break dgn low <= R, harga fill = min(R, open).
      Resisten baru yg di-break sebelum fill -> limit dipindah ke R baru.
   d) SL = low terendah sejak candle sentuh s/d candle break, DIKURANGI SL_BUFFER_PCT (0.2%).
   ENTRY_MODE=m5sr (versi sebelumnya):
   a) RESISTEN M5 berFVG: c1 bullish, c2 bearish, level R = close c1, c3 tidak menyentuh R; FVG bearish di C1..C4 (aturan sama
      dgn level H1). Mulai dari candle sentuh (M5SR_LOOKBACK=0). Level gugur kalau harga menyentuh R sebelum FVG selesai.
   b) SUPPORT M5 berFVG terbentuk SETELAH resisten aktif (c1 support > candle FVG resisten): c1 bearish, c2 bullish, c3 bersih,
      FVG bullish di C1..C4.
   c) FVG support itu MEM-BREAK R: close candle ke-3 FVG support > R.
   d) Dari pergerakan itu terbentuk IFVG = overlap FVG bullish support dgn FVG bearish resisten (bagian overlap belum tersentuh
      sebelum FVG support mulai). ENTRY LIMIT di tepi atas overlap. IFVG baru sebelum fill -> limit dipindah.
   e) SL = low terdalam sejak candle sentuh s/d FVG support selesai, DIKURANGI buffer SL_BUFFER_PCT (0.2%).
   ENTRY_MODE=ifvg (default): FVG M5 searah 'mengambil' FVG M5 tidak searah (IFVG_LOOKBACK=48), SL tanpa buffer. ENTRY: IFVG_ENTRY=ifvg (default) = limit di
   tepi atas overlap IFVG. Opsi lain, IFVG_ENTRY=level:
   saat IFVG terbentuk (x3 closed), kalau IFVG ada DI ATAS level H1 (tepi atas overlap > level) -> LIMIT di level H1; kalau IFVG ada di
   BAWAH level (tepi atas <= level) -> MARKET order di open candle M5 berikutnya. IFVG_ENTRY=ifvg = limit di tepi atas overlap (versi lama); IFVG_ENTRY=market = selalu market di open candle setelah x3; mix = di atas level limit IFVG, di bawah level market.
7) EXIT. TP_MODE=rr: TP TETAP = entry +/- TP_RR (2.0) x |entry - SL| (RR 1:2). TP_MODE=far + MIN_RR=2: TP gerak terjauh tapi setup dgn jarak TP < 2 x jarak SL
   digugurkan (MIN_RR_MODE=drop, default) ATAU MIN_RR_MODE=extend: TP gerak terjauh tapi minimal 1:2 -> kalau jaraknya < 2 x jarak SL, TP DIPERBESAR
   sampai tepat 1:2 (TP = max(gerak terjauh, entry +/- 2 x |entry-SL|); TP terjauh yg tak valid/di sisi rugi juga diperbesar). (Opsi lama: TP_MODE=opp = TARGET LEVEL LAWAN: ujung wick level lawan H1+FVG yg belum tersentuh, dinamis, tanpa EMA;
   TARGET_PICK=farthest (default) -> TERJAUH di sisi profit dari entry, nearest -> terdekat, newest -> c1 terbaru; wick yg sudah tersentuh =
   terpakai, dilewati. TP_MODE=far = pergerakan terjauh H1 (lihat bawah).
   TP_MODE=far: tanpa trailing. TP = PERGERAKAN TERJAUH (basis H1) setelah level terbentuk sampai harga kembali menyentuh level
   (Long: high tertinggi candle H1 dari c2 s/d SEBELUM candle H1 yang menyentuh level; Short: low terendah). Contoh Long: level 0.18,
   naik sampai 0.21, balik ke 0.18 -> TP 0.21. TP tetap (statis) sejak entry. Kalau TP tidak berada di sisi profit dari
   harga fill -> setup dibuang (tp_invalid). SL dicek duluan dalam candle yang sama; TP tidak dicek di candle fill.
   Posisi yg belum selesai di akhir data tidak dihitung.
Fee: masuk FEE_ENTRY_PCT (limit), keluar FEE_EXIT_PCT. Risk RISK_PCT dari balance (compound), 1 posisi per arah per koin.

Deploy ke Railway: Start command -> python backtest_web.py ; buka domain Railway untuk lihat progress & hasil.
Periode default: 1 tahun ke belakang (override: BACKTEST_START_DATE / BACKTEST_END_DATE).
Env tambahan: FVG_LAST_C=4, EMA_BIAS_FILTER=1/0, BIAS_AT=touch|act, BIAS_EMA_FAST=21, BIAS_EMA_SLOW=30, EMA_WARMUP=90, IFVG_LOOKBACK=48,
ENTRY_MODE=m5sr|ifvg, M5SR_LOOKBACK=0, SL_BUFFER_PCT=0.002.
trades.csv (WIB): Koin, Dir, Hasil ('TP +x.xR' / 'SL'), Level, Terbentuk, Touch, Setup selesai, Limit, Fill, SL, TP, Fill time, Exit, Exit time.
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
BIAS_AT          = os.environ.get('BIAS_AT', 'touch').strip().lower()   # bias EMA21/30 dihitung: 'touch' = saat level TERSENTUH (cross sejak level terbentuk ikut dihitung) | 'act' = saat level aktif (lama)
BIAS_EMA_FAST    = int(os.environ.get('BIAS_EMA_FAST', 21))
BIAS_EMA_SLOW    = int(os.environ.get('BIAS_EMA_SLOW', 30))
EMA_WARMUP       = int(os.environ.get('EMA_WARMUP', 90))             # level dgn candle FVG ke-k < ini dibuang
FVG_LAST_C       = int(os.environ.get('FVG_LAST_C', 4))              # FVG H1 dicari di C1..C{FVG_LAST_C}
IFVG_LOOKBACK    = int(os.environ.get('IFVG_LOOKBACK', 48))          # FVG tidak searah maks N candle M5 sebelum sentuh
IFVG_ENTRY       = os.environ.get('IFVG_ENTRY', 'ifvg').strip().lower()   # mode ifvg: 'level' = IFVG di atas level -> LIMIT di level H1, IFVG di bawah level -> MARKET | 'mix' = di atas level tetap limit di tepi IFVG, di bawah level MARKET | 'market' = SELALU market di open candle M5 setelah IFVG selesai | 'ifvg' = limit di tepi IFVG (lama)
ENTRY_MODE       = os.environ.get('ENTRY_MODE', 'ifvg').strip().lower()  # 'm5res' (resisten M5 berFVG di-break body, entry di level) | 'm5sr' (S&R M5 -> IFVG) | 'ifvg' (IFVG biasa)
M5SR_LOOKBACK    = int(os.environ.get('M5SR_LOOKBACK', 48))          # mode m5res/m5sr: level M5 boleh mulai maks N candle M5 SEBELUM sentuh (0 = hanya mulai dari candle sentuh)
SLOPE_FILTER     = os.environ.get('SLOPE_FILTER', '1').strip() not in ('0', 'false', 'False', 'off', '')   # slope EMA30 harus searah saat level tersentuh
SLOPE_LB         = int(os.environ.get('SLOPE_LB', 10))                # slope = EMA30 candle H1 terakhir yg closed sebelum sentuh - EMA30 N candle sebelumnya
IDM_FILTER       = os.environ.get('IDM_FILTER', '0').strip() not in ('0', 'false', 'False', 'off', '')   # level H1 harus punya inducement (IDM)
IDM_TYPE         = os.environ.get('IDM_TYPE', 'same').strip().lower()   # 'same' = IDM searah level (Long: support kecil) | 'opposite' | 'any'
SWING_VALID      = os.environ.get('SWING_VALID', '1').strip() not in ('0', 'false', 'False', 'off', '')   # validasi level via swing high/low yg di-break
SWING_N          = int(os.environ.get('SWING_N', 5))                  # swing kuat: tak ada yg lebih tinggi/rendah dlm N candle H1 kiri & kanan
SWING_SWEEP_FAIL = os.environ.get('SWING_SWEEP_FAIL', '1').strip() not in ('0', 'false', 'False', 'off', '')   # candle pertama yg menembus swing hanya sweep -> gagal walau nanti ada break
SWING_MAX_BACK   = int(os.environ.get('SWING_MAX_BACK', 300))         # cari swing mundur maks N candle H1 dari c1
LEVEL_VS_EMA     = os.environ.get('LEVEL_VS_EMA', 'off').strip().lower()  # titik level vs EMA30: 'c1' = EMA di candle c1 | 'act' = EMA di candle FVG selesai | 'off'
LEVEL_EMA_TOUCH  = os.environ.get('LEVEL_EMA_TOUCH', 'off').strip().lower()  # filter level: 'both' = c1 DAN c2 harus menyentuh EMA21 | 'any' = salah satu | 'off'
LEVEL_MODE       = os.environ.get('LEVEL_MODE', 'keep').strip().lower()  # 'keep' = semua level disimpan, aktif sampai tersentuh | 'replace' = level searah baru menggantikan yg lama
TARGET_PICK      = os.environ.get('TARGET_PICK', 'farthest').strip().lower()  # TP_MODE=opp: 'farthest' = level lawan terjauh yg belum tersentuh | 'nearest' = terdekat | 'newest' = c1 terbaru
TP_RR            = float(os.environ.get('TP_RR', 2.0))              # TP_MODE=rr: TP tetap = entry +/- TP_RR x |entry-SL| (2.0 = RR 1:2)
MIN_RR           = float(os.environ.get('MIN_RR', 0))               # TP_MODE=far: setup digugurkan kalau jarak TP < MIN_RR x |entry-SL| (0 = nonaktif; 2.0 = RR minimal 1:2)
MIN_RR_MODE      = os.environ.get('MIN_RR_MODE', 'drop').strip().lower()   # MIN_RR>0: 'drop' = gugurkan setup RR kurang | 'extend' = perbesar TP sampai RR = MIN_RR
TP_MODE          = os.environ.get('TP_MODE', 'far').strip().lower()  # 'far' = pergerakan terjauh (H1) | 'rr' = TP tetap RR 1:TP_RR | 'opp' = level lawan (dinamis, H1+FVG)
SL_BUFFER_PCT    = float(os.environ.get('SL_BUFFER_PCT', '0.002'))   # mode m5res/m5sr: SL = pergerakan terjauh + buffer 0.2%

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
    'ulang_count': 0, 'warmup_count': 0, 'ema_count': 0, 'bias_touch_count': 0, 'bias_pulih_count': 0, 'ema_touch_count': 0, 'ema_level_count': 0, 'swing_invalid_count': 0, 'idm_none_count': 0, 'slope_count': 0, 'swing_not_broken_count': 0, 'swing_sweep_count': 0, 'swing_no_pivot_count': 0, 'no_touch_count': 0, 'no_ifvg_count': 0,
    'no_fill_count': 0, 'sl_invalid_count': 0, 'tp_invalid_count': 0, 'rr_kurang_count': 0, 'rr_diperbesar_count': 0,
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
# DETEKSI STRATEGI (H1: level + FVG + bias EMA | M5: resisten berFVG di-break / S&R berFVG -> Inverted FVG)
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
     4) FVG searah 'mengambil' gap tidak searah = zona saling overlap. IFVG = overlap; tepi ATAS overlap = min(atas zona tidak searah,
        atas zona searah) (Short: tepi bawah). IFVG_ENTRY=ifvg: ENTRY limit di tepi itu. IFVG_ENTRY=level (default): IFVG di atas level
        (tepi atas > level) -> LIMIT di level H1; IFVG di bawah level -> MARKET di open candle M5 setelah x3. Dipasang saat x3 closed.
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
        if f.get('mkt'):
            return s0                                                   # market: langsung di open candle setelah x3
        hit = l_[s0:last_idx + 1] <= f['ent'] + EPS
        return s0 + int(hit.argmax()) if hit.any() else None

    def _result(f, r):
        fp = float(o_[r]) if f.get('mkt') else min(f['ent'], float(o_[r]))
        if fp <= f['sl']:
            return 'sl_invalid', None
        z_lo = max(f['bb'], f['ub'])
        return 'fill', dict(touch=a + k, fill=a + r, x1=a + f['x1'], x3=a + f['q'], y1=a + f['y1'], y3=a + f['y3'],
                            etype=f.get('etype', 'limit_ifvg'), ifvg_top=_price(d, Lv, f['top']),
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
        top = min(float(bt[j]), ut)                                     # tepi atas overlap IFVG
        f = dict(q=q, x1=x1, y1=y1, y3=y3, ent=top, top=top, sl=float(l_[k:q + 1].min()),
                 bb=float(bb[j]), bt=float(bt[j]), ub=ub, ut=ut, n=n_ifvg, etype='limit_ifvg')
        if IFVG_ENTRY == 'market':                                      # selalu market di open candle setelah x3
            f['mkt'] = True; f['etype'] = 'market'
        elif IFVG_ENTRY in ('level', 'mix'):
            if top > EPS:                                               # IFVG di ATAS level
                if IFVG_ENTRY == 'level':
                    f['ent'] = 0.0; f['etype'] = 'limit_level'          # level: limit di level H1 | mix: tetap limit di tepi IFVG
            else:                                                       # IFVG di BAWAH level -> market order
                f['mkt'] = True; f['etype'] = 'market'
        if pend is not None:                                            # limit lama masih hidup sampai candle q (inklusif)
            r = _fill_in(pend, q)
            if r is not None:
                return _result(pend, r)
        pend = f
        if f.get('mkt'):                                                # market: tidak menunggu, eksekusi di open candle q+1
            if q + 1 < m:
                return _result(f, q + 1)
            return 'no_fill', None
    if pend is None:
        return 'no_ifvg', None
    r = _fill_in(pend, m - 1)
    if r is not None:
        return _result(pend, r)
    return 'no_fill', None


def _m5_fvg(o, h, l, c, i, d, n):
    """FVG searah level M5 di C1..C{FVG_LAST_C} (aturan sama dgn activate_level H1). d='Long' = FVG bullish (support),
    d='Short' = FVG bearish (resisten), pada koordinat relatif (sudah dicerminkan). Return (a, k) atau None."""
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
            return (a_, k)
    return None


def _m5_levels(o, h, l, c, lo):
    """Support & resisten M5 berFVG pada koordinat relatif (c1 >= lo). Level = close c1, c3 bersih, FVG di C1..C4,
    gugur kalau harga menyentuh level antara c4 s/d candle penutup FVG. Return (list_support, list_resisten);
    tiap item: dict(i=c1, a, k, L, bot, top) dgn zona FVG [bot, top]."""
    m = len(c)
    sup, res = [], []
    if m < 6:
        return sup, res
    idx = np.arange(lo, m - 4)
    if len(idx) == 0:
        return sup, res
    sm = (c[idx] < o[idx]) & (c[idx + 1] > o[idx + 1]) & (l[idx + 2] > c[idx] + WICK_EPS)
    rm = (c[idx] > o[idx]) & (c[idx + 1] < o[idx + 1]) & (h[idx + 2] < c[idx] - WICK_EPS)
    for i in idx[sm]:
        i = int(i); fv = _m5_fvg(o, h, l, c, i, 'Long', m)
        if fv is None:
            continue
        a_, k = fv; L = float(c[i])
        if (l[i + 3:k + 1] <= L + WICK_EPS).any():
            continue
        sup.append(dict(i=i, a=a_, k=k, L=L, bot=float(h[a_]), top=float(l[k])))
    for i in idx[rm]:
        i = int(i); fv = _m5_fvg(o, h, l, c, i, 'Short', m)
        if fv is None:
            continue
        a_, k = fv; L = float(c[i])
        if (h[i + 3:k + 1] >= L - WICK_EPS).any():
            continue
        res.append(dict(i=i, a=a_, k=k, L=L, bot=float(h[k]), top=float(l[a_])))
    return sup, res


def scan_m5sr(lv, e, M):
    """Konfirmasi M5 'S&R M5 berFVG -> IFVG' setelah level H1 aktif sampai indeks e. Long (Short = cermin):
     1) SENTUH: candle M5 pertama dgn low <= level H1 (indeks k).
     2) RESISTEN M5 berFVG (c1 bullish, c2 bearish, R = close c1, c3 bersih, FVG bearish C1..C4) dgn c1 >= k - M5SR_LOOKBACK.
     3) SUPPORT M5 berFVG (c1 bearish, c2 bullish, c3 bersih, FVG bullish C1..C4) yg c1-nya SETELAH FVG resisten selesai.
     4) FVG support MEM-BREAK R: close candle ke-3 FVG support > R.
     5) IFVG = overlap FVG bullish support x FVG bearish resisten (bagian overlap belum tersentuh sejak FVG resisten selesai
        s/d sebelum FVG support mulai). ENTRY limit di tepi ATAS overlap = min(atas zona bearish, atas zona bullish).
     6) SL = low terendah sejak sentuh s/d FVG support selesai, dikurangi SL_BUFFER_PCT (Short: high tertinggi + buffer).
     7) Pasangan baru sebelum limit lama terisi -> limit pindah. Tanpa expired.
    Return (status, info) seperti scan_ifvg; status 'no_ifvg' = tidak ada pasangan resisten-support yg valid."""
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
    sup, res = _m5_levels(o_, h_, l_, c_, max(0, k - M5SR_LOOKBACK))
    if not sup or not res:
        return 'no_ifvg', None

    cands = []
    for s_ in sorted(sup, key=lambda z: z['k']):
        best = None
        for r_ in res:
            if r_['k'] >= s_['i']:                                       # support terbentuk setelah resisten aktif
                continue
            if not (c_[s_['k']] > r_['L'] + EPS):                        # FVG support harus mem-break resisten
                continue
            ub, ut, bb, bt = s_['bot'], s_['top'], r_['bot'], r_['top']
            if not (ub < bt and ut > bb):                                # FVG support 'mengambil' FVG resisten
                continue
            if s_['a'] - 1 >= r_['k'] + 1 and h_[r_['k'] + 1:s_['a']].max() > max(bb, ub) + EPS:
                continue                                                 # overlap sudah tersentuh sebelum FVG support
            if best is None or r_['k'] > best['k']:
                best = r_
        if best is not None:
            cands.append((s_, best))
    if not cands:
        return 'no_ifvg', None

    def _fill_in(f, last_idx):
        s0 = f['q'] + 1
        if s0 > last_idx:
            return None
        hit = l_[s0:last_idx + 1] <= f['ent'] + EPS
        return s0 + int(hit.argmax()) if hit.any() else None

    def _result(f, r):
        fp = min(f['ent'], float(o_[r]))
        fp_p = _price(d, Lv, fp)
        sl_p = _price(d, Lv, f['sl'])
        sl_p = sl_p * (1 - SL_BUFFER_PCT) if d == 'Long' else sl_p * (1 + SL_BUFFER_PCT)
        if (d == 'Long' and fp_p <= sl_p) or (d == 'Short' and fp_p >= sl_p):
            return 'sl_invalid', None
        z_lo = max(f['bb'], f['ub'])
        return 'fill', dict(touch=a + k, fill=a + r, x1=a + f['x1'], x3=a + f['q'], y1=a + f['y1'], y3=a + f['y3'],
                            n_ifvg=f['n'], entry=fp_p, sl=sl_p, ent_lvl=_price(d, Lv, f['ent']), z_lo=_price(d, Lv, z_lo),
                            bear=(_price(d, Lv, f['bb']), _price(d, Lv, f['bt'])),
                            bull=(_price(d, Lv, f['ub']), _price(d, Lv, f['ut'])), res_level=_price(d, Lv, f['rL']))

    pend = None
    n_ifvg = 0
    for s_, r_ in cands:                                                 # urut waktu FVG support selesai
        q = s_['k']
        ub, ut, bb, bt = s_['bot'], s_['top'], r_['bot'], r_['top']
        n_ifvg += 1
        f = dict(q=q, x1=s_['a'], y1=r_['a'], y3=r_['k'], ent=min(bt, ut), sl=float(l_[k:q + 1].min()),
                 bb=bb, bt=bt, ub=ub, ut=ut, rL=r_['L'], n=n_ifvg)
        if pend is not None:
            rr = _fill_in(pend, q)
            if rr is not None:
                return _result(pend, rr)
        pend = f
    rr = _fill_in(pend, m - 1)
    if rr is not None:
        return _result(pend, rr)
    return 'no_fill', None


def scan_m5res(lv, e, M):
    """Konfirmasi M5 'resisten M5 berFVG di-break body -> entry di level resisten'. Long (Short = cermin):
     1) SENTUH: candle M5 pertama dgn low <= level H1 (indeks k).
     2) RESISTEN M5 berFVG (c1 bullish, c2 bearish, R = close c1, c3 bersih, FVG bearish C1..C4), c1 >= k - M5SR_LOOKBACK.
     3) BREAK BODY: candle M5 pertama SETELAH FVG resisten selesai (dan setelah/saat sentuh) yg close > R.
     4) ENTRY limit di R, dipasang saat candle break closed. Fill = candle pertama sesudahnya dgn low <= R,
        harga fill = min(R, open). Resisten baru yg di-break sebelum fill -> limit pindah. Tanpa expired.
     5) SL = low terendah sejak sentuh s/d candle break, dikurangi SL_BUFFER_PCT (Short: high tertinggi + buffer).
    Return (status, info) seperti scan_ifvg; 'no_ifvg' = tidak ada resisten berFVG yg di-break."""
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
    _, res = _m5_levels(o_, h_, l_, c_, max(0, k - M5SR_LOOKBACK))
    cands = []
    for r_ in res:
        j0 = max(r_['k'] + 1, k)                                         # break harus terjadi SETELAH sentuh
        if j0 >= m:
            continue
        hit = np.flatnonzero(c_[j0:] > r_['L'] + EPS)
        if len(hit):
            cands.append((j0 + int(hit[0]), r_))
    if not cands:
        return 'no_ifvg', None
    cands.sort(key=lambda z: (z[0], z[1]['k']))

    def _fill_in(f, last_idx):
        s0 = f['q'] + 1
        if s0 > last_idx:
            return None
        hit = l_[s0:last_idx + 1] <= f['ent'] + EPS
        return s0 + int(hit.argmax()) if hit.any() else None

    def _result(f, r):
        fp = min(f['ent'], float(o_[r]))
        fp_p = _price(d, Lv, fp)
        sl_p = _price(d, Lv, f['sl'])
        sl_p = sl_p * (1 - SL_BUFFER_PCT) if d == 'Long' else sl_p * (1 + SL_BUFFER_PCT)
        if (d == 'Long' and fp_p <= sl_p) or (d == 'Short' and fp_p >= sl_p):
            return 'sl_invalid', None
        ent_p = _price(d, Lv, f['ent'])
        return 'fill', dict(touch=a + k, fill=a + r, x1=a + f['y1'], x3=a + f['q'], y1=a + f['y1'], y3=a + f['y3'],
                            n_ifvg=f['n'], entry=fp_p, sl=sl_p, ent_lvl=ent_p, z_lo=ent_p,
                            bear=(_price(d, Lv, f['bb']), _price(d, Lv, f['bt'])), bull=(ent_p, ent_p), res_level=ent_p)

    pend = None
    n = 0
    for q, r_ in cands:                                                  # urut waktu break
        n += 1
        f = dict(q=q, y1=r_['a'], y3=r_['k'], ent=r_['L'], sl=float(l_[k:q + 1].min()), bb=r_['bot'], bt=r_['top'], n=n)
        if pend is not None:
            rr = _fill_in(pend, q)
            if rr is not None:
                return _result(pend, rr)
        pend = f
    rr = _fill_in(pend, m - 1)
    if rr is not None:
        return _result(pend, rr)
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


def farthest_move_tp(H1, M, c1, touch, d):
    """TP = pergerakan terjauh (basis H1) setelah level terbentuk, sebelum harga kembali menyentuh level.
    Candle H1 sentuh = candle H1 yang memuat candle M5 sentuh (indeks M5 `touch`). Rentang = candle H1 c2 (candle setelah c1,
    saat level terbentuk) s/d SEBELUM candle H1 sentuh. Long -> high tertinggi; Short -> low terendah.
    Return None kalau rentang kosong."""
    ht = int(np.searchsorted(H1['ts'], int(M['TS'][touch]), side='right')) - 1
    s0 = c1 + 1
    if ht <= s0:
        return None
    if d == 'Long':
        return float(H1['h'][s0:ht].max())
    return float(H1['l'][s0:ht].min())


SCAN_WINDOW0 = 3000      # candle M5 (~10 hari): jendela awal scan; diperlebar x4 kalau belum ada fill (hasil identik dgn scan penuh)


def scan_windowed(fn, lv, e, M):
    """Jalankan scan_* bertahap pada jendela yg makin lebar. Fill yg ditemukan di dalam jendela = hasil yg sama dgn scan penuh
    (kandidat setelah jendela baru muncul sesudah fill itu), jadi level yg tersimpan lama tidak membuat scan jadi berat."""
    a = int(np.searchsorted(M['TS'], lv['act_ts'], side='left'))
    W = SCAN_WINDOW0
    while True:
        e2 = min(e, a + W)
        st, inf = fn(lv, e2, M)
        if st in ('fill', 'sl_invalid') or e2 >= e:
            return st, inf
        W *= 4


def swing_check(H1, c1, touch_ts, d):
    """Validasi level lewat SWING sebelum level tersentuh (tanpa lookahead).
    Long: dari c1 mundur cari high (wick) tertinggi 'kuat' = tak ada candle H1 yg lebih tinggi dlm SWING_N candle kiri & SWING_N kanan.
    Short: cermin (low terendah). Pivot hanya dihitung kalau candle kanan ke-N sudah CLOSED sebelum touch_ts.
    Valid kalau ada close H1 yg melewati swing (BREAK body) sebelum touch. Return (ok, alasan):
    'break' | 'not_broken' (tak ditembus) | 'sweep' (wick menembus, close tidak) | 'no_pivot'."""
    N = SWING_N
    ilast = int(np.searchsorted(H1['ts'] + H1_MS, touch_ts, side='right')) - 1      # candle H1 terakhir yg sudah closed sebelum sentuh
    hi = H1['h'] if d == 'Long' else -H1['l']
    cl = H1['c'] if d == 'Long' else -H1['c']
    jmax = min(c1, ilast - N)
    jmin = max(N, c1 - SWING_MAX_BACK)
    for j in range(jmax, jmin - 1, -1):
        if hi[j] >= hi[j - N:j + N + 1].max():
            brk = np.flatnonzero(cl[j + 1:ilast + 1] > hi[j])
            if len(brk):
                if SWING_SWEEP_FAIL:
                    exc = np.flatnonzero(hi[j + 1:ilast + 1] > hi[j])      # candle pertama yg wick-nya menembus swing
                    if exc[0] < brk[0]:
                        return False, 'sweep'                               # menembus dulu lewat sweep (close tidak melewati) -> gagal
                return True, 'break'
            if (hi[j + 1:ilast + 1] > hi[j]).any():
                return False, 'sweep'
            return False, 'not_broken'
    return False, 'no_pivot'


def idm_check(H1, idm_idx, c1, touch_ts, d):
    """Level H1 harus punya INDUCEMENT (IDM). IDM = level tanpa FVG (pola raw_levels: c1+c2, c3 bersih), searah level (IDM_TYPE=same;
    Long -> support kecil, Short -> resisten kecil), opposite = lawan arah, any = keduanya. Urutan Long: support berFVG -> IDM -> candle tertinggi
    -> harga menyentuh level lagi. 'Candle tertinggi' = candle H1 dgn high tertinggi (Short: low terendah) dari SETELAH c2 (c1+2) s/d sebelum
    candle H1 sentuh = puncak pergerakan (BUKAN swing masa lalu). IDM boleh terbentuk sebelum ATAU sesudah swing di-break; syarat: c1 IDM >= c2 level,
    c2 IDM <= candle tertinggi, dan c3 IDM sudah closed sebelum sentuh. Return (ok, alasan): 'ok' | 'none'."""
    ht = int(np.searchsorted(H1['ts'], touch_ts, side='right')) - 1            # candle H1 yg memuat sentuh (belum closed)
    lo_i, hi_i = c1 + 2, ht - 1
    if hi_i < lo_i:
        return False, 'none'
    seg = H1['h'][lo_i:hi_i + 1] if d == 'Long' else H1['l'][lo_i:hi_i + 1]
    jh = lo_i + int(seg.argmax() if d == 'Long' else seg.argmin())            # candle tertinggi/terendah
    types = {'same': [d], 'opposite': ['Short' if d == 'Long' else 'Long'], 'any': ['Long', 'Short']}.get(IDM_TYPE, [d])
    for t in types:
        arr = idm_idx[t]
        k0 = int(np.searchsorted(arr, c1 + 1, side='left'))                    # c1 IDM >= c2 level
        if k0 < len(arr):
            i = int(arr[k0])
            if i + 1 <= jh and i + 2 <= ht - 1:
                return True, 'ok'
    return False, 'none'


def bias_touch_check(ea, eb, H1ts, touch_ts, d, kk):
    """Bias EMA21/30 saat LEVEL TERSENTUH (candle H1 terakhir yg CLOSED sebelum candle sentuh; tanpa lookahead).
    Long valid kalau EMA21 > EMA30, Short kalau EMA21 <= EMA30. Return (ok, pulih): pulih=True kalau bias saat level aktif (candle kk)
    TIDAK searah tapi saat sentuh searah, yaitu ada EMA cross yg memulihkan bias antara level terbentuk s/d tersentuh."""
    il = int(np.searchsorted(H1ts, touch_ts, side='right')) - 2
    if il < EMA_WARMUP:
        return False, False
    ok = (d == 'Long') == bool(ea[il] > eb[il])
    act_ok = (d == 'Long') == bool(ea[kk] > eb[kk])
    return ok, (ok and kk >= EMA_WARMUP and not act_ok)        # kk < warm-up: bias awal tak bisa dinilai -> tidak dihitung 'pulih'


def slope_check(e30, H1ts, touch_ts, d):
    """Slope EMA30 searah saat sentuh. i = candle H1 terakhir yg CLOSED sebelum candle H1 yg memuat sentuh (tanpa lookahead).
    Long valid kalau EMA30[i] > EMA30[i-SLOPE_LB]; Short kalau lebih kecil. Data kurang -> tidak valid."""
    il = int(np.searchsorted(H1ts, touch_ts, side='right')) - 2
    if il - SLOPE_LB < 0:
        return False
    diff = e30[il] - e30[il - SLOPE_LB]
    return diff > 0 if d == 'Long' else diff < 0


def detect_snr_events(df, m5):
    """Level H1 (c1/c2 + c3 bersih) + FVG H1 C1..C5 + level tidak diulang sebelum FVG + bias EMA21/EMA30
    -> monitoring M5 (sentuh -> ENTRY_MODE: m5res = resisten M5 berFVG di-break body, entry di level | m5sr = S&R M5 -> IFVG | ifvg = IFVG biasa) -> LIMIT di IFVG, SL kedalaman terjauh (m5sr: + buffer).
    Exit: TP_MODE=far -> pergerakan terjauh H1 (farthest_move_tp); TP_MODE=opp -> target level lawan dinamis (opposite_levels).
    Return (events, stats). events urut fill_ts, dict:
    {'kind','type','level','direction','entry_price','sl_price','tp_price','fill_ts','test1_ts','test2_ts','c1_ts','meta'}"""
    stats = {'levels': 0, 'tanpa_fvg': 0, 'sudah_disentuh': 0, 'ulang': 0, 'warmup': 0, 'ema_ditolak': 0, 'bias_touch': 0, 'bias_pulih': 0, 'rr_kurang': 0, 'rr_diperbesar': 0, 'ema_touch': 0, 'ema_level': 0, 'swing_no_pivot': 0, 'swing_not_broken': 0, 'swing_sweep': 0, 'idm_none': 0, 'slope_ditolak': 0, 'aktif': 0,
             'no_touch': 0, 'no_ifvg': 0, 'no_fill': 0, 'sl_invalid': 0, 'tp_invalid': 0, 'entry': 0}
    events = []
    if m5 is None or df is None or len(df) < 10:
        return events, stats
    H1 = {k: df[v].values.astype(float) for k, v in (('o', 'open'), ('h', 'high'), ('l', 'low'), ('c', 'close'))}
    H1['ts'] = df['ts'].values.astype(np.int64)
    raw = raw_levels(H1['o'], H1['h'], H1['l'], H1['c'])
    idm_idx = {t: np.array(sorted(x['c1'] for x in raw if x['d'] == t), dtype=np.int64) for t in ('Long', 'Short')}
    e30_all = pd.Series(H1['c']).ewm(span=BIAS_EMA_SLOW, adjust=False).mean().values if SLOPE_FILTER else None
    stats['levels'] = len(raw)
    acts = [x for x in (activate_level(lv, H1, stats) for lv in raw) if x is not None]
    keep = []
    for x in acts:
        if level_repeated(x, H1):
            stats['ulang'] += 1
        else:
            keep.append(x)
    acts = keep
    cs_ = pd.Series(H1['c'])
    ea = cs_.ewm(span=BIAS_EMA_FAST, adjust=False).mean().values
    eb = cs_.ewm(span=BIAS_EMA_SLOW, adjust=False).mean().values
    if EMA_BIAS_FILTER and BIAS_AT == 'act':
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
    if LEVEL_EMA_TOUCH in ('both', 'any'):
        e21 = pd.Series(H1['c']).ewm(span=BIAS_EMA_FAST, adjust=False).mean().values
        keep = []
        for x in acts:
            i = x['c1']
            if i < EMA_WARMUP:
                stats['warmup'] += 1
                continue
            t1 = bool(H1['l'][i] <= e21[i] <= H1['h'][i])
            t2 = bool(H1['l'][i + 1] <= e21[i + 1] <= H1['h'][i + 1])
            if (t1 and t2) if LEVEL_EMA_TOUCH == 'both' else (t1 or t2):
                keep.append(x)
            else:
                stats['ema_touch'] += 1
        acts = keep
    if LEVEL_VS_EMA in ('c1', 'act'):
        e30 = pd.Series(H1['c']).ewm(span=BIAS_EMA_SLOW, adjust=False).mean().values
        keep = []
        for x in acts:
            i = x['c1']
            if i < EMA_WARMUP:
                stats['warmup'] += 1
                continue
            j = i if LEVEL_VS_EMA == 'c1' else x['fvg'][1]
            ok = x['L'] > e30[j] if x['d'] == 'Long' else x['L'] < e30[j]
            if ok:
                keep.append(x)
            else:
                stats['ema_level'] += 1
        acts = keep
    stats['aktif'] = len(acts)

    OPP = {}
    if TP_MODE == 'opp':
        st0 = {'tanpa_fvg': 0, 'sudah_disentuh': 0}
        OPP = {d: opposite_levels(H1, m5, d, raw, st0) for d in ('Long', 'Short')}
    TS = m5['TS']
    for d in ('Long', 'Short'):
        lst = sorted([x for x in acts if x['d'] == d], key=lambda x: x['act_ts'])
        for j, lv in enumerate(lst):
            if LEVEL_MODE == 'replace' and j + 1 < len(lst):
                e = int(np.searchsorted(TS, lst[j + 1]['act_ts'], side='left'))     # level baru menggantikan
            else:
                e = len(TS)                                                           # level tetap aktif sampai tersentuh
            status, inf = scan_windowed({'m5res': scan_m5res, 'm5sr': scan_m5sr}.get(ENTRY_MODE, scan_ifvg), lv, e, m5)
            if status != 'fill':
                stats[status] += 1
                continue
            if SWING_VALID:
                ok, why = swing_check(H1, lv['c1'], int(TS[inf['touch']]), d)
                if not ok:
                    stats['swing_' + why] += 1       # swing tidak di-break / hanya sweep / tak ada swing -> level tidak valid
                    continue
            if EMA_BIAS_FILTER and BIAS_AT == 'touch':
                ok, pulih = bias_touch_check(ea, eb, H1['ts'], int(TS[inf['touch']]), d, lv['fvg'][1])
                if not ok:
                    stats['bias_touch'] += 1         # bias EMA21/30 saat sentuh tidak searah -> level tidak valid
                    continue
                if pulih:
                    stats['bias_pulih'] += 1         # bias awal salah, EMA cross memulihkan sebelum sentuh -> tetap valid
            if SLOPE_FILTER:
                if not slope_check(e30_all, H1['ts'], int(TS[inf['touch']]), d):
                    stats['slope_ditolak'] += 1      # EMA30 menentang arah saat sentuh -> level tidak valid
                    continue
            if IDM_FILTER:
                ok, why = idm_check(H1, idm_idx, lv['c1'], int(TS[inf['touch']]), d)
                if not ok:
                    stats['idm_none'] += 1           # tidak ada inducement antara c2 s/d candle tertinggi -> level tidak valid
                    continue
            tp = None
            tp_ext = False
            if TP_MODE == 'rr':
                risk = abs(inf['entry'] - inf['sl'])
                tp = inf['entry'] + TP_RR * risk if d == 'Long' else inf['entry'] - TP_RR * risk      # TP tetap RR 1:TP_RR
            elif TP_MODE != 'opp':
                tp = farthest_move_tp(H1, m5, lv['c1'], inf['touch'], d)
                risk = abs(inf['entry'] - inf['sl'])
                bad = tp is None or (d == 'Long' and tp <= inf['entry']) or (d == 'Short' and tp >= inf['entry'])
                if MIN_RR > 0 and MIN_RR_MODE == 'extend':
                    if bad or abs(tp - inf['entry']) < MIN_RR * risk:
                        tp = inf['entry'] + MIN_RR * risk if d == 'Long' else inf['entry'] - MIN_RR * risk      # perbesar TP sampai RR = MIN_RR
                        tp_ext = True
                        stats['rr_diperbesar'] += 1
                else:
                    if bad:
                        stats['tp_invalid'] += 1      # TP tidak di sisi profit dari harga fill -> setup dibuang
                        continue
                    if MIN_RR > 0 and abs(tp - inf['entry']) < MIN_RR * risk:
                        stats['rr_kurang'] += 1       # RR (jarak TP / jarak SL) di bawah minimum -> setup digugurkan
                        continue
            stats['entry'] += 1
            f = inf['fill']; fv = lv['fvg']
            events.append({
                'kind': 'SNR_SUPPORT' if d == 'Long' else 'SNR_RESISTANCE', 'type': lv['ty'], 'level': lv['L'],
                'direction': d, 'entry_price': inf['entry'], 'sl_price': inf['sl'], 'tp_price': tp, 'targets': OPP.get(d, []), 'fill_ts': int(TS[f]),
                'test1_ts': int(TS[inf['touch']]), 'test2_ts': int(TS[inf['x3']]), 'c1_ts': int(H1['ts'][lv['c1']]),
                'meta': {
                    'fvg_h1': (int(H1['ts'][fv[0]]), int(H1['ts'][fv[1]])), 'level_aktif': int(lv['act_ts']),
                    'touch_ts': int(TS[inf['touch']]), 'y1_ts': int(TS[inf['y1']]), 'y3_ts': int(TS[inf['y3']]),
                    'x1_ts': int(TS[inf['x1']]), 'x3_ts': int(TS[inf['x3']]), 'n_ifvg': inf['n_ifvg'],
                    'bear': inf['bear'], 'bull': inf['bull'], 'ifvg_entry': inf['ent_lvl'], 'ifvg_low': inf['z_lo'],
                    'res_m5': inf.get('res_level'), 'tp_ext': tp_ext, 'etype': inf.get('etype'), 'ifvg_top': inf.get('ifvg_top'),
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
# EXIT: SL tetap; TP tetap = pergerakan terjauh (H1) setelah level terbentuk s/d sebelum candle H1 sentuh (ev['tp_price']).

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
    """Target aktif di candle now_ts: level lawan yang sudah aktif (act<=now) dan belum tersentuh (ft>=now), di sisi profit dari entry.
    TARGET_PICK=farthest: yang TERJAUH dari entry (Long: T terbesar; Short: T terkecil) | nearest: yang TERDEKAT | newest: c1 TERBARU.
    Seri -> c1 terbaru."""
    long_ = pos.get('direction') == 'Long'
    best = None
    for act, ft, T, c1 in pos['targets']:
        if not (act <= now_ts <= ft):
            continue
        if best is None:
            best = (T, c1)
            continue
        if TARGET_PICK == 'farthest':
            better = (T > best[0]) if long_ else (T < best[0])
        elif TARGET_PICK == 'nearest':
            better = (T < best[0]) if long_ else (T > best[0])
        else:
            better = c1 > best[1]
        if better or (TARGET_PICK in ('farthest', 'nearest') and T == best[0] and c1 > best[1]):
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
            'fill_ts': entry_ts, 'tp': ev['tp_price'],
            'targets': _cand_targets(ev['targets'], direction, entry_price, entry_ts) if TP_MODE == 'opp' else [],
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
            'tp': (pos['tp'] if TP_MODE != 'opp' else (exit_price if reason == 'TP' else None)), 'exit': exit_price, 'reason': reason, 'r_mult': r_mult, 'pnl_usd': pnl_net,
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

        # 1) exit posisi aktif simbol ini. Urutan per candle: SL dulu (exit = min/max(SL, open) utk gap), lalu TP
        #    (exit = max/min(TP, open)). Candle fill (entry_ts == now_ts) tidak dicek TP.
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
                        T = pos['tp'] if TP_MODE != 'opp' else _current_target(pos, now_ts)
                        if T is not None and hi >= T:
                            close_trade(key, max(T, op), 'TP', now_ts)
                else:  # Short
                    if hi >= S * (1 - 1e-9):
                        close_trade(key, max(S, op), 'SL', now_ts)
                        continue
                    if pos['entry_ts'] < now_ts:
                        T = pos['tp'] if TP_MODE != 'opp' else _current_target(pos, now_ts)
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
        _log_msg(f"🚀 Mulai backtest SNR (level H1 + FVG H1 + bias EMA{BIAS_EMA_FAST}/{BIAS_EMA_SLOW} + konfirmasi M5 [{ENTRY_MODE}], exit {_tp_desc(short=True)}) — {len(SYMBOLS)} koin, {BACKTEST_START_DATE} s/d {BACKTEST_END_DATE}")
        _log_msg(f"   Level c1/c2 (c3 bersih) + FVG H1 di C1..C{FVG_LAST_C} + level tidak berulang sebelum FVG + bias EMA (filter={'ON' if EMA_BIAS_FILTER else 'OFF'}, warm-up {EMA_WARMUP}) "
                  f"-> sentuh -> " + (f"resisten M5 berFVG di-break body -> LIMIT di level resisten (lookback {M5SR_LOOKBACK}), SL = terjauh + {SL_BUFFER_PCT*100:.1f}%" if ENTRY_MODE == 'm5res' else f"resisten M5 berFVG di-break FVG support M5 -> IFVG (lookback {M5SR_LOOKBACK}), SL = terjauh + {SL_BUFFER_PCT*100:.1f}%" if ENTRY_MODE == 'm5sr' else f"IFVG M5 (lookback {IFVG_LOOKBACK}), SL = kedalaman terjauh") + ", tanpa expired. "
                  f"Exit: {_tp_desc()}; SL dicek duluan.")

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
                      f"EMA {st['ema_ditolak']}, bias saat sentuh {st['bias_touch']} (pulih oleh cross {st['bias_pulih']}), tak sentuh EMA{BIAS_EMA_FAST} {st['ema_touch']}, level vs EMA{BIAS_EMA_SLOW} {st['ema_level']}; M5: tanpa sentuh {st['no_touch']}, tanpa IFVG {st['no_ifvg']}, limit tak terfill {st['no_fill']}, SL tidak valid {st['sl_invalid']}, TP tidak valid {st['tp_invalid']}, RR < {MIN_RR:g} {st['rr_kurang']}, TP diperbesar ke RR {MIN_RR:g} {st['rr_diperbesar']}; "
                      f"swing: tak di-break {st['swing_not_broken']}, hanya sweep {st['swing_sweep']}, tanpa swing {st['swing_no_pivot']}; tanpa IDM {st['idm_none']}; slope EMA{BIAS_EMA_SLOW} menentang {st['slope_ditolak']}.")

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
                'warmup_count': sum_st('warmup'), 'ema_count': sum_st('ema_ditolak'), 'bias_touch_count': sum_st('bias_touch'), 'bias_pulih_count': sum_st('bias_pulih'), 'ema_touch_count': sum_st('ema_touch'), 'ema_level_count': sum_st('ema_level'),
                'swing_invalid_count': sum_st('swing_no_pivot') + sum_st('swing_not_broken') + sum_st('swing_sweep'), 'idm_none_count': sum_st('idm_none'), 'slope_count': sum_st('slope_ditolak'),
                'swing_not_broken_count': sum_st('swing_not_broken'), 'swing_sweep_count': sum_st('swing_sweep'), 'swing_no_pivot_count': sum_st('swing_no_pivot'),
                'no_touch_count': sum_st('no_touch'), 'no_ifvg_count': sum_st('no_ifvg'),
                'no_fill_count': sum_st('no_fill'), 'sl_invalid_count': sum_st('sl_invalid'),
                'tp_invalid_count': sum_st('tp_invalid'), 'rr_kurang_count': sum_st('rr_kurang'), 'rr_diperbesar_count': sum_st('rr_diperbesar'),
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


def _tp_desc(short=False) -> str:
    if TP_MODE == 'opp':
        pick = {'farthest': 'terjauh', 'nearest': 'terdekat', 'newest': 'terbaru'}.get(TARGET_PICK, TARGET_PICK)
        return f"TP = level lawan {pick}" if short else f"TP = ujung wick level lawan {pick} (H1+FVG, tanpa EMA) yang belum tersentuh, dinamis"
    if TP_MODE == 'rr':
        return f"TP fix RR 1:{TP_RR:g}" if short else f"TP tetap = entry +/- {TP_RR:g} x jarak entry-SL (RR 1:{TP_RR:g})"
    extra = ((f", minimal RR 1:{MIN_RR:g} (diperbesar kalau kurang)" if MIN_RR_MODE == 'extend' else f", setup RR < 1:{MIN_RR:g} digugurkan")) if MIN_RR > 0 else ""
    return ("TP = pergerakan terjauh" + extra) if short else ("TP = pergerakan terjauh (H1) setelah level terbentuk s/d sebelum candle H1 sentuh" + extra)


def _tp_html() -> str:
    if TP_MODE == 'opp':
        pick = {'farthest': 'terjauh', 'nearest': 'terdekat', 'newest': 'terbaru'}.get(TARGET_PICK, TARGET_PICK)
        return (f"<b>TARGET</b> = ujung wick level lawan <b>{pick}</b> (H1+FVG, tanpa EMA) yang belum tersentuh dan lebih jauh dari entry; "
                "level yang wick-nya sudah tersentuh dianggap terpakai dan dilewati; berubah dinamis. SL dicek duluan dalam candle yang sama; TP tidak dicek di candle fill.")
    if TP_MODE == 'rr':
        return (f"<b>TP</b> tetap = entry ± {TP_RR:g} × jarak entry-SL (RR 1:{TP_RR:g}). SL dicek duluan dalam candle yang sama; TP tidak dicek di candle fill.")
    extra = ((f" Kalau jarak TP &lt; {MIN_RR:g} × jarak SL, TP <b>diperbesar</b> sampai RR 1:{MIN_RR:g}." if MIN_RR_MODE == 'extend' else
              f" Setup dengan jarak TP &lt; {MIN_RR:g} × jarak SL (RR di bawah 1:{MIN_RR:g}) digugurkan.")) if MIN_RR > 0 else ""
    return ("<b>TP</b> = pergerakan terjauh (candle H1) setelah level terbentuk s/d sebelum candle H1 yang menyentuh level "
            "(Long: high tertinggi; Short: low terendah), tetap sejak entry." + extra + " SL dicek duluan dalam candle yang sama; TP tidak dicek di candle fill.")


def _ematouch_html() -> str:
    if LEVEL_EMA_TOUCH == 'both':
        return f"candle c1 DAN c2 level H1 harus menyentuh EMA{BIAS_EMA_FAST} (low &lt;= EMA &lt;= high)"
    if LEVEL_EMA_TOUCH == 'any':
        return f"candle c1 ATAU c2 level H1 harus menyentuh EMA{BIAS_EMA_FAST}"
    return "tidak dipakai (OFF)"


def _levelema_html() -> str:
    if LEVEL_VS_EMA in ('c1', 'act'):
        at = 'candle c1 (saat level terbentuk)' if LEVEL_VS_EMA == 'c1' else 'candle FVG selesai'
        return f"harga level harus di atas EMA{BIAS_EMA_SLOW} untuk Long, di bawahnya untuk Short (EMA diambil di {at})"
    return "tidak dipakai (OFF)"


def _swing_html() -> str:
    if not SWING_VALID:
        return "tidak dipakai (OFF)."
    return (f"dari c1 mundur dicari wick tertinggi (Short: terendah) yang kuat, tak terlampaui {SWING_N} candle H1 di kiri dan {SWING_N} di kanan. "
            "Level valid hanya kalau swing itu di-<b>break body</b> (close melewati) <b>sebelum level tersentuh</b>; "
            "tidak di-break atau hanya <b>sweep</b> wick = level tidak valid.")


def _slope_html() -> str:
    if not SLOPE_FILTER:
        return "tidak dipakai (OFF)."
    return (f"saat level tersentuh, EMA{BIAS_EMA_SLOW} H1 harus searah: Long → naik, Short → turun "
            f"(EMA{BIAS_EMA_SLOW} candle H1 terakhir yang sudah closed dibanding {SLOPE_LB} candle sebelumnya).")


def _idm_html() -> str:
    if not IDM_FILTER:
        return "tidak dipakai (OFF)."
    tp = {'same': 'searah level (Long: support kecil, Short: resisten kecil)', 'opposite': 'berlawanan arah dengan level', 'any': 'searah atau berlawanan'}.get(IDM_TYPE, IDM_TYPE)
    return ("level H1 harus punya <b>IDM</b> = level tanpa FVG (pola c1+c2, c3 bersih), " + tp + ", di antara c2 level sampai <b>candle tertinggi</b> "
            "(Short: terendah) = puncak pergerakan sebelum harga menyentuh level lagi. Urutan: level-FVG → IDM → candle tertinggi → sentuh level.")


def _confirm_html() -> str:
    if ENTRY_MODE == 'm5res':
        return ("setelah level disentuh, tunggu <b>resisten M5 berFVG</b> (Short: support), lalu tunggu level itu di-<b>break body</b> "
                "(close melewati level). Entry <b>limit</b> di level resisten M5 tersebut (retest); resisten baru yg di-break sebelum fill "
                f"memindahkan limit. <b>SL</b> = kedalaman terjauh sejak sentuh s/d candle break + buffer {SL_BUFFER_PCT*100:.1f}%.")
    if ENTRY_MODE == 'm5sr':
        return ("setelah level disentuh, cari <b>resisten M5 berFVG</b> (Short: support), lalu level itu di-break oleh FVG dari "
                "<b>support M5 berFVG</b> (Short: resisten); dari pergerakan itu terbentuk <b>IFVG</b> (overlap kedua FVG). "
                "Entry <b>limit</b> di tepi atas overlap (Short: bawah); pasangan baru sebelum fill memindahkan limit. "
                f"<b>SL</b> = kedalaman terjauh sejak sentuh s/d FVG support selesai + buffer {SL_BUFFER_PCT*100:.1f}%.")
    ent = ("Saat IFVG terbentuk: kalau IFVG di <b>atas level H1</b> → entry <b>limit di level H1</b>; kalau IFVG di <b>bawah level</b> → <b>market order</b> "
           "(open candle M5 berikutnya). " if IFVG_ENTRY == 'level' else
           "<b>Market order</b> langsung di open candle M5 setelah IFVG selesai. " if IFVG_ENTRY == 'market' else
           "Entry <b>limit</b> di tepi atas overlap (Short: bawah). ")
    return (f"setelah level disentuh, FVG M5 searah harus 'mengambil' gap FVG M5 tidak searah (boleh terbentuk sampai {IFVG_LOOKBACK} "
            "candle sebelum sentuh). " + ent + "IFVG baru sebelum fill memindahkan limit. "
            "<b>SL</b> = kedalaman terjauh sejak sentuh s/d FVG searah selesai (tanpa buffer).")


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
  <h1>📊 Backtest SNR (H1 level + FVG + EMA{BIAS_EMA_FAST}/{BIAS_EMA_SLOW} bias + {'resisten M5 di-break' if ENTRY_MODE == 'm5res' else 'S&R M5 → IFVG' if ENTRY_MODE == 'm5sr' else 'IFVG'} M5, {_tp_desc(short=True)})</h1>
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
    <br>• <b>Sentuh EMA{BIAS_EMA_FAST}</b> (level awal saja): {_ematouch_html()}.
    <br>• <b>Level vs EMA{BIAS_EMA_SLOW}</b> (level awal saja): {_levelema_html()}.
    <br>• <b>Validasi swing</b>: {_swing_html()}
    <br>• <b>Slope EMA{BIAS_EMA_SLOW} saat sentuh</b>: {_slope_html()}
    <br>• <b>Inducement (IDM)</b>: {_idm_html()}
    <br>• <b>Bias EMA{BIAS_EMA_FAST}/{BIAS_EMA_SLOW}</b> (level awal saja, dihitung {'SAAT LEVEL TERSENTUH — EMA cross sejak level terbentuk ikut menentukan' if BIAS_AT == 'touch' else 'saat level aktif'}): Long hanya kalau EMA{BIAS_EMA_FAST} &gt; EMA{BIAS_EMA_SLOW}, Short kalau &lt;
    (filter {'ON' if EMA_BIAS_FILTER else 'OFF'}, warm-up {EMA_WARMUP} candle). {'Semua level disimpan; level yang belum tersentuh tetap aktif walau level searah baru terbentuk' if LEVEL_MODE == 'keep' else 'Level searah baru menggantikan yang lama'}; tanpa expired.
    <br>• <b>Konfirmasi M5 ({ENTRY_MODE})</b>: {_confirm_html()}
    <br>• <b>Exit</b>: tanpa trailing. {_tp_html()}
    <br>⚙️ Risk {RISK_PCT*100:.0f}% dari balance (compounding). Slot maksimum: {_fmt_max_concurrent()}.
    Level terdeteksi: {cr.get('levels_count',0)}. Aktif (lolos FVG/filter): {cr.get('aktif_count',0)}. Limit terfill: {cr.get('filled_count',0)}.
    Gugur — tanpa FVG: {cr.get('tanpa_fvg_count',0)}, disentuh sebelum FVG: {cr.get('disentuh_count',0)}, level berulang: {cr.get('ulang_count',0)},
    warm-up: {cr.get('warmup_count',0)}, filter EMA: {cr.get('ema_count',0)}, bias saat sentuh: {cr.get('bias_touch_count',0)} (pulih oleh cross: {cr.get('bias_pulih_count',0)}), tak sentuh EMA{BIAS_EMA_FAST}: {cr.get('ema_touch_count',0)}, level vs EMA{BIAS_EMA_SLOW}: {cr.get('ema_level_count',0)}; M5 — tanpa sentuh: {cr.get('no_touch_count',0)},
    tanpa IFVG: {cr.get('no_ifvg_count',0)}, limit tak terfill: {cr.get('no_fill_count',0)}, SL tidak valid: {cr.get('sl_invalid_count',0)}, TP tidak valid: {cr.get('tp_invalid_count',0)}, RR di bawah minimum: {cr.get('rr_kurang_count',0)}, TP diperbesar: {cr.get('rr_diperbesar_count',0)}.
    Validasi swing — tak di-break: {cr.get('swing_not_broken_count',0)}, hanya sweep: {cr.get('swing_sweep_count',0)}, tanpa swing: {cr.get('swing_no_pivot_count',0)}; tanpa IDM: {cr.get('idm_none_count',0)}; slope EMA{BIAS_EMA_SLOW} menentang: {cr.get('slope_count',0)}.
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
    return "" if x is None else f"{float(x):.10g}"


def _hasil_str(t):
    """TP -> 'TP +1.6R' (R bersih setelah fee); SL -> 'SL' (+ R bersih kalau bukan -1R persis, mis. gap)."""
    if t['reason'] == 'TP':
        return f"TP {t['r_mult']:+.1f}R"
    return f"SL {t['r_mult']:+.1f}R"


CSV_COLUMNS = ['Koin', 'Dir', 'Hasil', 'Level', 'Terbentuk', 'Touch', 'Setup selesai', 'Limit', 'Fill', 'SL', 'TP',
               'Fill time', 'Exit', 'Exit time']


def _trades_csv() -> bytes:
    """trades.csv ringkas, waktu WIB (UTC+7). Terbentuk = open candle H1 c1; Touch = candle M5 pertama menyentuh level;
    Setup selesai = candle M5 penutup setup (m5res: candle break; m5sr/ifvg: FVG searah selesai); Limit = harga limit; Fill = harga fill; Exit = harga exit."""
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
            _fmt_price(mt.get('ifvg_entry', t['entry'])), _fmt_price(t['entry']), _fmt_price(t['sl']), _fmt_price(t['tp']),
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
