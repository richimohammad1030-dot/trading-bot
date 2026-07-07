"""
screener.py - TIER 1 SCREENER MURNI (Fase 1, REVISI: bias H1 via bos_choch)

Menggantikan peran lama signal_engine.py. Ini adalah organ, bukan otak
(v2.0 Bab 1.4). Modul ini TIDAK PERNAH memutuskan "layak entry atau
tidak" -- itu 100% milik Tier 2 (Claude), yang akan dibangun di Fase 4.

REVISI ARSITEKTURAL (sesi Opus, setelah verifikasi diagnostik di data
live): H4 sebagai timeframe bias DIBUANG. Alasan:
  - H4/ADX terlalu lambat untuk kebutuhan intraday (selesai di hari
    yang sama), dan rawan grey zone ADX (bias ambigu, contoh live:
    USDJPY ADX 21.77 -> keputusan Claude tidak stabil).
  - Diagnostik (diag_bias.py v1) membuktikan logika "2-swing manual"
    CACAT -- AUDUSD salah dibaca bullish padahal bos_choch bilang
    bearish (2-swing ketipu bounce sesaat).
  - diag_bias.py v2 (berbasis bos_choch, melacak struktur berurutan
    & internal) TERBUKTI andal di 5 pair live -- 100% sejalan dengan
    EMA20/50 sebagai pembanding independen.

KOMBINASI TIMEFRAME BARU: HANYA 2 timeframe (bukan 3):
    STRUCTURE_TF (default H1) = BIAS + STRUKTUR + AREA PENTING, semua
      dari satu sumber (bos_choch H1), tidak ada lapisan H4 terpisah.
    TIMING_TF (default M5) = eksekusi/gate.

TUGAS TIER 1 (dan HANYA ini):
    1. Tentukan BIAS dari bos_choch di structure_tf (H1) -- OPSI B:
       - Event terakhir BOS + recent konsisten searah -> bias TEGAS
         (bullish/bearish), fase "kontinuitas".
       - Event terakhir CHOCH (atau tidak ada event sama sekali)
         -> bias "ranging/transisi", fase "potensi_choch"/"no_bias".
         Tier 1 TIDAK memaksa satu arah -- zona DUA ARAH dikumpulkan,
         Tier 2 yang menentukan arah mana yang valid.
    2. Kumpulkan & tandai AREA PENTING (Tahap 1: OB + SBR/RBS) --
       SEARAH bias SAJA jika bias tegas; DUA ARAH jika transisi/
       ranging (Opsi B). SEMUA touch_count (freshness = DATA, bukan
       filter, v2.1 Bab 4.2/3.1).
    3. GATE: deteksi "harga masuk area" + candle timing_tf (M5)
       pertama CLOSE di dalam/dekat zona (v2.1 Bab 1.3 -- mekanis,
       BUKAN deteksi rejection/wick yang itu penilaian milik Tier 2).
    4. PRE-FILTER: HANYA spread (v2.0 Bab 4.2). Tidak ada filter lain
       yang menyaring berdasarkan kualitas.
    5. DEBOUNCE: state-flag + cooldown/throttle mekanis (zone_state.py).
    6. ANTI-DOUBLE-POSITION: per-zona (zone_state.py).

Output run_screener_for_pair() adalah PAKET KANDIDAT untuk Tier 2 --
BUKAN sinyal entry final. Paket lengkap (chart markup, data volume/
likuiditas, EMA/ADX sebagai data pendukung) dibangun di Fase 3-4.

TIMEFRAME-AGNOSTIC (v2.0 Bab 1.3, prinsip fraktal) tetap berlaku:
structure_tf/timing_tf dibaca dari config, default H1/M5. Mesin yang
sama bisa dipakai untuk skala scalping (M15/M1) nanti.
"""

import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config_loader import load_config, get_all_pairs
import mt5_connector as mt5c
from spread_filter import check_spread
from indicators import get_confirmed_snapshot, get_bos_choch
from structure import (get_sbr_rbs_zones, get_active_fvg,
                        get_active_liquidity_zones,
                        get_active_supply_demand_zones)
from orderblock import (init_db as init_ob_db, detect_and_save_obs,
                         get_active_obs, check_mitigation, cleanup_stale_obs)
import zone_state as zs


def _isnan(x):
    try:
        return x != x
    except Exception:
        return False


# =====================================================
# 1. BIAS + FASE dari bos_choch (structure_tf, default H1) -- OPSI B
# =====================================================
def determine_bias_and_phase(df_structure: "pd.DataFrame", config: dict,
                              lookback_events: int = 3) -> dict:
    """
    Tentukan BIAS dan FASE STRUKTUR sekaligus dari event BOS/CHoCH
    TERAKHIR di structure_tf (H1). Menggantikan get_bias() [H4/ADX]
    + sebagian classify_structure_phase() versi lama.

    LOGIKA (Opsi B, dikunci sesi Opus setelah verifikasi diag_bias.py):

    1. Tidak ada event BOS/CHoCH sama sekali
       -> bias="ranging", fase="no_bias", is_transitional=True

    2. Event terakhir = BOS, dan event dalam lookback_events KONSISTEN
       searah (semua bullish atau semua bearish)
       -> bias TEGAS (bullish/bearish), fase="kontinuitas",
          is_transitional=False

    3. Event terakhir = BOS, tapi event dalam lookback CAMPUR (ada
       bullish & bearish)
       -> bias TEGAS mengikuti arah event terakhir, fase="retest"
          (struktur besar mixed, tapi BOS terakhir masih jadi acuan),
          is_transitional=False

    4. Event terakhir = CHOCH
       -> bias="ranging" (TIDAK dipaksa ke arah CHoCH -- CHoCH baru
          sinyal AWAL transisi, belum terkonfirmasi jadi trend baru),
          fase="potensi_choch", is_transitional=True
       -> Tier 1 kumpulkan zona DUA ARAH, Tier 2 yang menilai apakah
          reversal sejati atau sekadar pullback (v2.0 Bab 2.1).

    is_transitional=True berarti collect_zones() akan mengambil
    KEDUA arah (bullish & bearish), bukan hanya satu.
    """
    bos_choch_df = get_bos_choch(df_structure)  # anti-repaint internal (_prep)
    valid = bos_choch_df[
        (bos_choch_df["BOS"].notna() & (bos_choch_df["BOS"] != 0)) |
        (bos_choch_df["CHOCH"].notna() & (bos_choch_df["CHOCH"] != 0))
    ]

    if len(valid) == 0:
        return {"bias": "ranging", "phase": "no_bias",
                "is_transitional": True, "reason": "no_bos_choch_events",
                "events": []}

    events = []
    for idx, row in valid.iterrows():
        if row["BOS"] not in (0, None) and not _isnan(row["BOS"]):
            events.append(("BOS", "bullish" if row["BOS"] == 1 else "bearish",
                           float(row["Level"]), int(idx)))
        elif row["CHOCH"] not in (0, None) and not _isnan(row["CHOCH"]):
            events.append(("CHOCH", "bullish" if row["CHOCH"] == 1 else "bearish",
                           float(row["Level"]), int(idx)))

    recent = events[-lookback_events:]
    last_type, last_dir, _, _ = events[-1]

    if last_type == "CHOCH":
        # OPSI B: CHoCH = transisi, TIDAK memaksa satu arah.
        return {"bias": "ranging", "phase": "potensi_choch",
                "is_transitional": True,
                "reason": f"last_event_choch_{last_dir}_not_confirmed",
                "choch_direction": last_dir, "events": events}

    # Event terakhir BOS
    directions = [d for (_, d, _, _) in recent]
    n_bull = directions.count("bullish")
    n_bear = directions.count("bearish")

    if n_bull > 0 and n_bear == 0:
        bias, phase, reason = "bullish", "kontinuitas", f"consistent_bos_bullish_{n_bull}events"
    elif n_bear > 0 and n_bull == 0:
        bias, phase, reason = "bearish", "kontinuitas", f"consistent_bos_bearish_{n_bear}events"
    else:
        bias, phase, reason = last_dir, "retest", f"mixed_recent_last_bos_{last_dir}"

    return {"bias": bias, "phase": phase, "is_transitional": False,
            "reason": reason, "events": events}


# =====================================================
# 2. KUMPULKAN AREA PENTING (zona) -- Tahap 1: OB + SBR/RBS
# =====================================================
def collect_zones(symbol: str, pair: str, df_structure: "pd.DataFrame",
                   config: dict, expected_directions: list) -> list:
    """
    Kumpulkan zona SESUAI expected_directions:
      - Bias TEGAS (kontinuitas/retest)  -> expected_directions = [bias]
        (satu arah saja -- mencegah counter-trend, v2.0 Bab 2.1)
      - Bias TRANSISI/ranging (Opsi B)   -> expected_directions =
        ["bullish", "bearish"] (dua arah -- Tier 2 yang tentukan arah)

    Fase 2 (v2.0 Bab 3.3) -- 5 tipe zona aktif:
      Tahap 1: OB, SBR/RBS
      Tahap 2: FVG, EQH/EQL
      Tahap 4: Supply/Demand (SD, basing + displacement)
    Semua tipe melalui gerbang arah dan pendaftaran identitas yang
    SAMA -- tidak ada perlakuan khusus per tipe di luar cara
    masing-masing terbentuk (v2.0 Bab 3: "zona didefinisikan oleh
    hukum pembentuknya").

    CATATAN: Tahap 3 (struktur swing fresh, SWING_HIGH/SWING_LOW)
    SENGAJA TIDAK diaktifkan di sini. Verifikasi replay_gate_check.py
    menunjukkan tipe ini menyumbang ~70% (204/293) dari seluruh
    kandidat dalam 10 hari replay -- jauh melebihi target volume
    intraday (~1 sinyal final/hari), sementara OB/SBR/RBS/FVG/EQH/
    EQL/SD sudah menyajikan bahan yang cukup kaya untuk Tier 2.
    Fungsi get_active_swing_structure_zones() TETAP ada di
    structure.py (tidak dihapus, hanya tidak dipanggil) -- bisa
    diaktifkan kembali nanti jika data backtest menunjukkan Tier 2
    kekurangan sinyal, kemungkinan dengan ambang tambahan (mis.
    throttle per-pair) untuk mengendalikan volumenya.

    touch_count TETAP tidak difilter (semua freshness level dikirim
    sebagai data ke Tier 2) -- hanya ARAH yang disaring, dan itu pun
    hanya saat bias sudah tegas.
    """
    snap = get_confirmed_snapshot(df_structure, config)
    atr_val = snap["atr_current"]
    current_price = float(df_structure["close"].iloc[-2])  # anti-repaint

    zones = []

    # --- OB (Tahap 1, sudah ada di orderblock.py) ---
    cleanup_stale_obs(pair, current_price, atr_val)
    check_mitigation(pair, df_structure)
    detect_and_save_obs(df_structure, pair, config)
    active_obs = get_active_obs(pair)

    for ob in active_obs:
        if ob["direction"] not in expected_directions:
            continue
        registered = zs.find_or_create_zone(
            pair, "OB", ob["direction"], ob["ob_top"], ob["ob_bottom"]
        )
        zones.append({**registered, "source_detail": ob})

    # --- SBR/RBS (Tahap 1, sudah ada di structure.py) ---
    sbr_rbs = get_sbr_rbs_zones(df_structure, current_price, atr_val, config)
    for z in sbr_rbs:
        if z["direction"] not in expected_directions:
            continue
        registered = zs.find_or_create_zone(
            pair, z["type"], z["direction"], z["zone_top"], z["zone_bottom"]
        )
        zones.append({**registered, "source_detail": z})

    # --- FVG (Tahap 2 BARU): fair value gap yang belum termitigasi ---
    fvgs = get_active_fvg(df_structure)
    for fvg in fvgs:
        if fvg["type"] not in expected_directions:
            continue
        registered = zs.find_or_create_zone(
            pair, "FVG", fvg["type"], fvg["top"], fvg["bottom"]
        )
        zones.append({**registered, "source_detail": fvg})

    # --- EQH/EQL (Tahap 2): liquidity pool yang belum disapu ---
    liquidity_zones = get_active_liquidity_zones(df_structure, atr_val, config)
    for lz in liquidity_zones:
        if lz["direction"] not in expected_directions:
            continue
        registered = zs.find_or_create_zone(
            pair, lz["type"], lz["direction"], lz["zone_top"], lz["zone_bottom"]
        )
        zones.append({**registered, "source_detail": lz})

    # --- Supply/Demand (Tahap 4 BARU): basing + displacement ---
    sd_zones = get_active_supply_demand_zones(df_structure, atr_val, config)
    for sdz in sd_zones:
        if sdz["direction"] not in expected_directions:
            continue
        registered = zs.find_or_create_zone(
            pair, sdz["type"], sdz["direction"], sdz["zone_top"], sdz["zone_bottom"]
        )
        zones.append({**registered, "source_detail": sdz})

    return zones


# =====================================================
# 3. GATE: harga masuk zona + candle timing_tf close di zona
# =====================================================
def check_gate(zone: dict, current_price: float,
               df_timing: "pd.DataFrame", config: dict) -> dict:
    """
    Gate v2.1 Bab 1.3: gate TERPICU jika DUA syarat mekanis terpenuhi:
      (a) harga saat ini berada di dalam [zone_bottom, zone_top]
      (b) candle timing_tf (M5) YANG SUDAH CLOSE juga close di dalam/
          dekat zona (bukan sekadar high/low menyentuh sekilas)

    Ini FAKTA BINER (organ boleh deteksi) -- BUKAN menilai bentuk
    wick/rejection (itu penilaian, hanya boleh Tier 2, v2.1 Bab 1.2).
    """
    price_in_zone = zone["zone_bottom"] <= current_price <= zone["zone_top"]
    if not price_in_zone:
        return {"gate_open": False, "reason": "price_not_in_zone"}

    last_closed_close = float(df_timing["close"].iloc[-2])
    m5_close_in_zone = zone["zone_bottom"] <= last_closed_close <= zone["zone_top"]

    if not m5_close_in_zone:
        return {"gate_open": False, "reason": "price_in_zone_but_m5_not_closed_in_zone"}

    return {"gate_open": True, "reason": "price_in_zone_and_m5_closed_in_zone"}


# =====================================================
# ORKESTRATOR UTAMA: satu pair, satu siklus screener
# =====================================================
def run_screener_for_pair(pair: str, config: dict) -> dict:
    """
    Jalankan siklus screener PENUH untuk satu pair (2 timeframe: H1+M5).
    TIDAK mengeksekusi order, TIDAK memutuskan entry -- hanya
    menghasilkan daftar kandidat yang GATE-nya terbuka & lolos
    debounce/cooldown, siap diteruskan ke pembentukan paket Tier 2.

    Return: dict {
        "pair": str, "stage": str, "bias": dict,
        "structure_phase": dict, "candidates": list[dict],
        "debug": dict,
    }
    """
    debug = {}
    symbol = mt5c.resolve_symbol(pair, config)
    tf_cfg = config.get("screener_timeframes", {})
    structure_tf = tf_cfg.get("structure_tf", "H1")
    structure_count = tf_cfg.get("structure_lookback", 200)
    timing_tf = tf_cfg.get("timing_tf", "M5")
    timing_count = tf_cfg.get("timing_lookback", 20)

    base = {"pair": pair, "stage": None, "bias": None,
            "structure_phase": None, "candidates": [], "debug": debug}

    # --- PRE-FILTER: HANYA spread (v2.0 Bab 4.2, v2.1 Bab 0.1) ---
    spread = check_spread(symbol, config)
    debug["spread"] = spread
    if not spread["allowed"]:
        return {**base, "stage": "spread_filter_blocked"}

    # --- 1. BIAS + FASE dari structure_tf (H1) via bos_choch ---
    df_structure = mt5c.get_candles(symbol, structure_tf, count=structure_count)
    bp = determine_bias_and_phase(df_structure, config)
    debug["bias_phase"] = {k: v for k, v in bp.items() if k != "events"}
    debug["bos_choch_events"] = bp["events"][-5:]  # ringkas 5 terakhir saja

    base["bias"] = {"bias": bp["bias"], "reason": bp["reason"],
                     "is_transitional": bp["is_transitional"]}
    base["structure_phase"] = {"phase": bp["phase"], "reason": bp["reason"]}

    # EMA/ADX sebagai DATA PENDUKUNG (bukan penentu bias, v2.0 Bab 7) --
    # disimpan di debug supaya siap dipakai paket Tier 2 (Fase 3-4)
    snap = get_confirmed_snapshot(df_structure, config)
    debug["supporting_data"] = {
        "ema20": snap["ema20"], "ema50": snap["ema50"],
        "adx": snap["adx"], "plus_di": snap["plus_di"],
        "minus_di": snap["minus_di"], "atr": snap["atr_current"],
    }

    # Reset cooldown semua zona di pair ini jika fase transisi baru
    # muncul (struktur berubah = layak dinilai ulang, v2.0 Bab 5.4)
    if bp["phase"] == "potensi_choch":
        zs.reset_cooldown_on_new_structure(pair)

    # --- 2. KUMPULKAN ZONA (satu arah jika tegas, dua arah jika transisi) ---
    if bp["is_transitional"]:
        expected_directions = ["bullish", "bearish"]  # Opsi B
    else:
        expected_directions = [bp["bias"]]

    zones = collect_zones(symbol, pair, df_structure, config, expected_directions)
    debug["total_zones_tracked"] = len(zones)
    debug["expected_directions"] = expected_directions

    current_price = float(df_structure["close"].iloc[-2])
    atr_val = snap["atr_current"]

    # --- 3-5. GATE + DEBOUNCE + ANTI-DOUBLE-POSITION per zona ---
    df_timing = mt5c.get_candles(symbol, timing_tf, count=timing_count)
    candidates = []
    zone_debug = []

    for zone in zones:
        entry = {"zone_id": zone["zone_id"], "zone_type": zone["zone_type"],
                 "direction": zone["direction"], "touch_count": zone["touch_count"]}

        if zone["has_open_position"]:
            entry["skipped_reason"] = "has_open_position"
            zone_debug.append(entry)
            continue

        if zs.is_blocked_by_cooldown(zone):
            entry["skipped_reason"] = "cooldown_or_throttle_active"
            zone_debug.append(entry)
            continue

        gate = check_gate(zone, current_price, df_timing, config)
        entry["gate"] = gate

        if not gate["gate_open"]:
            if zone["already_sent"] and zs.is_price_beyond_exit_threshold(
                zone, current_price, atr_val
            ):
                zs.reset_sent_flag(zone["zone_id"])
                entry["sent_flag_reset"] = True
            zone_debug.append(entry)
            continue

        if zone["already_sent"]:
            entry["skipped_reason"] = "already_sent_debounce_active"
            zone_debug.append(entry)
            continue

        # LOLOS semua gerbang mekanis -> kandidat siap dikirim ke Tier 2
        zs.mark_sent(zone["zone_id"])
        candidate = {
            "zone_id": zone["zone_id"],
            "zone_type": zone["zone_type"],
            "direction": zone["direction"],
            "zone_top": zone["zone_top"],
            "zone_bottom": zone["zone_bottom"],
            "touch_count": zone["touch_count"],
            "is_revisit": not zone["is_new"] and zone["touch_count"] > 0,
            "last_verdict": zone.get("last_verdict"),
            "last_confidence": zone.get("last_confidence"),
            "last_reasoning": zone.get("last_reasoning"),
            "bias": bp["bias"],
            "is_transitional": bp["is_transitional"],
            "structure_phase": bp["phase"],
            "current_price": current_price,
            "supporting_data": debug["supporting_data"],
            "source_detail": zone["source_detail"],
        }
        candidates.append(candidate)
        entry["result"] = "candidate_ready_for_tier2"
        zone_debug.append(entry)

    debug["zone_detail"] = zone_debug
    base["candidates"] = candidates
    base["stage"] = "candidates_ready" if candidates else "no_candidates_this_cycle"
    return base


def run_screener_cycle(config: dict) -> list:
    """Jalankan screener untuk SEMUA pair di config. Return list hasil per-pair."""
    pairs = get_all_pairs(config)
    results = []
    for pair in pairs:
        try:
            result = run_screener_for_pair(pair, config)
        except Exception as e:
            # Kegagalan satu pair TIDAK BOLEH menjatuhkan pair lain
            # (v2.3 Bab 1.3 -- loop per-pair terisolasi)
            result = {"pair": pair, "stage": "error", "error": str(e),
                      "bias": None, "structure_phase": None, "candidates": []}
        results.append(result)
    return results


if __name__ == "__main__":
    config = load_config()

    print("=" * 60)
    print("TEST: screener.py (Fase 1 REVISI - bias H1 via bos_choch)")
    print("=" * 60)

    if not mt5c.connect():
        sys.exit(1)

    init_ob_db()
    zs.init_db()

    results = run_screener_cycle(config)

    for r in results:
        print(f"\n{'='*60}")
        print(f"PAIR: {r['pair']}")
        print("=" * 60)
        print(f"  stage: {r['stage']}")
        if r.get("bias"):
            trans = " [TRANSISI - dua arah]" if r["bias"].get("is_transitional") else ""
            print(f"  bias : {r['bias'].get('bias')}{trans}")
            print(f"         alasan: {r['bias'].get('reason')}")
        if r.get("structure_phase"):
            print(f"  fase : {r['structure_phase'].get('phase')}")

        dbg = r.get("debug", {})
        total_tracked = dbg.get("total_zones_tracked")
        expected_dirs = dbg.get("expected_directions")
        if total_tracked is not None:
            print(f"  total zona terlacak (arah={expected_dirs}): {total_tracked}")

        sup = dbg.get("supporting_data")
        if sup:
            print(f"  data pendukung: EMA20={sup['ema20']:.5f} EMA50={sup['ema50']:.5f} "
                  f"ADX={sup['adx']}")

        zone_detail = dbg.get("zone_detail", [])
        if zone_detail:
            print("  rincian tiap zona:")
            for zd in zone_detail:
                gate_info = zd.get("gate", {})
                reason = zd.get("skipped_reason") or gate_info.get("reason") or zd.get("result")
                print(f"    - {zd['zone_type']:5s} {zd['direction']:8s} "
                      f"touch={zd['touch_count']:<3} -> {reason}")
        elif total_tracked == 0:
            print("  (tidak ada zona yang terlacak sama sekali)")

        print(f"  kandidat siap Tier 2: {len(r['candidates'])}")
        for c in r["candidates"]:
            revisit = " [KUNJUNGAN ULANG]" if c["is_revisit"] else ""
            trans = " [dari fase transisi]" if c["is_transitional"] else ""
            print(f"    - {c['zone_type']:5s} {c['direction']:8s} "
                  f"[{c['zone_bottom']:.5f}-{c['zone_top']:.5f}] "
                  f"touch={c['touch_count']}{revisit}{trans}")

    mt5c.shutdown()
    print("\n[SELESAI] screener.py Fase 1 (revisi bias H1) berhasil dijalankan.")
