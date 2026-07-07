"""
test_single_candidate.py - Verifikasi TERKENDALI jalur sampai ke Claude

BEDA dari tier2_orchestrator.py biasa: script ini TIDAK menunggu
gate terbuka secara alami. Ia MENGAMBIL satu zona AKTIF (dari OB
yang sudah terdeteksi live, atau dummy kalau tidak ada), lalu
MEMAKSA-nya lewat evaluate_candidate() -- jadi kita bisa pastikan
seluruh pipa (paket -> gambar -> Claude -> parsing -> simpan)
BENAR-BENAR bekerja, tanpa menunggu kondisi pasar alami.

INI AKAN MEMANGGIL CLAUDE API SUNGGUHAN (1 kali) -- ada biaya.
Jalankan HANYA kalau Anda siap menguji sekali dengan sengaja.

REVISI (fix disconnect risk_engine, sesi audit arsitektur):
evaluate_candidate() di tier2_orchestrator.py SEKARANG memanggil
check_circuit_breaker() di awal (Titik 1b) dan _apply_execution_gate()
setelah verdict 'execute' (Titik 2) -- keduanya baca tabel risk_engine
(risk_counters, level_blacklist, trade_log). Tanpa init_risk_db(),
tabel itu belum ada -> crash "no such table: risk_counters".
Fix: init_risk_db() + cleanup_blacklist() ditambahkan sebelum
evaluate_candidate() dipanggil, sejajar dengan init_ob_db()/zs.init_db().

Konsekuensi lain dari wiring risk: outcome yang dikembalikan
evaluate_candidate() sekarang bisa berupa:
  "execute"                 -> lolos Claude DAN lolos gate risiko
  "execute_blocked_by_risk" -> Claude bilang execute, TAPI diblokir
                                risk_engine (blacklist/exposure/dst)
  "skip"                    -> Claude bilang skip
  "circuit_breaker"         -> daily/weekly/monthly limit aktif,
                                TIDAK sempat memanggil Claude
  "api_failed"              -> timeout/error API Claude
Blok cetak hasil di bawah direvisi untuk menampilkan detail `risk`
gate (kalau ada), supaya kasus execute_blocked_by_risk tidak terlihat
seperti sekadar "execute" biasa saat dibaca di terminal.

Jalankan: python src/test_single_candidate.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config_loader import load_config
import mt5_connector as mt5c
from indicators import get_confirmed_snapshot
from orderblock import init_db as init_ob_db, get_active_obs
import zone_state as zs
import package_log as pkglog
from risk_engine import init_risk_db, cleanup_blacklist   # BARU
from tier2_orchestrator import evaluate_candidate

PAIR = "EURUSD"  # ganti kalau mau uji pair lain

if __name__ == "__main__":
    config = load_config()

    print("=" * 60)
    print(f"TEST TERKENDALI: paksa 1 kandidat {PAIR} lewat Tier 2")
    print("=" * 60)
    print("[PERINGATAN] Ini akan memanggil Claude API SUNGGUHAN "
          "(1x, 2 gambar). Ada biaya kecil.")
    confirm = input("Lanjutkan? (ketik 'ya' untuk lanjut): ")
    if confirm.strip().lower() != "ya":
        print("Dibatalkan.")
        sys.exit(0)

    if not mt5c.connect():
        sys.exit(1)

    init_ob_db()
    zs.init_db()
    pkglog.init_db()
    init_risk_db()        # BARU -- wajib sebelum evaluate_candidate()
    cleanup_blacklist()   # BARU -- buang blacklist kedaluwarsa (higiene)

    symbol = mt5c.resolve_symbol(PAIR, config)
    tf_cfg = config.get("screener_timeframes", {})
    df_structure = mt5c.get_candles(symbol, tf_cfg.get("structure_tf", "H1"),
                                     count=tf_cfg.get("structure_lookback", 200))
    df_timing = mt5c.get_candles(symbol, tf_cfg.get("timing_tf", "M5"),
                                  count=tf_cfg.get("timing_lookback", 20) + 40)

    active_obs = get_active_obs(PAIR)
    if active_obs:
        ob = active_obs[0]
        current_price = float(df_structure["close"].iloc[-2])
        zone = zs.find_or_create_zone(PAIR, "OB", ob["direction"],
                                       ob["ob_top"], ob["ob_bottom"])
        candidate = {
            "zone_id": zone["zone_id"], "zone_type": "OB",
            "direction": ob["direction"],
            "zone_top": ob["ob_top"], "zone_bottom": ob["ob_bottom"],
            "touch_count": zone["touch_count"],
            "is_revisit": not zone["is_new"] and zone["touch_count"] > 0,
            "last_verdict": zone.get("last_verdict"),
            "last_confidence": zone.get("last_confidence"),
            "last_reasoning": zone.get("last_reasoning"),
            "bias": ob["direction"], "is_transitional": False,
            "structure_phase": "kontinuitas", "current_price": current_price,
            "supporting_data": get_confirmed_snapshot(df_structure, config),
            "source_detail": ob,
        }
        print(f"[INFO] Pakai OB NYATA yang sedang aktif: {ob['direction']} "
              f"{ob['ob_bottom']:.5f}-{ob['ob_top']:.5f}")
    else:
        print("[INFO] Tidak ada OB aktif -- pakai zona dummy berbasis ATR.")
        current_price = float(df_structure["close"].iloc[-2])
        snap = get_confirmed_snapshot(df_structure, config)
        atr_val = snap["atr_current"]
        zone = zs.find_or_create_zone(
            PAIR, "OB_TEST", "bullish",
            round(current_price - 0.3 * atr_val, 5),
            round(current_price - 0.9 * atr_val, 5))
        candidate = {
            "zone_id": zone["zone_id"], "zone_type": "OB_TEST",
            "direction": "bullish",
            "zone_top": zone["zone_top"], "zone_bottom": zone["zone_bottom"],
            "touch_count": 0, "is_revisit": False,
            "last_verdict": None, "last_confidence": None, "last_reasoning": None,
            "bias": "bullish", "is_transitional": False,
            "structure_phase": "kontinuitas", "current_price": current_price,
            "supporting_data": snap, "source_detail": {},
        }

    print("\n[PROSES] Memanggil evaluate_candidate() -- akan hit Claude API...")
    result = evaluate_candidate(candidate, PAIR, symbol, df_structure,
                                 df_timing, [candidate], config)

    print(f"\n{'='*60}")
    print("HASIL")
    print("=" * 60)
    print(f"outcome  : {result['outcome']}")
    print(f"log_id   : {result['log_id']}")

    # BARU -- tampilkan detail gate risiko kalau ada, supaya
    # execute_blocked_by_risk / circuit_breaker tidak terlewat.
    risk = result.get("risk")
    if risk:
        print("\nDetail risk gate:")
        for k, v in risk.items():
            print(f"  {k}: {v}")

    if result["outcome"] == "execute_blocked_by_risk":
        print("\n[PERHATIAN] Claude memutuskan EXECUTE, tapi risk_engine "
              "MEMBLOKIR trade ini (lihat 'reason' di atas). Posisi TIDAK "
              "ditandai open -- ini perilaku yang BENAR, bukan bug.")
    elif result["outcome"] == "circuit_breaker":
        print("\n[PERHATIAN] Circuit breaker AKTIF -- Claude API TIDAK "
              "dipanggil sama sekali (tidak ada biaya untuk run ini).")

    if result["claude_response"]:
        print("\nRespons Claude lengkap:")
        for k, v in result["claude_response"].items():
            print(f"  {k}: {v}")

    mt5c.shutdown()
    print("\n[SELESAI]")
