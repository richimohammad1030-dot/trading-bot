"""
spread_filter.py - Step 11

Filter spread sebelum entry, sesuai blueprint v16.

Referensi riset:
- nyao_scalper_mt5 (GitHub): "Max-Spread Filter: blocks new entries
  when spread too wide (fixed MaxSpreadPoints, atau auto-detect)"
  → Validasi pendekatan dual-mode (fixed + relative)
- MQL5 forum (471015, 458576): spread melebar signifikan saat
  market session gap (21:30-01:40 UTC, Sydney→Tokyo) dan saat
  likuiditas rendah/news release
- earnforex.com: threshold sebaiknya relatif ke average spread
  pair tersebut (XAUUSD vs EURUSD punya baseline yang sangat
  berbeda, fixed point tidak universal)

DESAIN:
Dual-mode check (keduanya harus lolos):
1. ABSOLUTE: spread saat ini <= max_spread_pip (dari config, per pair)
2. RELATIVE: spread saat ini <= rolling_avg_spread × multiplier
   (menangkap "widening" abnormal meski masih di bawah absolute max)

MT5 sudah expose symbol_info().spread secara real-time (dalam points),
tidak perlu hitung manual — ini operasi sangat ringan (0 API call
tambahan, hanya baca dari koneksi MT5 yang sudah ada).

REVISI (Step 5, perbaikan normalisasi pip/point, Addendum v2.3 Bab 3.6):
get_current_spread_pip() SEBELUMNYA menghitung konversi point->pip
sendiri (digits in (5,3) -> bagi 10). Ini SALAH SATU dari 4+1
implementasi pip berbeda yang diaudit (lihat
TEMUAN_normalisasi_pip_point.md) -- kebetulan BENAR untuk 5 pair aktif
Anda, tapi tidak boleh ada 2 sumber kebenaran. Sekarang didelegasikan
ke mt5_connector.get_spread_pip() (SATU-SATUNYA fungsi pip terpusat).
get_rolling_avg_spread() TIDAK diubah -- ia menghitung rata-rata dari
KOLOM 'spread' candle historis (bukan symbol_info live), levelnya
berbeda dan tidak termasuk 4 implementasi yang diaudit.
"""

import sys
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import load_config
import mt5_connector as mt5c


def get_current_spread_pip(symbol: str) -> dict:
    """
    Ambil spread real-time dari MT5 dalam pip.

    REVISI Step 5: nilai spread_pip SEKARANG dihitung via
    mt5_connector.get_spread_pip() (fungsi pip terpusat, Addendum
    v2.3 Bab 3.6) -- bukan konversi manual di sini lagi. bid/ask/
    spread_points tetap dibaca langsung (bukan bagian dari konversi
    pip, tidak perlu didelegasikan).

    Return: dict {
        "spread_points": int,
        "spread_pip": float,
        "bid": float, "ask": float,
    }
    """
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)

    if info is None or tick is None:
        return {"spread_points": None, "spread_pip": None,
                "bid": None, "ask": None}

    spread_points = info.spread

    # Safety check: bid/ask = 0 berarti symbol belum subscribe
    # tick data real-time (market watch belum aktif untuk symbol ini)
    if tick.bid == 0 or tick.ask == 0:
        return {"spread_points": None, "spread_pip": None,
                "bid": tick.bid, "ask": tick.ask}

    # REVISI: delegasi ke fungsi pip terpusat (bukan hitung sendiri).
    # Bisa raise RuntimeError jika symbol_info bermasalah -- SENGAJA
    # tidak di-silent-catch, konsisten filosofi "gagal harus terlihat"
    # (claude_client.py, package_builder.py).
    spread_pip = mt5c.get_spread_pip(symbol)

    return {
        "spread_points": spread_points,
        "spread_pip": round(spread_pip, 2),
        "bid": tick.bid,
        "ask": tick.ask,
    }


def get_rolling_avg_spread(symbol: str, df_m30: pd.DataFrame = None,
                            lookback: int = 20) -> float:
    """
    Hitung rolling average spread dari N candle M30 terakhir.

    MT5 rates_get() include kolom 'spread' (dalam points) di setiap
    candle historis — ini data yang sudah ada, tidak perlu fetch
    tambahan jika df_m30 sudah di-pass dari caller.

    Jika df_m30 tidak disediakan, fetch sendiri dari MT5.

    CATATAN: fungsi ini TIDAK termasuk 4 implementasi pip yang diaudit
    (TEMUAN_normalisasi_pip_point.md) -- ia menghitung dari kolom
    'spread' candle historis (points), bukan symbol_info live. Tetap
    pakai konversi digit->divisor manual di sini karena levelnya beda
    (rata-rata historis, bukan pip_size sesaat) -- TIDAK diubah di
    Step 5 ini.
    """
    if df_m30 is None or "spread" not in df_m30.columns:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M30,
                                         0, lookback + 1)
        if rates is None or len(rates) == 0:
            return None
        df_m30 = pd.DataFrame(rates)

    info = mt5.symbol_info(symbol)
    if info is None:
        return None

    digits = info.digits
    divisor = 10.0 if digits in (5, 3) else 1.0

    # Exclude candle terakhir (anti-repaint, mungkin masih live)
    recent_spreads = df_m30["spread"].iloc[:-1].tail(lookback)
    if len(recent_spreads) == 0:
        return None

    avg_points = recent_spreads.mean()
    return round(avg_points / divisor, 2)


def check_spread(symbol: str, config: dict,
                  df_m30: pd.DataFrame = None) -> dict:
    """
    Cek apakah spread saat ini layak untuk entry.
    Dual-mode: absolute max (skip_above) DAN relative ke baseline.
    
    Menggunakan config format yang sudah ada di config.yaml:
    spread:
      EURUSD: { baseline: 0.4, skip_above: 1.0 }
      ...
    
    Args:
        symbol: nama symbol broker (misal EURUSDm)
        config: config dict (berisi spread.<PAIR>.baseline/skip_above)
        df_m30: optional, candle M30 yang sudah di-fetch caller
                (hindari fetch ulang jika sudah ada)
    
    Return: dict {
        "allowed": bool,
        "reason": str | None,
        "current_spread_pip": float,
        "max_allowed_pip": float,
        "baseline_pip": float,
        "rolling_avg_pip": float | None,
        "relative_ratio": float | None,
    }
    """
    pair_clean = symbol.replace("m", "").replace(".raw", "").upper()
    
    spread_cfg = config.get("spread", {})
    pair_cfg = spread_cfg.get(pair_clean, {"baseline": 1.0, "skip_above": 3.0})
    baseline = pair_cfg.get("baseline", 1.0)
    max_allowed = pair_cfg.get("skip_above", baseline * 2.5)
    
    current = get_current_spread_pip(symbol)
    if current["spread_pip"] is None:
        return {
            "allowed": False,
            "reason": "symbol_info_unavailable",
            "current_spread_pip": None,
            "max_allowed_pip": max_allowed,
            "baseline_pip": baseline,
            "rolling_avg_pip": None,
            "relative_ratio": None,
        }
    
    current_pip = current["spread_pip"]
    
    # CHECK 1: Absolute max (skip_above dari config blueprint)
    if current_pip > max_allowed:
        return {
            "allowed": False,
            "reason": f"absolute_exceeded ({current_pip} > {max_allowed} pip)",
            "current_spread_pip": current_pip,
            "max_allowed_pip": max_allowed,
            "baseline_pip": baseline,
            "rolling_avg_pip": None,
            "relative_ratio": None,
        }
    
    # CHECK 2: Relative ke rolling average (deteksi widening abnormal
    # meski masih di bawah skip_above absolut)
    rolling_avg = get_rolling_avg_spread(symbol, df_m30)
    relative_ratio = None
    relative_multiplier = spread_cfg.get("relative_multiplier", 2.5)
    
    if rolling_avg is not None and rolling_avg > 0:
        relative_ratio = round(current_pip / rolling_avg, 2)
        if relative_ratio > relative_multiplier:
            return {
                "allowed": False,
                "reason": (f"relative_widening ({current_pip} pip = "
                          f"{relative_ratio}x avg {rolling_avg} pip)"),
                "current_spread_pip": current_pip,
                "max_allowed_pip": max_allowed,
                "baseline_pip": baseline,
                "rolling_avg_pip": rolling_avg,
                "relative_ratio": relative_ratio,
            }
    
    return {
        "allowed": True,
        "reason": None,
        "current_spread_pip": current_pip,
        "max_allowed_pip": max_allowed,
        "baseline_pip": baseline,
        "rolling_avg_pip": rolling_avg,
        "relative_ratio": relative_ratio,
    }


if __name__ == "__main__":
    import mt5_connector as mt5c
    
    config = load_config()
    
    print("=" * 60)
    print("TEST: spread_filter.py (Step 5 -- delegasi ke pip terpusat)")
    print("=" * 60)
    
    if not mt5c.connect():
        sys.exit(1)
    
    pairs = ["EURUSD", "GBPUSD", "AUDUSD", "USDJPY", "XAUUSD"]
    
    print("\n--- TEST 1: Current Spread per Pair ---")
    for pair in pairs:
        symbol = mt5c.resolve_symbol(pair, config)
        s = get_current_spread_pip(symbol)
        if s["spread_pip"] is not None:
            print(f"  {pair:8s}: {s['spread_pip']:.2f} pip "
                  f"(bid={s['bid']}, ask={s['ask']}, "
                  f"points={s['spread_points']})")
        else:
            print(f"  {pair:8s}: tidak tersedia")
    
    print("\n--- TEST 2: Rolling Average Spread (20 candle M30) ---")
    for pair in pairs:
        symbol = mt5c.resolve_symbol(pair, config)
        avg = get_rolling_avg_spread(symbol, lookback=20)
        print(f"  {pair:8s}: avg={avg} pip" if avg is not None
              else f"  {pair:8s}: tidak ada data")
    
    print("\n--- TEST 3: Full Spread Check (dual-mode) ---")
    for pair in pairs:
        symbol = mt5c.resolve_symbol(pair, config)
        result = check_spread(symbol, config)
        status = "✅ ALLOW" if result["allowed"] else "⛔ BLOCK"
        print(f"  {pair:8s}: {status} | "
              f"current={result['current_spread_pip']} pip | "
              f"baseline={result['baseline_pip']} pip | "
              f"skip_above={result['max_allowed_pip']} pip | "
              f"avg20={result['rolling_avg_pip']} pip | "
              f"ratio={result['relative_ratio']}")
        if result["reason"]:
            print(f"            reason: {result['reason']}")
    
    mt5c.shutdown()
    print("\n[SELESAI] spread_filter.py (Step 5) berhasil.")
