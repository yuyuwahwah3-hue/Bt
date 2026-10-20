"""
backtest_cross.py — Backtest EMA21/EMA30 cross H1 + filter LEVEL searah berFVG, stop-and-reverse
===============================================================================================
- Timeframe H1 saja. EMA21 (cepat) & EMA30 (lambat) dari close.
- Golden cross -> LONG, death cross -> SHORT (tutup posisi lawan lalu balik arah). Tanpa SL, tanpa TP.
- LEVEL berFVG:
    Support (Long): c1 bearish, c2 bullish, level = close c1, c3 tidak menyentuh level (low[c3] > level).
    FVG bullish dari C{FVG_FIRST_C}..C{FVG_LAST_C} (default C1..C4): bisa C1-C3 (a=c1,b=c2,k=c3) atau C2-C4 (a=c2,b=c3,k=c4):
    low[k] > high[a] dan close[k] > high[b] (close candle ke-3 harus di atas wick/high candle ke-2).
    FVG di C1-C3 -> level langsung aktif di c3 (tanpa menunggu c4). FVG di C2-C4 -> gugur kalau c4 menyentuh level.
    Resisten (Short) = cermin.
- FILTER ENTRY (LEVEL_FILTER=1): c1 level searah berFVG harus berada di jendela: LEVEL_BEFORE=5 candle terakhir sebelum cross
  (candle cross = candle ke-1, jadi c1 maksimal di ci-4) sampai LEVEL_AFTER=10 candle setelah cross.
    a) FVG level sudah selesai saat cross (k <= ci) -> entry MARKET di open candle ci+1.
    b) FVG belum selesai -> tunggu FVG selesai, entry market di open candle setelah candle k (boleh jauh dari titik cross),
       asal sebelum cross berlawanan.
    c) tidak ada level di jendela, atau cross berlawanan muncul duluan -> sinyal gugur.
- EXIT tidak difilter: posisi selalu ditutup di open candle H1 setelah cross berlawanan. Cross tanpa level -> bot flat
  sampai ada cross berikutnya yang lolos.
- Ukuran posisi: MARGIN = RISK_PCT x balance (5%), leverage 10x -> notional 50% balance ($5 pada modal $10 = minimum order Bybit).
  Kalau notional < MIN_ORDER_USD -> dinaikkan ke minimum.
- Likuidasi (isolated): harga melawan >= (1/LEVERAGE - LIQ_MMR) dari entry (dicek per candle H1) -> margin hilang.
- Fee: 0.1% masuk, 0.3% keluar dari nilai posisi. Posisi terbuka di akhir data tidak dihitung.
- Dua simulasi: GABUNGAN (1 balance bersama) dan PER-KOIN independen (tiap koin modal awal sendiri).
Env: INITIAL_BALANCE, RISK_PCT, LEVERAGE, MIN_ORDER_USD, LEVEL_FILTER, LEVEL_BEFORE, LEVEL_AFTER, FVG_FIRST_C, FVG_LAST_C,
EMA_FAST, EMA_SLOW, EMA_WARMUP, FEE_ENTRY_PCT, FEE_EXIT_PCT, LIQ_MMR, MARGIN_USAGE_CAP, BACKTEST_START_DATE, BACKTEST_END_DATE,
SYMBOLS (pisah koma).
Start command Railway: python backtest_cross.py
"""
import os, threading, time, io, csv, traceback
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import numpy as np
import pandas as pd
from pybit.unified_trading import HTTP

# ============================================================
# CONFIG
# ============================================================
PORT             = int(os.environ.get('PORT', 8080))
INITIAL_BALANCE  = float(os.environ.get('INITIAL_BALANCE', '10.0'))
RISK_PCT         = float(os.environ.get('RISK_PCT', '0.05'))
LEVERAGE         = float(os.environ.get('LEVERAGE', '10'))
MIN_ORDER_USD    = float(os.environ.get('MIN_ORDER_USD', '5.0'))
LEVEL_FILTER     = os.environ.get('LEVEL_FILTER', '1').strip() not in ('0', 'false', 'False', 'off', '')
LEVEL_BEFORE     = int(os.environ.get('LEVEL_BEFORE', 5))            # c1 level maks di candle ke-N ke belakang (candle cross = ke-1)
LEVEL_AFTER      = int(os.environ.get('LEVEL_AFTER', 10))            # c1 level boleh sampai N candle setelah cross
FVG_FIRST_C      = int(os.environ.get('FVG_FIRST_C', 1))             # 1 -> FVG bisa C1-C3 atau C2-C4 ; 2 -> hanya C2-C4
FVG_LAST_C       = int(os.environ.get('FVG_LAST_C', 4))              # FVG selesai paling lambat di candle ke-N level (C4)
EMA_FAST         = int(os.environ.get('EMA_FAST', 21))
EMA_SLOW         = int(os.environ.get('EMA_SLOW', 30))
EMA_WARMUP       = int(os.environ.get('EMA_WARMUP', 100))
FEE_ENTRY_PCT    = float(os.environ.get('FEE_ENTRY_PCT', '0.001'))
FEE_EXIT_PCT     = float(os.environ.get('FEE_EXIT_PCT', str(0.001 * 3)))
LIQ_MMR          = float(os.environ.get('LIQ_MMR', '0.005'))
LIQ_PCT          = max(0.0, 1.0 / LEVERAGE - LIQ_MMR)
MARGIN_USAGE_CAP = float(os.environ.get('MARGIN_USAGE_CAP', '0.90'))
WICK_EPS         = 1e-9

def _default_end_date():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')

def _default_start_date():
    return (datetime.now(timezone.utc) - timedelta(days=365)).strftime('%Y-%m-%d')

BACKTEST_START_DATE = os.environ.get('BACKTEST_START_DATE', _default_start_date())
BACKTEST_END_DATE   = os.environ.get('BACKTEST_END_DATE', _default_end_date())

CACHE_DIR = os.environ.get('CACHE_DIR', './data_cache')
os.makedirs(CACHE_DIR, exist_ok=True)

_DEFAULT_SYMBOLS = [
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
_sym_env = os.environ.get('SYMBOLS', '').strip()
SYMBOLS = [s.strip().upper() for s in _sym_env.split(',') if s.strip()] if _sym_env else _DEFAULT_SYMBOLS


def _date_to_ms(date_str):
    dt = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

_START_MS = _date_to_ms(BACKTEST_START_DATE)
_END_MS   = _date_to_ms(BACKTEST_END_DATE) + 86400 * 1000 - 1

# ============================================================
# GLOBAL STATE
# ============================================================
_lock = threading.Lock()
_log = []
_phase = 'running'
_trades_all = []
_summary = {}
_per_coin = []          # kontribusi per koin (simulasi gabungan)
_per_coin_indep = []    # simulasi per-koin independen
_monthly = []


def _ts():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).strftime('%H:%M:%S')

def _fmt_wib(ts_ms):
    dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc) + timedelta(hours=7)
    return dt.strftime('%Y-%m-%d %H:%M')

def _log_msg(msg):
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    with _lock:
        _log.append(line)


# ============================================================
# FETCH H1 DARI BYBIT (cache ke disk)
# ============================================================
def _cache_path(symbol):
    return os.path.join(CACHE_DIR, f"{symbol}_H1_{BACKTEST_START_DATE}_{BACKTEST_END_DATE}.csv")


def fetch_h1(symbol):
    path = _cache_path(symbol)
    if os.path.exists(path):
        try:
            df = pd.read_csv(path)
            if not df.empty and {'ts', 'open', 'high', 'low', 'close'}.issubset(df.columns):
                _log_msg(f"   💾 {symbol}: pakai cache ({len(df):,} candle H1)")
                return df
        except Exception as e:
            _log_msg(f"   ⚠ {symbol}: cache korup ({e}), fetch ulang.")
    session = HTTP(testnet=False)
    rows, cur_end = [], _END_MS
    while True:
        data = None
        for attempt in range(4):
            try:
                res = session.get_kline(symbol=symbol, category='linear', interval=60,
                                        limit=1000, start=_START_MS, end=cur_end)
                data = res['result']['list']
                break
            except Exception as e:
                wait = 2 ** attempt
                _log_msg(f"   ⚠ {symbol} API error (percobaan {attempt+1}): {e} — retry {wait}s")
                time.sleep(wait)
        if data is None:
            _log_msg(f"   ❌ {symbol}: gagal fetch setelah 4 percobaan.")
            break
        if not data:
            break
        for kl in data:
            rows.append({'ts': int(kl[0]), 'open': float(kl[1]), 'high': float(kl[2]),
                         'low': float(kl[3]), 'close': float(kl[4])})
        oldest = int(data[-1][0])
        if oldest <= _START_MS:
            break
        cur_end = oldest - 1
        time.sleep(0.15)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).drop_duplicates(subset='ts').sort_values('ts').reset_index(drop=True)
    try:
        df.to_csv(path, index=False)
    except Exception as e:
        _log_msg(f"   ⚠ {symbol}: gagal simpan cache — {e}")
    return df


# ============================================================
# LEVEL searah berFVG (H1)
# ============================================================
def level_activations(o, h, l, c, d):
    """Semua level searah berFVG. Return (c1s, ks): indeks c1 level dan indeks candle k (FVG selesai/closed), urut menurut k.
    d=+1: support (Long), d=-1: resisten (Short).
    Support: c1 bearish, c2 bullish, level = close c1, c3 tidak menyentuh level. FVG bullish dari C{FVG_FIRST_C}..C{FVG_LAST_C}
    (a, b=a+1, k=a+2): low[k] > high[a] dan close[k] > high[b]. FVG di C1-C3 -> level aktif di c3 (tanpa menunggu c4).
    FVG di C2-C4 -> gugur kalau c4 menyentuh level. Short = cermin."""
    if d == -1:
        o, h, l, c = -o, -l, -h, -c                       # cerminkan ke Long
    n = len(c)
    out = []
    first_off = FVG_FIRST_C - 1
    for i in range(0, n - 4):
        if not (c[i] < o[i] and c[i + 1] > o[i + 1] and l[i + 2] > c[i] + WICK_EPS):
            continue
        L = c[i]
        for a in range(i + first_off, i + FVG_LAST_C - 2):    # k = a+2 <= i + FVG_LAST_C - 1
            k = a + 2
            if k >= n:
                break
            if l[k] > h[a] and c[k] > h[a + 1]:
                seg = l[i + 3:k + 1]
                if not (len(seg) and (seg <= L + WICK_EPS).any()):
                    out.append((k, i))
                break
    out.sort()
    arr = np.array(out, dtype=np.int64).reshape(-1, 2)
    return arr[:, 1], arr[:, 0]                              # (c1s, ks)


# ============================================================
# SINYAL: segmen posisi per koin (stop-and-reverse + filter level berFVG)
# ============================================================
def build_segments(symbol, df):
    """Return (segs, stats). segs = list dict(symbol, dir(+1/-1), e_ts, e_px, x_ts, x_px, reason); reason: 'cross' | 'liq'.
    Filter entry: c1 level searah berFVG harus berada di ci-(LEVEL_BEFORE-1) .. ci+LEVEL_AFTER (candle cross = candle ke-1).
    FVG sudah selesai saat cross (k <= ci) -> entry market di open ci+1. Belum -> tunggu FVG selesai, entry di open k+1
    (boleh jauh dari cross), asal sebelum cross berlawanan."""
    o = df['open'].values.astype(float); h = df['high'].values.astype(float)
    l = df['low'].values.astype(float);  c = df['close'].values.astype(float)
    ts = df['ts'].values.astype(np.int64)
    n = len(c)
    stats = {'cross': 0, 'valid': 0, 'filtered': 0, 'langsung': 0, 'tunggu': 0}
    if n < EMA_WARMUP + 5:
        return [], stats
    cs = pd.Series(c)
    ef = cs.ewm(span=EMA_FAST, adjust=False).mean().values
    es = cs.ewm(span=EMA_SLOW, adjust=False).mean().values
    up = ef > es
    gc = np.flatnonzero(up[1:] & ~up[:-1]) + 1          # golden cross: candle i closed
    dc = np.flatnonzero(~up[1:] & up[:-1]) + 1          # death cross
    crosses = sorted([(int(i), 1) for i in gc if i >= EMA_WARMUP] + [(int(i), -1) for i in dc if i >= EMA_WARMUP])
    stats['cross'] = len(crosses)
    acts = {1: level_activations(o, h, l, c, 1), -1: level_activations(o, h, l, c, -1)} if LEVEL_FILTER else {}
    segs = []
    for k_, (ci, d) in enumerate(crosses):
        if k_ + 1 >= len(crosses):
            break                                        # posisi terakhir belum selesai -> tidak dihitung
        nxt = crosses[k_ + 1][0]
        x = nxt + 1                                      # exit selalu di open candle setelah cross berikutnya (tanpa filter)
        mode = None
        if not LEVEL_FILTER:
            e = ci + 1
        else:
            c1s, ks = acts[d]
            inw = (c1s >= ci - (LEVEL_BEFORE - 1)) & (c1s <= ci + LEVEL_AFTER)   # c1 level di jendela (cross = candle ke-1)
            kk = ks[inw]
            if len(kk) and (kk <= ci).any():
                e = ci + 1; mode = 'langsung'            # FVG sudah selesai saat cross -> entry market
            elif len(kk) and int(kk.min()) < nxt:        # tunggu FVG selesai, harus sebelum cross berlawanan
                e = int(kk.min()) + 1; mode = 'tunggu'
            else:
                stats['filtered'] += 1
                continue
        if e >= n or x >= n or e >= x:
            continue
        stats['valid'] += 1
        if mode:
            stats[mode] += 1
        e_px = o[e]
        liq_idx = None
        if LIQ_PCT > 0:
            if d == 1:
                hit = l[e:x] <= e_px * (1 - LIQ_PCT)
            else:
                hit = h[e:x] >= e_px * (1 + LIQ_PCT)
            if hit.any():
                liq_idx = e + int(hit.argmax())
        if liq_idx is not None:
            x_ts = int(ts[liq_idx]); x_px = e_px * (1 - LIQ_PCT) if d == 1 else e_px * (1 + LIQ_PCT); reason = 'liq'
        else:
            x_ts = int(ts[x]); x_px = float(o[x]); reason = 'cross'
        segs.append({'symbol': symbol, 'dir': d, 'e_ts': int(ts[e]), 'e_px': float(e_px),
                     'x_ts': x_ts, 'x_px': float(x_px), 'reason': reason})
    return segs, stats


# ============================================================
# SIMULASI (dipakai utk gabungan dan per-koin)
# ============================================================
def simulate(all_segs):
    events = []
    for sid, s in enumerate(all_segs):
        events.append((s['e_ts'], 1, sid))               # open
        events.append((s['x_ts'], 0, sid))               # close (0 duluan di timestamp sama)
    events.sort()
    balance = INITIAL_BALANCE
    used = 0.0
    live = {}
    trades = []
    blocked = 0
    peak = balance
    max_dd = 0.0
    for t, typ, sid in events:
        s = all_segs[sid]
        if typ == 1:
            if balance <= 0:
                blocked += 1
                continue
            margin = balance * RISK_PCT
            if margin * LEVERAGE < MIN_ORDER_USD:        # di bawah minimum order Bybit -> naikkan ke minimum
                margin = MIN_ORDER_USD / LEVERAGE
            if used + margin > balance * MARGIN_USAGE_CAP:
                blocked += 1
                continue
            qty = margin * LEVERAGE / s['e_px']
            live[sid] = (qty, margin)
            used += margin
        else:
            if sid not in live:
                continue
            qty, margin = live.pop(sid)
            used -= margin
            d = s['dir']
            if s['reason'] == 'liq':
                pnl = -margin
            else:
                gross = d * (s['x_px'] - s['e_px']) * qty
                fee = s['e_px'] * qty * FEE_ENTRY_PCT + s['x_px'] * qty * FEE_EXIT_PCT
                pnl = max(gross - fee, -margin)
            balance += pnl
            peak = max(peak, balance)
            if peak > 0:
                max_dd = max(max_dd, (peak - balance) / peak * 100)
            trades.append({**s, 'margin': margin, 'pnl': pnl, 'pnl_pct': pnl / margin * 100, 'balance_after': balance})
    n = len(trades)
    wins = sum(1 for x in trades if x['pnl'] > 0)
    liqs = sum(1 for x in trades if x['reason'] == 'liq')
    summary = {
        'n_trades': n, 'n_win': wins, 'wr': (wins / n * 100) if n else 0.0, 'n_liq': liqs,
        'final_balance': balance, 'roi': (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100,
        'total_pnl': balance - INITIAL_BALANCE, 'max_dd': max_dd, 'blocked': blocked,
        'avg_win': (sum(x['pnl'] for x in trades if x['pnl'] > 0) / wins) if wins else 0.0,
        'avg_loss': (sum(x['pnl'] for x in trades if x['pnl'] <= 0) / (n - wins)) if n - wins else 0.0,
    }
    return trades, summary


def per_coin_breakdown(trades):
    by = {}
    for t in trades:
        d = by.setdefault(t['symbol'], {'n': 0, 'win': 0, 'pnl': 0.0, 'liq': 0})
        d['n'] += 1
        d['win'] += 1 if t['pnl'] > 0 else 0
        d['pnl'] += t['pnl']
        d['liq'] += 1 if t['reason'] == 'liq' else 0
    rows = [{'symbol': s, 'n': d['n'], 'wr': d['win'] / d['n'] * 100, 'pnl': d['pnl'], 'liq': d['liq']} for s, d in by.items()]
    rows.sort(key=lambda r: -r['pnl'])
    return rows


def monthly_breakdown(trades):
    if not trades:
        return [], 0.0
    ts_sorted = sorted(trades, key=lambda t: t['x_ts'])
    by = {}
    for t in ts_sorted:
        dt = datetime.fromtimestamp(t['x_ts'] / 1000, tz=timezone.utc) + timedelta(hours=7)
        by.setdefault(dt.strftime('%Y-%m'), []).append(t)
    rows, running = [], INITIAL_BALANCE
    for m in sorted(by):
        tr = by[m]
        end = tr[-1]['balance_after']
        wins = sum(1 for t in tr if t['pnl'] > 0)
        rows.append({'month': m, 'n': len(tr), 'wr': wins / len(tr) * 100, 'start': running, 'end': end,
                     'profit': end - running, 'growth': (end - running) / running * 100 if running > 0 else 0.0})
        running = end
    return rows, sum(r['growth'] for r in rows) / len(rows)


# ============================================================
# WORKER
# ============================================================
def _run():
    global _phase, _summary, _per_coin, _per_coin_indep, _monthly
    try:
        win_txt = (f"ON (c1 level {LEVEL_BEFORE} candle sebelum cross s/d {LEVEL_AFTER} candle setelah cross)"
                   if LEVEL_FILTER else "OFF")
        _log_msg(f"🚀 Backtest EMA{EMA_FAST}/EMA{EMA_SLOW} cross H1 (stop-and-reverse, tanpa SL/TP) — {len(SYMBOLS)} koin, "
                 f"{BACKTEST_START_DATE} s/d {BACKTEST_END_DATE}; margin {RISK_PCT*100:g}%/posisi, leverage {LEVERAGE:g}x, "
                 f"likuidasi ±{LIQ_PCT*100:.1f}%, filter level berFVG {win_txt}")
        all_segs = []
        segs_by = {}
        for symbol in SYMBOLS:
            _log_msg(f"📊 {symbol}: fetch H1...")
            df = fetch_h1(symbol)
            if df.empty:
                _log_msg(f"   ⚠ {symbol}: data kosong, skip.")
                continue
            df = df.sort_values('ts').reset_index(drop=True)
            segs, st = build_segments(symbol, df)
            segs_by[symbol] = segs
            all_segs.extend(segs)
            n_liq = sum(1 for s in segs if s['reason'] == 'liq')
            _log_msg(f"   ✅ {symbol}: {len(df):,} candle, {st['cross']} cross, {st['filtered']} gugur (tanpa level), "
                     f"{st['langsung']} entry langsung + {st['tunggu']} entry setelah menunggu, "
                     f"{len(segs)} posisi selesai ({n_liq} likuidasi)")
        _log_msg(f"🧮 Simulasi gabungan ({len(all_segs)} posisi)...")
        trades, summ = simulate(all_segs)
        monthly, avg_g = monthly_breakdown(trades)

        _log_msg(f"🧮 Simulasi PER-KOIN independen ({len(segs_by)} koin, modal ${INITIAL_BALANCE:g} masing-masing)...")
        indep = []
        for sym, sg in segs_by.items():
            _, sm = simulate(sg)
            indep.append({'symbol': sym, 'n': sm['n_trades'], 'wr': sm['wr'], 'liq': sm['n_liq'],
                          'final': sm['final_balance'], 'roi': sm['roi'], 'max_dd': sm['max_dd'], 'blocked': sm['blocked']})
        indep.sort(key=lambda r: -r['roi'])

        with _lock:
            _trades_all[:] = sorted(trades, key=lambda t: t['x_ts'])
            _summary = {**summ, 'avg_growth': avg_g}
            _per_coin = per_coin_breakdown(trades)
            _per_coin_indep = indep
            _monthly = monthly
            _phase = 'done'
        _log_msg(f"✅ SELESAI. {summ['n_trades']} trade, WR {summ['wr']:.1f}%, likuidasi {summ['n_liq']}, "
                 f"balance akhir ${summ['final_balance']:.2f} (ROI {summ['roi']:+.1f}%), max drawdown {summ['max_dd']:.1f}%")
    except Exception as e:
        _log_msg(f"❌ ERROR: {e}")
        _log_msg(traceback.format_exc())
        with _lock:
            _phase = 'error'


# ============================================================
# DASHBOARD
# ============================================================
def _render_html():
    with _lock:
        phase = _phase
        s = dict(_summary)
        coins = list(_per_coin)
        indep = list(_per_coin_indep)
        months = list(_monthly)
        log_html = '\n'.join(_log[-300:])
    status_map = {
        'running': '<div class="status running">⏳ Sedang berjalan...</div>',
        'error': '<div class="status error">❌ Error — lihat log di bawah.</div>',
        'done': '<div class="status done">✅ Selesai</div>',
    }
    status = status_map.get(phase, status_map['done'])

    def g(k, d=0):
        return s.get(k, d)

    def cls(v):
        return 'pos' if v >= 0 else 'neg'

    cards = ''
    if s:
        liq_cls = 'neg' if g('n_liq') else ''
        cards = (
            '<div class="cards">'
            f'<div class="card"><div class="label">Total Trade</div><div class="value">{g("n_trades")}</div></div>'
            f'<div class="card"><div class="label">Win Rate</div><div class="value">{g("wr"):.1f}%</div></div>'
            f'<div class="card"><div class="label">Likuidasi</div><div class="value {liq_cls}">{g("n_liq")}</div></div>'
            f'<div class="card"><div class="label">Balance Akhir</div><div class="value">${g("final_balance"):.2f}</div></div>'
            f'<div class="card"><div class="label">ROI</div><div class="value {cls(g("roi"))}">{g("roi"):+.1f}%</div></div>'
            f'<div class="card"><div class="label">Max Drawdown</div><div class="value neg">{g("max_dd"):.1f}%</div></div>'
            f'<div class="card"><div class="label">Rata-rata Win / Loss</div><div class="value" style="font-size:16px">'
            f'${g("avg_win"):+.2f} / ${g("avg_loss"):+.2f}</div></div>'
            '</div>'
        )
    coin_rows = ''.join(f'<tr><td>{r["symbol"]}</td><td>{r["n"]}</td><td>{r["wr"]:.1f}%</td><td>{r["liq"]}</td>'
                        f'<td class="{cls(r["pnl"])}">${r["pnl"]:+.2f}</td></tr>' for r in coins)
    indep_rows = ''.join(f'<tr><td>{r["symbol"]}</td><td>{r["n"]}</td><td>{r["wr"]:.1f}%</td><td>{r["liq"]}</td>'
                         f'<td>${r["final"]:.2f}</td><td class="{cls(r["roi"])}">{r["roi"]:+.1f}%</td>'
                         f'<td class="neg">{r["max_dd"]:.1f}%</td><td>{r["blocked"]}</td></tr>' for r in indep)
    month_rows = ''.join(f'<tr><td>{r["month"]}</td><td>{r["n"]}</td><td>{r["wr"]:.1f}%</td><td>${r["start"]:.2f}</td>'
                         f'<td>${r["end"]:.2f}</td><td class="{cls(r["profit"])}">${r["profit"]:+.2f}</td>'
                         f'<td class="{cls(r["growth"])}">{r["growth"]:+.1f}%</td></tr>' for r in months)
    avg_g = g('avg_growth', 0.0)
    if LEVEL_FILTER:
        filt = (f'Filter entry: c1 level searah berFVG (FVG dari C{FVG_FIRST_C}-C{FVG_LAST_C}) harus berada dalam {LEVEL_BEFORE} candle terakhir '
                f'sebelum cross (candle cross = ke-1) sampai {LEVEL_AFTER} candle setelah cross. FVG sudah selesai saat cross -> langsung entry; '
                f'belum -> tunggu FVG selesai (entry boleh jauh dari cross); kalau tidak ada, sinyal gugur. Exit tetap hanya cross.')
    else:
        filt = 'Filter entry: OFF.'

    css = '''
 body { font-family:-apple-system,Arial,sans-serif; background:#0f1117; color:#e6e6e6; margin:0; padding:20px; }
 h1 { font-size:20px; } h2 { font-size:16px; margin-top:28px; }
 .status { padding:10px 14px; border-radius:8px; margin-bottom:16px; font-weight:600; }
 .status.running { background:#3a2f00; color:#ffd866; } .status.done { background:#0f3a1e; color:#7ee787; } .status.error { background:#3a0f0f; color:#ff7b72; }
 .cards { display:flex; flex-wrap:wrap; gap:12px; margin-bottom:20px; }
 .card { background:#161b22; border:1px solid #30363d; border-radius:10px; padding:14px 18px; min-width:140px; }
 .card .label { font-size:12px; color:#8b949e; } .card .value { font-size:22px; font-weight:700; margin-top:4px; }
 table { border-collapse:collapse; width:100%; font-size:13px; margin-bottom:10px; }
 th,td { border:1px solid #30363d; padding:6px 10px; text-align:right; } th { background:#161b22; color:#8b949e; }
 td:first-child,th:first-child { text-align:left; }
 .pos { color:#7ee787; } .neg { color:#ff7b72; }
 .note { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:12px 16px; font-size:13px; line-height:1.6; margin-top:16px; }
 .log { background:#0d1117; border:1px solid #30363d; border-radius:8px; padding:12px; font-size:12px; font-family:monospace; max-height:400px; overflow-y:auto; white-space:pre-wrap; }
 a { color:#58a6ff; }
'''
    html = (
        '<!DOCTYPE html><html lang="id"><head><meta charset="utf-8"><meta http-equiv="refresh" content="10">'
        '<title>Backtest EMA Cross H1</title><style>' + css + '</style></head><body>'
        f'<h1>📊 Backtest EMA{EMA_FAST}/EMA{EMA_SLOW} Cross — H1, stop-and-reverse</h1>'
        + status + cards +
        '<div class="note">'
        f'Golden cross → Long, death cross → tutup &amp; Short (dan sebaliknya). Tanpa SL/TP. {filt}<br>'
        f'Margin {RISK_PCT*100:g}% balance per posisi (min order ${MIN_ORDER_USD:g}), leverage {LEVERAGE:g}x '
        f'(likuidasi bila harga melawan ±{LIQ_PCT*100:.1f}%). Fee {FEE_ENTRY_PCT*100:.2f}% masuk / {FEE_EXIT_PCT*100:.2f}% keluar. '
        f'Modal awal ${INITIAL_BALANCE:g}. Sinyal terlewat (margin penuh, simulasi gabungan): {g("blocked")}.<br>'
        '<a href="/trades.csv">/trades.csv</a> | <a href="/logs">/logs</a></div>'
        '<h2>Per Bulan (simulasi gabungan)</h2>'
        f'<div class="note">Rata-rata pertumbuhan: <b class="{cls(avg_g)}">{avg_g:+.1f}% / bulan</b></div>'
        '<table><tr><th>Bulan</th><th>Trade</th><th>WR%</th><th>Balance Awal</th><th>Balance Akhir</th><th>Profit</th><th>Pertumbuhan</th></tr>'
        + month_rows + '</table>'
        '<h2>Kontribusi per Koin (simulasi gabungan, 1 balance bersama)</h2>'
        '<table><tr><th>Symbol</th><th>Trade</th><th>WR%</th><th>Likuidasi</th><th>Total PnL</th></tr>' + coin_rows + '</table>'
        f'<h2>Simulasi Per-Koin (independen, modal awal ${INITIAL_BALANCE:g} sendiri-sendiri)</h2>'
        '<table><tr><th>Symbol</th><th>Trade</th><th>WR%</th><th>Likuidasi</th><th>Balance Akhir</th><th>ROI</th><th>Max DD</th><th>Terlewat</th></tr>'
        + indep_rows + '</table>'
        '<h2>Log</h2><div class="log" id="log">' + log_html + '</div>'
        "<script>var e=document.getElementById('log');if(e)e.scrollTop=e.scrollHeight;</script>"
        '</body></html>'
    )
    return html.encode('utf-8')


CSV_COLUMNS = ['Koin', 'Dir', 'Hasil', 'Entry time', 'Entry', 'Exit time', 'Exit', 'Alasan exit', 'Margin', 'PnL USD', 'PnL % margin']


def _trades_csv():
    with _lock:
        tr = list(_trades_all)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for t in tr:
        w.writerow([t['symbol'], 'LONG' if t['dir'] == 1 else 'SHORT', 'WIN' if t['pnl'] > 0 else 'LOSS',
                    _fmt_wib(t['e_ts']), f"{t['e_px']:.10g}", _fmt_wib(t['x_ts']), f"{t['x_px']:.10g}",
                    t['reason'], f"{t['margin']:.4f}", f"{t['pnl']:.4f}", f"{t['pnl_pct']:.2f}"])
    return buf.getvalue().encode('utf-8')


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path == '/trades.csv':
                self._send(200, _trades_csv(), 'text/csv; charset=utf-8',
                           {'Content-Disposition': 'attachment; filename="trades.csv"'})
            elif path == '/logs':
                with _lock:
                    body = '\n'.join(_log).encode('utf-8')
                self._send(200, body, 'text/plain; charset=utf-8')
            else:
                self._send(200, _render_html(), 'text/html; charset=utf-8')
        except Exception:
            # tampilkan traceback di browser supaya error halaman kelihatan (bukan web mati)
            self._send(500, traceback.format_exc().encode('utf-8'), 'text/plain; charset=utf-8')

    def log_message(self, *_):
        pass


if __name__ == '__main__':
    threading.Thread(target=_run, daemon=True).start()
    server = ThreadingHTTPServer(('0.0.0.0', PORT), _Handler)
    print(f"🌐 Server running on port {PORT}", flush=True)
    server.serve_forever()