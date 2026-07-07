"""
indicators.py - REBUILD v2

Menggunakan smartmoneyconcepts (pip install smartmoneyconcepts)
sebagai engine SMC yang sudah teruji komunitas (1.7k stars).
Kita hanya tambahkan:
- Anti-repaint wrapper (sesuai blueprint v16)
- ADX/ATR/EMA murni pandas (tidak ada di library)
- get_confirmed_snapshot() untuk Tier 1

ANTI-REPAINT RULES (HARDCODED):
- Semua indikator baca dari candle[-2] (sudah close)
- Swing/fractal baca dari candle[-6] (5 kiri + 5 kanan confirmed)
- JANGAN pakai candle[-1] (masih berjalan)
"""

import os
import pandas as pd
import numpy as np

# Suppress credit message
os.environ["SMC_CREDIT"] = "0"
from smartmoneyconcepts import smc as SMC


# =====================================================
# ADX / ATR / EMA (murni pandas, tidak ada di library)
# =====================================================
def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high-low, (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    high, low, close = df["high"], df["low"], df["close"]
    prev_high, prev_low, prev_close = high.shift(1), low.shift(1), close.shift(1)
    up_move = high - prev_high
    dn_move = prev_low - low
    plus_dm  = np.where((up_move > dn_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((dn_move > up_move) & (dn_move > 0), dn_move, 0.0)
    plus_dm  = pd.Series(plus_dm,  index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low  - prev_close).abs()
    tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    alpha = 1/period
    tr_s  = tr.ewm(alpha=alpha, adjust=False).mean()
    pdi_s = plus_dm.ewm(alpha=alpha, adjust=False).mean()
    mdi_s = minus_dm.ewm(alpha=alpha, adjust=False).mean()
    plus_di  = 100 * pdi_s / tr_s
    minus_di = 100 * mdi_s / tr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx_s = dx.ewm(alpha=alpha, adjust=False).mean()
    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": adx_s})


# =====================================================
# SMC WRAPPERS (anti-repaint compliant)
# =====================================================
def _prep(df: pd.DataFrame) -> pd.DataFrame:
    """
    Siapkan df untuk library:
    - Exclude candle[-1] (anti-repaint)
    - Rename tick_volume -> volume (MT5 pakai tick_volume,
      library butuh 'volume')
    """
    df_closed = df.iloc[:-1].copy().reset_index(drop=True)
    if "volume" not in df_closed.columns and "tick_volume" in df_closed.columns:
        df_closed = df_closed.rename(columns={"tick_volume": "volume"})
    return df_closed


def get_swing_highs_lows(df: pd.DataFrame, swing_length: int = 5) -> pd.DataFrame:
    """Swing high/low dengan anti-repaint (exclude candle[-1])."""
    return SMC.swing_highs_lows(_prep(df), swing_length=swing_length)


def get_fvg(df: pd.DataFrame) -> pd.DataFrame:
    """FVG dari candle yang sudah close (exclude candle[-1])."""
    return SMC.fvg(_prep(df))


def get_bos_choch(df: pd.DataFrame, swing_length: int = 5) -> pd.DataFrame:
    """BOS/CHoCH dari candle yang sudah close."""
    df_closed = _prep(df)
    swing = SMC.swing_highs_lows(df_closed, swing_length=swing_length)
    return SMC.bos_choch(df_closed, swing, close_break=True)


def get_order_blocks(df: pd.DataFrame, swing_length: int = 5) -> pd.DataFrame:
    """
    OB dari candle yang sudah close.
    close_mitigation=True: OB mitigated jika harga close menembus zona.
    """
    df_closed = _prep(df)
    swing = SMC.swing_highs_lows(df_closed, swing_length=swing_length)
    return SMC.ob(df_closed, swing, close_mitigation=True)


def get_liquidity(df: pd.DataFrame, swing_length: int = 5,
                   range_percent: float = 0.01) -> pd.DataFrame:
    """
    EQH/EQL (equal highs/lows -- liquidity pools) dari candle yang
    sudah close (anti-repaint via _prep).

    FASE 2 (v2.0 Bab 3.3 Tahap 2): dipakai structure.py untuk
    membangun get_active_liquidity_zones().

    Return DataFrame dgn kolom: Liquidity (1=EQH/grouped highs,
    -1=EQL/grouped lows), Level (harga rata-rata grup), End (index
    swing terakhir dalam grup), Swept (index candle yang menyapu
    liquidity ini; 0 = BELUM disapu / masih fresh -- persis pola
    MitigatedIndex==0 di get_active_fvg()).

    range_percent: toleransi pengelompokan swing high/low yang
    dianggap "equal" (default 1% dari total range candle).
    """
    df_closed = _prep(df)
    swing = SMC.swing_highs_lows(df_closed, swing_length=swing_length)
    return SMC.liquidity(df_closed, swing, range_percent=range_percent)


# =====================================================
# ANTI-REPAINT SNAPSHOT (untuk Tier 1 scanner)
# =====================================================
def get_confirmed_snapshot(df: pd.DataFrame, config: dict) -> dict:
    """
    Return snapshot anti-repaint dari candle[-2]:
    - adx, plus_di, minus_di
    - atr_current, atr_avg_14
    - ema20, ema50, gap_now, gap_prev
    - last_confirmed swing high/low (dari library)
    """
    atr_cfg = config["atr"]
    ema_cfg = config["ema"]
    adx_cfg = config["adx"]

    atr_series = atr(df, atr_cfg["period"])
    adx_df     = adx(df, adx_cfg["period"])
    ema_fast   = ema(df["close"], ema_cfg["fast"])
    ema_slow   = ema(df["close"], ema_cfg["slow"])

    idx = -atr_cfg["read_offset"]      # candle[-2]
    idx_prev5 = idx - 5               # candle[-7]
    avg_days  = atr_cfg["avg_period_days"]

    # Swing high/low dari library (anti-repaint: df[:-1])
    swing = get_swing_highs_lows(df, swing_length=5)
    sh = swing[swing["HighLow"] == 1]
    sl = swing[swing["HighLow"] == -1]

    last_sh = {"price": float(sh["Level"].iloc[-1]),
               "index": int(sh.index[-1])} if len(sh) > 0 else None
    last_sl = {"price": float(sl["Level"].iloc[-1]),
               "index": int(sl.index[-1])} if len(sl) > 0 else None

    return {
        "adx":       round(float(adx_df["adx"].iloc[idx]), 2),
        "plus_di":   round(float(adx_df["plus_di"].iloc[idx]), 2),
        "minus_di":  round(float(adx_df["minus_di"].iloc[idx]), 2),
        "atr_current": round(float(atr_series.iloc[idx]), 6),
        "atr_avg_14":  round(float(atr_series.iloc[idx-avg_days:idx].mean()), 6),
        "ema20":     round(float(ema_fast.iloc[idx]), 6),
        "ema50":     round(float(ema_slow.iloc[idx]), 6),
        "ema20_prev5": round(float(ema_fast.iloc[idx_prev5]), 6),
        "ema50_prev5": round(float(ema_slow.iloc[idx_prev5]), 6),
        "gap_now":   round(abs(float(ema_fast.iloc[idx]) - float(ema_slow.iloc[idx])), 6),
        "gap_prev":  round(abs(float(ema_fast.iloc[idx_prev5]) - float(ema_slow.iloc[idx_prev5])), 6),
        "last_swing_high": last_sh,
        "last_swing_low":  last_sl,
        "reference_candle_minus2": str(df["time"].iloc[idx]),
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    from config_loader import load_config
    import mt5_connector as mt5c

    config = load_config()

    print("=" * 55)
    print("TEST: indicators.py v2 (smartmoneyconcepts engine)")
    print("=" * 55)

    if not mt5c.connect():
        sys.exit(1)

    symbol = mt5c.resolve_symbol("EURUSD", config)
    df = mt5c.get_candles(symbol, "H1", count=150)
    print(f"Data: {len(df)} candle H1 ({symbol})")

    snap = get_confirmed_snapshot(df, config)
    print("\n--- Snapshot (candle[-2]) ---")
    for k, v in snap.items():
        print(f"  {k:30s}: {v}")

    print("\n--- FVG (5 valid terakhir) ---")
    fvg = get_fvg(df)
    fvg_valid = fvg[fvg["FVG"].notna() & (fvg["FVG"] != 0)].tail(5)
    if len(fvg_valid) > 0:
        print(fvg_valid[["FVG","Top","Bottom","MitigatedIndex"]].to_string())
    else:
        print("  Tidak ada FVG dalam data ini")

    print("\n--- BOS/CHoCH (5 valid terakhir) ---")
    bos = get_bos_choch(df)
    bos_valid = bos[bos["BOS"].notna() & (bos["BOS"] != 0)].tail(5)
    if len(bos_valid) > 0:
        print(bos_valid[["BOS","CHOCH","Level","BrokenIndex"]].to_string())
    else:
        print("  Tidak ada BOS dalam data ini")

    print("\n--- LIQUIDITY / EQH-EQL (5 valid terakhir) ---")
    liq = get_liquidity(df)
    liq_valid = liq[liq["Liquidity"].notna()].tail(5)
    if len(liq_valid) > 0:
        print(liq_valid[["Liquidity", "Level", "End", "Swept"]].to_string())
    else:
        print("  Tidak ada liquidity pool dalam data ini")

    mt5c.shutdown()
    print("\n[SELESAI] indicators.py v2 berhasil.")
