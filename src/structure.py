"""
structure.py - REBUILD v2

Deteksi trend, BOS, FVG menggunakan smartmoneyconcepts library.
Tier 1 logic: semua matematis, 0 token.
Claude hanya dipanggil untuk grey zone ADX (task 1).
"""

import os, sys
from pathlib import Path

os.environ["SMC_CREDIT"] = "0"
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import load_config
from indicators import get_confirmed_snapshot, get_fvg, get_bos_choch, get_swing_highs_lows, get_liquidity
from claude_client import call_claude


def detect_trend(df: pd.DataFrame, config: dict,
                 pair: str = "UNKNOWN",
                 use_claude_for_grey: bool = True) -> dict:
    """
    Deteksi trend dari H1:
    - ADX > 25 + swing structure  = trending
    - ADX < 20                    = ranging
    - ADX 20-25                   = grey zone → Claude (task 1)
    """
    adx_cfg = config["adx"]
    snap = get_confirmed_snapshot(df, config)
    adx_val = snap["adx"]

    # Tentukan arah dari swing high/low
    sh = snap["last_swing_high"]
    sl = snap["last_swing_low"]
    swing = get_swing_highs_lows(df, swing_length=5)
    highs = swing[swing["HighLow"] == 1]["Level"].tail(3).values
    lows  = swing[swing["HighLow"] == -1]["Level"].tail(3).values

    hh_hl = (len(highs) >= 2 and highs[-1] > highs[-2] and
              len(lows)  >= 2 and lows[-1]  > lows[-2])
    lh_ll = (len(highs) >= 2 and highs[-1] < highs[-2] and
              len(lows)  >= 2 and lows[-1]  < lows[-2])

    trending_above = adx_cfg["trending_above"]
    ranging_below  = adx_cfg["ranging_below"]

    if adx_val < ranging_below:
        return {"trend": "ranging", "adx": adx_val,
                "direction": None, "hh_hl": hh_hl,
                "lh_ll": lh_ll, "source": "rules"}

    if adx_val > trending_above:
        if hh_hl:
            return {"trend": "uptrend", "adx": adx_val,
                    "direction": "bullish", "hh_hl": hh_hl,
                    "lh_ll": lh_ll, "source": "rules"}
        elif lh_ll:
            return {"trend": "downtrend", "adx": adx_val,
                    "direction": "bearish", "hh_hl": hh_hl,
                    "lh_ll": lh_ll, "source": "rules"}
        else:
            return {"trend": "ranging", "adx": adx_val,
                    "direction": None, "hh_hl": hh_hl,
                    "lh_ll": lh_ll, "source": "rules"}

    # Grey zone 20-25
    if not use_claude_for_grey:
        return {"trend": "ranging", "adx": adx_val,
                "direction": None, "hh_hl": hh_hl,
                "lh_ll": lh_ll, "source": "rules_fallback"}

    result = call_claude("adx_grey_zone", {
        "pair": pair, "adx": adx_val,
        "gap_now": snap["gap_now"], "gap_prev": snap["gap_prev"],
        "ema20": snap["ema20"], "ema50": snap["ema50"],
    })
    if result.get("result") == "trending":
        direction = result.get("direction")
        trend = "uptrend" if direction == "bullish" else "downtrend"
    else:
        trend, direction = "ranging", None

    return {"trend": trend, "adx": adx_val,
            "direction": direction, "hh_hl": hh_hl,
            "lh_ll": lh_ll, "source": "claude"}


def get_active_fvg(df: pd.DataFrame) -> list:
    """Ambil FVG yang belum termitigasi (MitigatedIndex == 0)."""
    fvg = get_fvg(df)
    active = fvg[(fvg["FVG"] != 0) & (fvg["MitigatedIndex"] == 0)]
    result = []
    for i, row in active.iterrows():
        result.append({
            "type": "bullish" if row["FVG"] == 1 else "bearish",
            "top": round(float(row["Top"]), 5),
            "bottom": round(float(row["Bottom"]), 5),
            "index": i,
        })
    return result


def get_active_liquidity_zones(df_h1: pd.DataFrame, atr_val: float,
                                config: dict) -> list:
    """
    EQH/EQL (equal highs/lows -- liquidity pools) sebagai zona penting.
    FASE 2 (v2.0 Bab 3.3 Tahap 2).

    Sesuai v2.0 Bab 3 (definisi zona = hukum pembentuknya): EQH/EQL
    dibentuk oleh KELOMPOK swing high/low yang berdekatan (magnet
    likuiditas institusional). "Hukum lebar"-nya berbeda dari OB
    (yang punya lebar candle asli) -- EQH/EQL pada dasarnya LEVEL
    presisi, jadi lebar zonanya tipis (level ± ATR*0.2), sama seperti
    perlakuan SBR/RBS di atas.

    ARAH ZONA (konsisten dgn perlakuan PDH/PDL sbg SnR, bukan bias):
    - EQH (grouped highs, Liquidity==1) -> direction "bearish"
      (zona resistance/reversal -- di atas sini rawan liquidity grab
      lalu turun; Tier 2 yang menilai apakah ini breakout atau grab)
    - EQL (grouped lows, Liquidity==-1) -> direction "bullish"
      (zona support/reversal, cermin dari EQH)

    HANYA liquidity yang BELUM disapu (Swept==0) yang dikembalikan --
    persis pola freshness MitigatedIndex==0 di get_active_fvg().
    Sekali disapu, liquidity itu "sudah dipakai" institusi -- bukan
    target valid lagi (sama seperti OB yang termitigasi didrop).

    Return: list of dict {
        "type": "EQH" | "EQL",
        "direction": "bullish" | "bearish",
        "level": float,
        "zone_top": float,
        "zone_bottom": float,
    }
    """
    liq_cfg = config.get("liquidity", {})
    swing_length = liq_cfg.get("swing_length", 5)
    range_percent = liq_cfg.get("range_percent", 0.01)
    zone_buffer_atr = liq_cfg.get("zone_buffer_atr", 0.2)

    liq_df = get_liquidity(df_h1, swing_length=swing_length,
                            range_percent=range_percent)

    valid = liq_df[liq_df["Liquidity"].notna() & (liq_df["Swept"] == 0)]

    zones = []
    for _, row in valid.iterrows():
        level = float(row["Level"])
        buffer = atr_val * zone_buffer_atr

        if row["Liquidity"] == 1:
            zone_type, direction = "EQH", "bearish"
        else:
            zone_type, direction = "EQL", "bullish"

        zones.append({
            "type": zone_type,
            "direction": direction,
            "level": level,
            "zone_top": round(level + buffer, 5),
            "zone_bottom": round(level - buffer, 5),
        })

    return zones


def get_active_swing_structure_zones(df_h1: pd.DataFrame, atr_val: float,
                                      config: dict) -> list:
    """
    Struktur swing FRESH (belum ditembus) sebagai zona penting.
    FASE 2 (v2.0 Bab 3.3 Tahap 3) -- dikunci sesi Opus:

    "Dalam trend yang sedang berjalan, struktur H1 hanya terdiri
    dari tiga elemen fresh: Swing High (resistance), RBS/SBR
    (support/resistance baru pasca-BOS), dan Swing Low (support
    kritis, penembusannya = CHoCH)."

    RBS/SBR (level yang SUDAH ditembus, lalu flip peran) sudah
    ditangani get_sbr_rbs_zones(). Fungsi ini melengkapi sisi
    SEBALIKNYA: swing high/low yang BELUM PERNAH ditembus BOS --
    masih murni sebagai resistance/support di depan harga.

    Cara mekanis membedakan "sudah ditembus" vs "belum": cocokkan
    level tiap swing high/low terhadap kumpulan Level dari SEMUA
    event BOS/CHoCH historis (yang sudah tercatat menembus level
    itu). Kalau levelnya TIDAK muncul di daftar BOS/CHoCH manapun
    (dalam toleransi kecil) -> berarti belum pernah ditembus -> fresh.

    ARAH ZONA:
    - Swing High belum ditembus -> direction "bearish" (resistance,
      cermin EQH)
    - Swing Low belum ditembus  -> direction "bullish" (support
      kritis, cermin EQL)

    Return: list of dict {
        "type": "SWING_HIGH" | "SWING_LOW",
        "direction": "bullish" | "bearish",
        "level": float,
        "zone_top": float,
        "zone_bottom": float,
    }
    """
    sw_cfg = config.get("swing_structure", {})
    zone_buffer_atr = sw_cfg.get("zone_buffer_atr", 0.2)
    max_zones = sw_cfg.get("max_zones_per_type", 1)
    match_tolerance_atr = sw_cfg.get("broken_level_tolerance_atr", 0.1)

    swing = get_swing_highs_lows(df_h1, swing_length=5)
    bos_choch_df = get_bos_choch(df_h1)

    # Kumpulkan SEMUA level yang pernah tercatat sbg BOS/CHoCH
    # (level itu berarti SUDAH ditembus -- exclude dari "fresh")
    broken_levels = bos_choch_df.loc[
        bos_choch_df["Level"].notna(), "Level"
    ].astype(float).tolist()

    tol = atr_val * match_tolerance_atr

    def _is_broken(level: float) -> bool:
        return any(abs(level - bl) <= tol for bl in broken_levels)

    highs = swing[swing["HighLow"] == 1]
    lows = swing[swing["HighLow"] == -1]

    zones = []

    # Swing high yang BELUM ditembus (fresh resistance), ambil
    # yang paling baru (max_zones_per_type terakhir)
    fresh_highs = [float(lvl) for lvl in highs["Level"].tolist() if not _is_broken(float(lvl))]
    for level in fresh_highs[-max_zones:]:
        buffer = atr_val * zone_buffer_atr
        zones.append({
            "type": "SWING_HIGH", "direction": "bearish", "level": level,
            "zone_top": round(level + buffer, 5),
            "zone_bottom": round(level - buffer, 5),
        })

    # Swing low yang BELUM ditembus (fresh support kritis)
    fresh_lows = [float(lvl) for lvl in lows["Level"].tolist() if not _is_broken(float(lvl))]
    for level in fresh_lows[-max_zones:]:
        buffer = atr_val * zone_buffer_atr
        zones.append({
            "type": "SWING_LOW", "direction": "bullish", "level": level,
            "zone_top": round(level + buffer, 5),
            "zone_bottom": round(level - buffer, 5),
        })

    return zones


def get_active_supply_demand_zones(df_h1: pd.DataFrame, atr_val: float,
                                    config: dict) -> list:
    """
    Zona Supply/Demand dari pola BASING + DISPLACEMENT.
    FASE 2 (v2.0 Bab 3.3 Tahap 4) -- definisi dikunci v2.0 Bab 3.1 +
    v2.1 addendum:

    BASING: N candle berurutan (min_basing_candles..max_basing_candles)
      dengan total range (high tertinggi - low terendah dalam grup)
      <= basing_range_atr_multiplier * ATR, DAN tiap candle di grup
      punya body kecil (|close-open| / (high-low) <= basing_body_
      ratio_max) -- tanda konsolidasi/akumulasi institusional.

    DISPLACEMENT: SATU candle SETELAH basing dengan range (high-low)
      >= displacement_atr_multiplier * ATR, DAN close-nya menembus
      KELUAR dari rentang basing (di atas basing_high = demand
      breakout / di bawah basing_low = supply breakout).

    ZONA = rentang [basing_low, basing_high] itu sendiri (tempat
    price kembali di-retest sebagai entry, searah displacement).

    ARAH ZONA:
    - Displacement naik (close > basing_high) -> DEMAND -> "bullish"
    - Displacement turun (close < basing_low) -> SUPPLY -> "bearish"

    Anti-repaint: exclude candle[-1] (belum close), sama seperti
    seluruh modul ini.

    Return: list of dict {
        "type": "SD",
        "direction": "bullish" | "bearish",
        "zone_top": float,
        "zone_bottom": float,
        "basing_candles": int,
        "displacement_range": float,
    }
    """
    sd_cfg = config.get("supply_demand", {})
    min_basing = sd_cfg.get("min_basing_candles", 3)
    max_basing = sd_cfg.get("max_basing_candles", 6)
    basing_range_atr_mult = sd_cfg.get("basing_range_atr_multiplier", 1.5)
    body_ratio_max = sd_cfg.get("basing_body_ratio_max", 0.5)
    displacement_atr_mult = sd_cfg.get("displacement_atr_multiplier", 2.0)
    max_zones = sd_cfg.get("max_zones", 5)

    df_closed = df_h1.iloc[:-1].reset_index(drop=True)  # anti-repaint
    n = len(df_closed)
    zones = []

    # Scan MUNDUR dari candle paling baru -- prioritaskan zona
    # yang paling relevan (dekat dengan harga sekarang) dulu
    for end_idx in range(n - 1, max_basing, -1):
        if len(zones) >= max_zones:
            break

        displacement = df_closed.iloc[end_idx]
        disp_range = float(displacement["high"] - displacement["low"])
        if disp_range < displacement_atr_mult * atr_val:
            continue  # bukan candle displacement, lewati

        for basing_len in range(min_basing, max_basing + 1):
            basing_start = end_idx - basing_len
            if basing_start < 0:
                break
            basing = df_closed.iloc[basing_start:end_idx]

            basing_high = float(basing["high"].max())
            basing_low = float(basing["low"].min())
            basing_range = basing_high - basing_low
            if basing_range > basing_range_atr_mult * atr_val:
                continue  # basing terlalu lebar, bukan konsolidasi sejati

            bodies_ok = True
            for _, c in basing.iterrows():
                candle_range = float(c["high"] - c["low"])
                if candle_range == 0:
                    continue
                body = abs(float(c["close"]) - float(c["open"]))
                if body / candle_range > body_ratio_max:
                    bodies_ok = False
                    break
            if not bodies_ok:
                continue

            disp_close = float(displacement["close"])
            if disp_close > basing_high:
                direction = "bullish"   # demand, breakout naik
            elif disp_close < basing_low:
                direction = "bearish"   # supply, breakout turun
            else:
                continue  # displacement tidak benar2 menembus basing

            zones.append({
                "type": "SD",
                "direction": direction,
                "zone_top": round(basing_high, 5),
                "zone_bottom": round(basing_low, 5),
                "basing_candles": basing_len,
                "displacement_range": round(disp_range, 5),
            })
            break  # basing_len terkecil yang cocok, lanjut ke end_idx berikutnya

    return zones


def get_recent_bos(df: pd.DataFrame) -> list:
    """Ambil BOS valid terbaru (5 terakhir, filter NaN)."""
    bos = get_bos_choch(df)
    # Filter: BOS != 0, tidak NaN, level tidak NaN
    active = bos[
        bos["BOS"].notna() &
        (bos["BOS"] != 0) &
        bos["Level"].notna()
    ].tail(5)
    result = []
    for i, row in active.iterrows():
        result.append({
            "direction": "bullish" if row["BOS"] == 1 else "bearish",
            "level": round(float(row["Level"]), 5),
            "broken_index": int(row["BrokenIndex"]) if pd.notna(row["BrokenIndex"]) else None,
        })
    return result


def get_sbr_rbs_zones(df_h1: pd.DataFrame, current_price: float,
                       atr_val: float, config: dict) -> list:
    """
    Identifikasi zona SBR/RBS dari level BOS H1.

    Referensi riset (alchemymarkets.com, innercircletrader.net,
    xs.com/breaker-block, threads.com/@ict_innercircle_trader):

    RBS (Resistance Becomes Support):
    - Bullish BOS: harga menembus swing high ke atas
    - Level swing high yang ditembus itu kini jadi SUPPORT
    - Entry: tunggu harga pullback TURUN ke level tersebut
    - Cocok untuk setup BUY

    SBR (Support Becomes Resistance):
    - Bearish BOS: harga menembus swing low ke bawah
    - Level swing low yang ditembus itu kini jadi RESISTANCE
    - Entry: tunggu harga pullback NAIK ke level tersebut
    - Cocok untuk setup SELL

    Fibonacci bonus confidence: cek apakah level SBR/RBS ini
    bertepatan dengan area Fibonacci 0.5-0.786 dari swing terakhir.
    Tidak wajib — hanya dicatat sebagai info tambahan.

    Filter in_range: sama dengan OB (2×ATR dari harga sekarang).
    Filter price_in_zone: harga sudah retrace ke level (±ATR*0.5).

    Return: list of dict {
        "type": "RBS" | "SBR",
        "direction": "bullish" | "bearish",  # arah entry
        "level": float,
        "zone_top": float,   # level + ATR*0.2 (zona tipis)
        "zone_bottom": float, # level - ATR*0.2
        "in_range": bool,
        "price_in_zone": bool,
        "fib_confluence": bool,  # bonus, tidak wajib
    }
    """
    bos_list = get_recent_bos(df_h1)
    if not bos_list:
        return []

    # Fibonacci bonus: ambil swing high/low terbaru untuk reference
    snap = get_confirmed_snapshot(df_h1, config)

    # AUDIT FIX: last_swing_high/low dari get_confirmed_snapshot() 
    # mengembalikan DICT {"price": float, "index": int}, bukan float.
    # Kode sebelumnya langsung membandingkan dict dengan dict:
    # "if swing_high and swing_low and swing_high > swing_low"
    # → TypeError: '>' not supported between instances of 'dict' and 'dict'
    sh_dict = snap.get("last_swing_high")
    sl_dict = snap.get("last_swing_low")
    swing_high_price = sh_dict["price"] if sh_dict else None
    swing_low_price  = sl_dict["price"] if sl_dict else None

    fib_levels = []
    if (swing_high_price is not None and swing_low_price is not None
            and swing_high_price > swing_low_price):
        swing_range = swing_high_price - swing_low_price
        fib_levels = [
            swing_high_price - 0.5   * swing_range,  # Fib 0.5
            swing_high_price - 0.618 * swing_range,  # Fib 0.618
            swing_high_price - 0.786 * swing_range,  # Fib 0.786
        ]

    zone_buffer = atr_val * 0.2   # zona tipis ±20% ATR di sekitar level
    in_range_radius = atr_val * 2.0
    price_in_zone_tolerance = atr_val * 0.5  # harga harus dalam 50% ATR dari level
    fib_tolerance = atr_val * 0.3   # level harus dalam 30% ATR dari angka Fib

    result = []
    for bos in bos_list:
        level = bos["level"]

        # Tentukan tipe SBR/RBS berdasarkan arah BOS
        if bos["direction"] == "bullish":
            zone_type = "RBS"        # Resistance jadi Support
            entry_direction = "bullish"  # BUY saat harga retrace ke level ini
        else:
            zone_type = "SBR"        # Support jadi Resistance
            entry_direction = "bearish"  # SELL saat harga retrace ke level ini

        dist = abs(current_price - level)
        in_range = dist <= in_range_radius
        price_in_zone = dist <= price_in_zone_tolerance

        # Cek Fibonacci confluence (bonus)
        fib_confluence = any(
            abs(level - fib) <= fib_tolerance
            for fib in fib_levels
        ) if fib_levels else False

        result.append({
            "type": zone_type,
            "direction": entry_direction,
            "level": level,
            "zone_top": round(level + zone_buffer, 5),
            "zone_bottom": round(level - zone_buffer, 5),
            "in_range": in_range,
            "price_in_zone": price_in_zone,
            "fib_confluence": fib_confluence,
        })

    return result


if __name__ == "__main__":
    import mt5_connector as mt5c
    config = load_config()

    print("=" * 55)
    print("TEST: structure.py v2")
    print("=" * 55)

    if not mt5c.connect():
        sys.exit(1)

    symbol = mt5c.resolve_symbol("EURUSD", config)
    df = mt5c.get_candles(symbol, "H1", count=150)

    print("\n--- TREND ---")
    trend = detect_trend(df, config, pair="EURUSD")
    for k, v in trend.items():
        print(f"  {k}: {v}")

    print("\n--- FVG AKTIF ---")
    fvgs = get_active_fvg(df)
    if fvgs:
        for f in fvgs[-3:]:
            print(f"  [{f['type']:8s}] {f['bottom']:.5f} - {f['top']:.5f}")
    else:
        print("  Tidak ada FVG aktif")

    print("\n--- BOS TERBARU ---")
    bos_list = get_recent_bos(df)
    for b in bos_list:
        print(f"  [{b['direction']:8s}] level: {b['level']:.5f}")

    print("\n--- SBR/RBS ZONES ---")
    from indicators import get_confirmed_snapshot, atr as calc_atr
    snap = get_confirmed_snapshot(df, config)
    current_price = float(df["close"].iloc[-2])
    atr_val = snap["atr_current"]
    zones = get_sbr_rbs_zones(df, current_price, atr_val, config)
    if zones:
        for z in zones:
            print(f"  [{z['type']:4s}] level={z['level']:.5f} "
                  f"in_range={z['in_range']} "
                  f"price_in_zone={z['price_in_zone']} "
                  f"fib_conf={z['fib_confluence']}")
    else:
        print("  Tidak ada zona SBR/RBS")

    print("\n--- EQH/EQL (LIQUIDITY) ZONES ---")
    liq_zones = get_active_liquidity_zones(df, atr_val, config)
    if liq_zones:
        for z in liq_zones:
            print(f"  [{z['type']:4s}] direction={z['direction']:8s} "
                  f"level={z['level']:.5f} "
                  f"zona=[{z['zone_bottom']:.5f}-{z['zone_top']:.5f}]")
    else:
        print("  Tidak ada zona EQH/EQL aktif")

    print("\n--- SWING STRUCTURE FRESH ZONES ---")
    swing_zones = get_active_swing_structure_zones(df, atr_val, config)
    if swing_zones:
        for z in swing_zones:
            print(f"  [{z['type']:10s}] direction={z['direction']:8s} "
                  f"level={z['level']:.5f} "
                  f"zona=[{z['zone_bottom']:.5f}-{z['zone_top']:.5f}]")
    else:
        print("  Tidak ada zona swing structure fresh")

    print("\n--- SUPPLY/DEMAND ZONES ---")
    sd_zones = get_active_supply_demand_zones(df, atr_val, config)
    if sd_zones:
        for z in sd_zones:
            print(f"  [{z['type']:4s}] direction={z['direction']:8s} "
                  f"zona=[{z['zone_bottom']:.5f}-{z['zone_top']:.5f}] "
                  f"basing={z['basing_candles']}candle "
                  f"disp={z['displacement_range']:.5f}")
    else:
        print("  Tidak ada zona Supply/Demand")

    mt5c.shutdown()
    print("\n[SELESAI] structure.py v2")
