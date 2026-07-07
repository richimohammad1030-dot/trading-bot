"""
package_builder.py - Fase 3: Rakit Paket Data Tier 1 -> Tier 2

Menggabungkan SEMUA yang sudah dibangun (screener.py, chart_generator.py,
zone_state.py) menjadi SATU paket lengkap per kandidat, siap dikirim ke
Claude (Tier 2, dibangun di Fase 4).

Komponen paket sesuai v2.0 Bab 7 (REVISI: H4 dibuang, bias dari H1):
    - Bias (H1, via bos_choch) + fase struktur
    - Zona aktif: tipe, batas, level, touch_count
    - Chart: H1 + M5 (PNG, dari chart_generator.py)
    - Volume per zona vs rata-rata
    - Likuiditas: jarak ke EQH/EQL terdekat
    - FVG: status aktif di sekitar zona kandidat
    - Riwayat zona: penilaian sebelumnya (dari zone_state.py, Opsi A)
    - Konteks: session, EMA/ADX (data pendukung, BUKAN penentu)

PRINSIP (v2.1 Bab 0.1): modul ini HANYA menyajikan data mekanis.
TIDAK ADA logika di sini yang menilai kualitas kandidat -- itu 100%
tugas Tier 2. Fungsi di sini murni mengumpulkan & merapikan fakta.
"""

import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chart_generator import generate_zone_chart
from package_log import save_package


# =====================================================
# SESSION (konteks waktu, murni informasi -- bukan filter)
# =====================================================
def get_session(dt_utc: datetime) -> str:
    """
    Tentukan sesi trading dari jam UTC. Murni label informasi untuk
    Tier 2 -- BUKAN filter Tier 1 (v2.1 Bab 0.1: jangan menghakimi).

    Batas umum (UTC): Sydney 21-06, Tokyo 00-09, London 07-16,
    New York 12-21. Overlap London-NY (12-16) = paling likuid.
    """
    h = dt_utc.hour
    in_london = 7 <= h < 16
    in_ny = 12 <= h < 21
    in_tokyo = 0 <= h < 9

    if in_london and in_ny:
        return "london_ny_overlap"
    if in_london:
        return "london"
    if in_ny:
        return "new_york"
    if in_tokyo:
        return "tokyo"
    return "sydney_quiet"


# =====================================================
# VOLUME KONTEKS (data, bukan filter)
# =====================================================
def compute_volume_context(df_structure, lookback: int = 20) -> dict:
    """
    Volume candle H1 terakhir (yang sudah close, index -2) relatif
    terhadap rata-rata `lookback` candle sebelumnya. Murni data --
    Tier 2 yang menilai apakah ini "volume tinggi = displacement
    institusional" atau tidak.
    """
    vol_col = "tick_volume" if "tick_volume" in df_structure.columns else "volume"
    if vol_col not in df_structure.columns:
        return {"current_volume": None, "avg_volume": None, "ratio": None}

    current_vol = float(df_structure[vol_col].iloc[-2])
    avg_vol = float(df_structure[vol_col].iloc[-(lookback + 2):-2].mean())
    ratio = round(current_vol / avg_vol, 2) if avg_vol > 0 else None

    return {
        "current_volume": current_vol,
        "avg_volume_lookback": round(avg_vol, 2),
        "ratio_vs_avg": ratio,
    }


# =====================================================
# LIKUIDITAS TERDEKAT (jarak ke EQH/EQL, data mentah)
# =====================================================
def compute_liquidity_distance(current_price: float, all_zones: list,
                                atr_val: float) -> dict:
    """
    Jarak ke EQH/EQL terdekat (dari SEMUA zona yang terlacak siklus
    ini, bukan cuma searah bias) -- konteks likuiditas untuk Tier 2
    menilai "draw on liquidity" (v2.0 Bab 9.2 semangat riset ICT).
    """
    eqh_zones = [z for z in all_zones if z.get("zone_type") == "EQH"]
    eql_zones = [z for z in all_zones if z.get("zone_type") == "EQL"]

    def _nearest(zones):
        if not zones:
            return None
        nearest = min(zones, key=lambda z: abs(
            current_price - (z["zone_top"] + z["zone_bottom"]) / 2
        ))
        mid = (nearest["zone_top"] + nearest["zone_bottom"]) / 2
        dist = abs(current_price - mid)
        return {
            "level": round(mid, 5),
            "distance_price": round(dist, 5),
            "distance_atr": round(dist / atr_val, 2) if atr_val else None,
        }

    return {
        "nearest_eqh": _nearest(eqh_zones),
        "nearest_eql": _nearest(eql_zones),
    }


# =====================================================
# STATUS FVG DI SEKITAR ZONA KANDIDAT
# =====================================================
def compute_fvg_context(candidate_zone: dict, all_zones: list) -> list:
    """
    Daftar FVG aktif lain (selain kandidat itu sendiri jika kandidat
    ini sendiri FVG) yang masih ada di sekitar siklus ini -- membantu
    Tier 2 menilai apakah ada imbalance lain yang relevan.
    """
    fvgs = [z for z in all_zones
            if z.get("zone_type") == "FVG" and z is not candidate_zone]
    return [{
        "direction": z["direction"],
        "zone_top": z["zone_top"],
        "zone_bottom": z["zone_bottom"],
        "touch_count": z.get("touch_count", 0),
    } for z in fvgs]


# =====================================================
# RIWAYAT ZONA (Opsi A -- v2.1 Bab 5.2, sudah mengalir dari zone_state)
# =====================================================
def compute_zone_history(candidate: dict) -> dict:
    """
    Riwayat penilaian zona ini SEBELUMNYA (jika kunjungan ulang).
    Data ini SUDAH tersedia di `candidate` (dialirkan dari
    zone_state.py via screener.py) -- fungsi ini hanya merapikan
    jadi bentuk siap-kirim.
    """
    return {
        "is_revisit": candidate.get("is_revisit", False),
        "previous_verdict": candidate.get("last_verdict"),
        "previous_confidence": candidate.get("last_confidence"),
        "previous_reasoning": candidate.get("last_reasoning"),
        "touch_count": candidate.get("touch_count", 0),
    }


# =====================================================
# ORKESTRATOR: rakit SATU paket lengkap untuk SATU kandidat
# =====================================================
# =====================================================
# DEFINISI ZONA (tekstual, untuk Tier 2 -- v2.0 Bab 3)
# =====================================================
# Chart HANYA menunjukkan REAKSI harga saat ini di zona -- ia TIDAK
# selalu menunjukkan candle asal yang membentuk zona (bisa sudah lama
# berlalu, di luar jendela candle yang ditampilkan). Untuk mencegah
# Tier 2 menebak-nebak "kenapa ini disebut OB", definisi SMC tiap
# tipe zona disertakan sebagai TEKS eksplisit di paket -- bukan
# gambar. Tier 1 sudah memvalidasi keabsahannya secara mekanis
# (smc library + filter lebar); Tier 2 TIDAK perlu membuktikan ulang
# asal-usul zona dari piksel, cukup menilai REAKSI harga terhadapnya.
ZONE_DEFINITIONS = {
    "OB": ("Order Block: candle terakhir berlawanan arah SEBELUM "
           "displacement tajam (breakout swing high/low). Area ini "
           "diyakini menyimpan order institusional yang belum terisi "
           "penuh; harga sering kembali (retest) sebelum melanjutkan "
           "arah displacement."),
    "SBR": ("Support-Becomes-Resistance: level swing LOW yang sudah "
            "ditembus turun (BOS bearish), kini berperan sebagai "
            "resistance. Setup SELL saat harga retrace naik ke sini."),
    "RBS": ("Resistance-Becomes-Support: level swing HIGH yang sudah "
            "ditembus naik (BOS bullish), kini berperan sebagai "
            "support. Setup BUY saat harga retrace turun ke sini."),
    "FVG": ("Fair Value Gap: celah harga (imbalance) antara 3 candle "
            "berurutan -- candle tengah bergerak begitu cepat "
            "sehingga meninggalkan gap yang belum diisi. Harga "
            "cenderung kembali mengisi gap ini sebelum melanjutkan."),
    "EQH": ("Equal Highs: kelompok swing high yang berdekatan -- "
            "liquidity pool (buy-side) tempat stop-loss/pending "
            "order terkumpul. Rawan liquidity grab (sweep lalu "
            "reversal) sebelum benar-benar breakout."),
    "EQL": ("Equal Lows: kelompok swing low yang berdekatan -- "
            "liquidity pool (sell-side), cermin dari EQH."),
    "SWING_HIGH": ("Swing high yang belum pernah ditembus BOS -- "
                   "resistance murni di depan harga."),
    "SWING_LOW": ("Swing low yang belum pernah ditembus BOS -- "
                  "support kritis di depan harga; jika tertembus, "
                  "berarti CHoCH."),
    "SD": ("Supply/Demand: area konsolidasi sempit (basing) yang "
           "diikuti candle displacement tajam menembus keluar. Zona "
           "= rentang basing itu sendiri, tempat institusi diduga "
           "mengakumulasi sebelum mendorong harga searah displacement."),
}


def build_tier2_package(candidate: dict, pair: str, symbol: str,
                         df_structure, df_timing, all_zones_this_cycle: list,
                         config: dict, persist: bool = True) -> dict:
    """
    Rakit paket LENGKAP untuk satu kandidat, siap dikirim ke Tier 2
    (Fase 4 akan membangun pemanggilan Claude aktual dari paket ini).

    Args:
        candidate: dict kandidat dari run_screener_for_pair()["candidates"]
        pair, symbol: identitas instrumen
        df_structure, df_timing: candle H1 & M5 (dari siklus screener
            yang sama -- TIDAK diambil ulang, demi konsistensi data
            dan hemat panggilan MT5)
        all_zones_this_cycle: SEMUA zona yang terlacak siklus ini
            (untuk konteks chart + likuiditas + FVG)
        config: config dict
        persist: True (default) -> simpan paket ke DB (package_log.py,
            Opsi B) SEBELUM dikirim Claude, dan sisipkan "log_id" ke
            paket yang dikembalikan -- WAJIB dipakai Fase 4 untuk
            update_verdict() setelah Claude membalas. Set False hanya
            untuk uji visual chart tanpa mencatat riwayat (mis. test
            manual berulang yang tidak perlu membanjiri DB).

    Return: dict paket lengkap (lihat struktur di bawah), berisi
        "log_id" (int atau None jika persist=False).
    """
    current_price = candidate["current_price"]
    supporting = candidate.get("supporting_data", {})
    atr_val = supporting.get("atr")

    # Zona konteks utk chart = zona lain (bukan kandidat ini sendiri),
    # dibatasi 5 terdekat supaya chart tidak penuh sesak
    other_zones = [z for z in all_zones_this_cycle
                   if z.get("zone_id") != candidate.get("zone_id")]
    other_zones_sorted = sorted(
        other_zones,
        key=lambda z: abs(current_price - (z["zone_top"] + z["zone_bottom"]) / 2)
    )[:5]

    candidate_zone_for_chart = {
        "zone_type": candidate["zone_type"],
        "direction": candidate["direction"],
        "zone_top": candidate["zone_top"],
        "zone_bottom": candidate["zone_bottom"],
    }

    charts = generate_zone_chart(
        df_structure, df_timing, pair, candidate_zone_for_chart,
        context_zones=other_zones_sorted,
    )

    now_utc = datetime.now(timezone.utc)

    package = {
        "pair": pair,
        "symbol": symbol,
        "timestamp_utc": now_utc.isoformat(),
        "session": get_session(now_utc),

        "bias": {
            "value": candidate["bias"],
            "is_transitional": candidate["is_transitional"],
            "structure_phase": candidate["structure_phase"],
        },

        "candidate_zone": {
            "zone_id": candidate["zone_id"],
            "zone_type": candidate["zone_type"],
            "zone_definition": ZONE_DEFINITIONS.get(
                candidate["zone_type"],
                "Tipe zona tidak dikenali -- perlakukan sebagai area "
                "penting generik, nilai dari reaksi harga saja."
            ),
            "direction": candidate["direction"],
            "zone_top": candidate["zone_top"],
            "zone_bottom": candidate["zone_bottom"],
            "current_price": current_price,
        },

        "zone_history": compute_zone_history(candidate),

        "volume_context": compute_volume_context(df_structure),

        "liquidity_context": compute_liquidity_distance(
            current_price, all_zones_this_cycle, atr_val
        ),

        "fvg_context": compute_fvg_context(candidate, all_zones_this_cycle),

        "supporting_data": supporting,  # EMA20/50, ADX, +DI/-DI, ATR

        "charts": charts,  # {h1_chart_path, m5_chart_path}

        "other_zones_nearby": [{
            "zone_type": z["zone_type"], "direction": z["direction"],
            "zone_top": z["zone_top"], "zone_bottom": z["zone_bottom"],
            "touch_count": z.get("touch_count", 0),
        } for z in other_zones_sorted],
    }

    # Opsi B: simpan paket ke DB SEBELUM dikirim Claude (v0.2 lanjutan
    # Fase 3). log_id disisipkan ke paket -- Fase 4 WAJIB memakainya
    # untuk update_verdict() setelah Claude membalas.
    package["log_id"] = save_package(package) if persist else None

    return package


if __name__ == "__main__":
    import mt5_connector as mt5c
    from config_loader import load_config
    from screener import run_screener_for_pair
    import zone_state as zs
    from orderblock import init_db as init_ob_db
    import json

    config = load_config()

    print("=" * 60)
    print("TEST: package_builder.py (Fase 3)")
    print("=" * 60)

    if not mt5c.connect():
        sys.exit(1)

    init_ob_db()
    zs.init_db()

    pair = "EURUSD"
    symbol = mt5c.resolve_symbol(pair, config)
    result = run_screener_for_pair(pair, config)

    tf_cfg = config.get("screener_timeframes", {})
    df_structure = mt5c.get_candles(symbol, tf_cfg.get("structure_tf", "H1"),
                                     count=tf_cfg.get("structure_lookback", 200))
    df_timing = mt5c.get_candles(symbol, tf_cfg.get("timing_tf", "M5"),
                                  count=tf_cfg.get("timing_lookback", 20) + 40)

    # Kumpulkan SEMUA zona siklus ini (termasuk yang tidak lolos gate)
    # dari debug untuk konteks chart -- fallback ke kandidat saja jika
    # tidak ada info zona lengkap di debug
    all_zones = result["candidates"].copy()

    if result["candidates"]:
        candidate = result["candidates"][0]
        print(f"Pakai kandidat NYATA: {candidate['zone_type']} "
              f"{candidate['direction']}")
    else:
        print("[INFO] Tidak ada kandidat live -- pakai dummy utk test.")
        current_price = float(df_structure["close"].iloc[-2])
        candidate = {
            "zone_id": "dummy_test", "zone_type": "OB", "direction": "bullish",
            "zone_top": round(current_price - 0.0005, 5),
            "zone_bottom": round(current_price - 0.0025, 5),
            "touch_count": 0, "is_revisit": False, "last_verdict": None,
            "last_confidence": None, "last_reasoning": None,
            "bias": "bullish", "is_transitional": False,
            "structure_phase": "kontinuitas", "current_price": current_price,
            "supporting_data": {"ema20": current_price, "ema50": current_price,
                                 "adx": 20.0, "plus_di": 20.0, "minus_di": 15.0,
                                 "atr": 0.001},
            "source_detail": {},
        }
        all_zones = [candidate]

    package = build_tier2_package(candidate, pair, symbol, df_structure,
                                   df_timing, all_zones, config)

    print("\n--- PAKET TIER 2 (ringkasan) ---")
    printable = {k: v for k, v in package.items()}
    print(json.dumps(printable, indent=2, default=str))

    mt5c.shutdown()
    print("\n[SELESAI] package_builder.py Fase 3 berhasil.")
