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