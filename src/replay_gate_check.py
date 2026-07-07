"""
replay_gate_check.py - VERIFIKASI HISTORIS jalur penuh screener

Tujuan: membuktikan apakah rangkaian bias -> zona -> gate benar-benar
bisa menghasilkan gate_open=True (kandidat), tanpa menunggu kondisi
pasar live. Melakukan "replay" mundur melalui candle H1 historis,
menjalankan FUNGSI ASLI dari screener.py (bukan re-implementasi) di
tiap titik waktu, lalu mengecek M5 candle-by-candle apakah gate
terbuka.

PENTING - ISOLASI DATABASE:
Script ini SENGAJA menulis ke database SEMENTARA (data/replay_temp.db),
BUKAN data/trading_bot.db yang dipakai live. Ini murni untuk simulasi
--tidak akan mencampur OB/zone historis replay dengan data produksi.
Aman dijalankan kapan saja tanpa mengganggu screener.py biasa.

Jalankan: python src/replay_gate_check.py
Lalu KIRIM seluruh output ke chat.
"""

import sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config_loader import load_config, get_all_pairs, ROOT_DIR
import mt5_connector as mt5c

# Import modul yang DB_PATH-nya akan di-monkeypatch ke temp file
import orderblock
import zone_state as zs
import screener as scr  # pakai fungsi ASLI: determine_bias_and_phase,
                         # collect_zones, check_gate

REPLAY_DAYS = 10          # mundur berapa hari (H1 candle historis)
MAX_HITS_TO_SHOW = 60     # dinaikkan dari 15 -- supaya tipe zona yang
                           # jarang (mis. SD) tidak "kalah cepat" oleh
                           # tipe yang sering (mis. SWING_HIGH) hanya
                           # karena urutan kemunculan kronologis


def setup_temp_db():
    """Alihkan DB_PATH orderblock.py & zone_state.py ke file sementara,
    supaya replay TIDAK menyentuh data/trading_bot.db produksi."""
    temp_path = ROOT_DIR / "data" / "replay_temp.db"
    for ext in ("", "-wal", "-shm"):
        p = Path(str(temp_path) + ext)
        if p.exists():
            p.unlink()

    orderblock.DB_PATH = temp_path
    zs.DB_PATH = temp_path

    orderblock.init_db()
    zs.init_db()
    print(f"[OK] DB replay sementara: {temp_path} (terpisah dari trading_bot.db)")


def replay_pair(pair: str, config: dict) -> list:
    """
    Replay satu pair. Return list of hits: dict(pair, h1_time, m5_time,
    zone_type, direction, bias, phase, price).
    """
    symbol = mt5c.resolve_symbol(pair, config)
    tf_cfg = config.get("screener_timeframes", {})
    structure_lookback = tf_cfg.get("structure_lookback", 200)

    # Ambil H1 cukup panjang: buffer utk window bias + hari yang direplay
    h1_count = structure_lookback + (REPLAY_DAYS * 24) + 5
    df_h1_full = mt5c.get_candles(symbol, "H1", count=h1_count)

    # Ambil M5 mencakup periode yang sama (+ buffer)
    m5_count = (REPLAY_DAYS * 24 * 12) + 50
    df_m5_full = mt5c.get_candles(symbol, "M5", count=m5_count)

    hits = []
    start_idx = len(df_h1_full) - (REPLAY_DAYS * 24) - 2
    start_idx = max(start_idx, structure_lookback)

    for h1_idx in range(start_idx, len(df_h1_full) - 1):
        # Window H1 "seolah waktu sekarang" = candle h1_idx baru close,
        # candle h1_idx+1 berperan sbg "current forming candle" (anti-repaint)
        window_start = max(0, h1_idx - structure_lookback)
        window_h1 = df_h1_full.iloc[window_start:h1_idx + 2].reset_index(drop=True)

        if len(window_h1) < 30:
            continue  # terlalu sedikit data utk analisis bermakna

        try:
            bp = scr.determine_bias_and_phase(window_h1, config)
        except Exception:
            continue

        if bp["is_transitional"]:
            expected_directions = ["bullish", "bearish"]
        else:
            expected_directions = [bp["bias"]]

        try:
            zones = scr.collect_zones(symbol, pair, window_h1, config, expected_directions)
        except Exception:
            continue

        if not zones:
            continue

        h1_close_time = df_h1_full["time"].iloc[h1_idx]
        next_h1_close_time = df_h1_full["time"].iloc[h1_idx + 1]

        # M5 candle-candle yang jatuh dalam periode H1 berjalan ini
        m5_period = df_m5_full[
            (df_m5_full["time"] > h1_close_time) &
            (df_m5_full["time"] <= next_h1_close_time)
        ]

        for m5_idx in range(len(m5_period)):
            # Window M5 dgn 1 baris ekstra dummy di akhir utk anti-repaint
            # slicing check_gate (yang pakai iloc[-2] sbg candle closed terakhir)
            base_slice = df_m5_full[df_m5_full["time"] <= m5_period["time"].iloc[m5_idx]]
            if len(base_slice) < 2:
                continue
            window_m5 = pd.concat(
                [base_slice, base_slice.iloc[[-1]]], ignore_index=True
            )
            current_price = float(window_m5["close"].iloc[-2])

            for zone in zones:
                gate = scr.check_gate(zone, current_price, window_m5, config)
                if gate["gate_open"] and not zone["already_sent"]:
                    hits.append({
                        "pair": pair,
                        "h1_time": str(h1_close_time),
                        "m5_time": str(m5_period["time"].iloc[m5_idx]),
                        "zone_type": zone["zone_type"],
                        "direction": zone["direction"],
                        "bias": bp["bias"],
                        "phase": bp["phase"],
                        "price": current_price,
                        "zone_range": f"{zone['zone_bottom']:.5f}-{zone['zone_top']:.5f}",
                    })
                    zs.mark_sent(zone["zone_id"])
                    # PENTING: refresh flag di objek in-memory JUGA, bukan
                    # cuma di DB -- kalau tidak, iterasi M5 berikutnya
                    # dalam H1 step yang sama akan pakai `zone` dict basi
                    # (already_sent masih 0) dan debounce gagal tersimulasi
                    # dengan benar. Bug ini TIDAK ada di screener.py produksi
                    # karena run_screener_for_pair() selalu baca ulang state
                    # dari DB tiap siklus -- ini murni artefak cara replay
                    # memegang objek `zones` lintas beberapa candle M5.
                    zone["already_sent"] = 1
                    if len(hits) >= MAX_HITS_TO_SHOW:
                        return hits

    return hits


if __name__ == "__main__":
    config = load_config()
    print("=" * 60)
    print(f"REPLAY VERIFIKASI GATE ({REPLAY_DAYS} hari historis, H1+M5)")
    print("=" * 60)

    if not mt5c.connect():
        sys.exit(1)

    setup_temp_db()

    pairs = get_all_pairs(config)
    all_hits = []

    for pair in pairs:
        print(f"\nMe-replay {pair} ...")
        try:
            hits = replay_pair(pair, config)
            all_hits.extend(hits)
            print(f"  -> {len(hits)} momen gate_open ditemukan")
        except Exception as e:
            print(f"  [ERROR] {pair}: {e}")
            import traceback
            traceback.print_exc()

    mt5c.shutdown()

    print(f"\n{'='*60}")
    print(f"RINGKASAN: total {len(all_hits)} momen gate_open di seluruh pair")
    unique_zones = set((h["pair"], h["zone_type"], h["direction"], h["zone_range"])
                        for h in all_hits)
    print(f"          dari {len(unique_zones)} zona UNIK (setelah fix debounce)")
    print("=" * 60)

    # Breakdown per TIPE ZONA -- supaya jelas tipe mana yang benar2
    # jarang ter-gate vs yang cuma "kalah cepat" karena urutan waktu
    from collections import Counter
    type_counts = Counter(h["zone_type"] for h in all_hits)
    print("\nBreakdown per tipe zona (seluruh dataset, bukan dibatasi tampilan):")
    for zt, cnt in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {zt:12s}: {cnt}")
    all_types = {"OB", "SBR", "RBS", "FVG", "EQH", "EQL", "SWING_HIGH", "SWING_LOW", "SD"}
    missing_types = all_types - set(type_counts.keys())
    if missing_types:
        print(f"\n  Tipe yang TIDAK PERNAH ter-gate dalam {REPLAY_DAYS} hari ini: "
              f"{sorted(missing_types)}")
        print("  (Wajar untuk tipe yang secara alami jarang terbentuk, "
              "mis. EQH/EQL/SD -- bukan otomatis bug.)")

    if all_hits:
        print("\nDetail (maks ditampilkan):")
        for h in all_hits[:MAX_HITS_TO_SHOW]:
            print(f"  [{h['pair']}] H1={h['h1_time']} M5={h['m5_time']} "
                  f"{h['zone_type']:5s} {h['direction']:8s} "
                  f"bias={h['bias']}/{h['phase']} "
                  f"price={h['price']:.5f} zona=[{h['zone_range']}]")
        print("\n[KESIMPULAN] Jalur bias->zona->gate TERBUKTI bisa "
              "menghasilkan kandidat di data historis nyata.")
    else:
        print("\n[PERHATIAN] TIDAK ADA momen gate_open ditemukan dalam "
              f"{REPLAY_DAYS} hari terakhir untuk semua pair.")
        print("Ini perlu diselidiki lebih lanjut -- kirim output ini "
              "untuk dianalisis apakah ambang gate terlalu ketat atau "
              "ada bug lain.")

    print(f"\n(DB sementara ada di data/replay_temp.db -- boleh dihapus "
          f"kapan saja, tidak memengaruhi trading_bot.db produksi)")
    print("\n[SELESAI] Kirim SELURUH output ini ke chat untuk analisis.")
