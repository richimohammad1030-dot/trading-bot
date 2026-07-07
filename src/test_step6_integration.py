"""
test_step6_integration.py - Fase 5, Step 6

Validasi TERKENDALI jalur integrasi paper_outcome_tracker.py di
tier2_orchestrator.py -- Claude API DI-MOCK (verdict dipaksa "execute"),
supaya deterministik dan TANPA biaya API. MT5 + database tetap ASLI.

BEDA dari test_single_candidate.py: script itu memanggil Claude API
sungguhan tapi hasilnya (execute/skip) tidak bisa dipastikan --
tidak cocok untuk memvalidasi jalur kode yang HANYA berjalan saat
outcome="execute". Test ini memaksa outcome tsb secara terkendali,
tanpa menyentuh call_claude() aslinya di claude_client.py (hanya
mengganti reference di namespace tier2_orchestrator untuk durasi
test ini saja).

Jalankan: python src/test_step6_integration.py
"""

import sys
import sqlite3
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config_loader import load_config
import mt5_connector as mt5c
from indicators import get_confirmed_snapshot
from orderblock import init_db as init_ob_db
import zone_state as zs
import package_log as pkglog
import paper_outcome_tracker as pot
import tier2_orchestrator as orch

PAIR = "EURUSD"
TEST_ZONE_TYPE = "OB_TEST_STEP6"  # tipe dummy, jelas beda dari tipe asli


def fake_call_claude(task_type, payload, images=None, timeout=20.0):
    """
    Stub -- gantikan Claude API sungguhan HANYA untuk test ini.
    Paksa verdict 'execute' dengan SL/TP jarak wajar dari current_price
    (bukan angka acak -- pakai jarak tetap 30/60 pip supaya gate risiko
    tetap bisa hitung lot secara normal, bukan menguji skema harga).
    """
    assert task_type == "final_visual_review", (
        f"Stub ini hanya untuk final_visual_review, dapat: {task_type}"
    )
    current_price = payload["candidate_zone"]["current_price"]
    direction = payload["candidate_zone"]["direction"]
    if direction == "bullish":
        sl = round(current_price - 0.0030, 5)
        tp = round(current_price + 0.0060, 5)
    else:
        sl = round(current_price + 0.0030, 5)
        tp = round(current_price - 0.0060, 5)
    return {
        "visual_alignment": "consistent",
        "additional_observations": "[TEST STUB] dipaksa execute utk validasi Step 6.",
        "final_decision": "execute",
        "confidence": 7,
        "skip_reason_type": None,
        "sl_price": sl,
        "tp_price": tp,
        "exit_reasoning": "[TEST STUB] SL/TP jarak tetap 30/60 pip.",
    }


def cleanup(paper_row_id, log_id):
    """Hapus semua jejak baris test dari 3 tabel -- data dummy TIDAK
    boleh ikut teragregasi nanti oleh daily_evaluation.py/weekly_evaluation.py."""
    conn = sqlite3.connect(pot.DB_PATH)
    if paper_row_id is not None:
        conn.execute("DELETE FROM paper_trade_outcomes WHERE id=?", (paper_row_id,))
    if log_id is not None:
        conn.execute("DELETE FROM tier2_packages WHERE id=?", (log_id,))
    conn.execute("DELETE FROM zone_state WHERE zone_type=?", (TEST_ZONE_TYPE,))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    print("=" * 60)
    print("TEST STEP 6: validasi integrasi paper_outcome_tracker.py")
    print("(Claude API DI-MOCK -- tidak ada biaya, verdict dipaksa execute)")
    print("=" * 60)

    if not mt5c.connect():
        sys.exit(1)

    config = load_config()
    init_ob_db()
    zs.init_db()
    pkglog.init_db()
    pot.init_db()

    # Pasang stub -- gantikan HANYA reference call_claude di namespace
    # tier2_orchestrator (yang sudah di-import ke sana), BUKAN file
    # claude_client.py aslinya.
    orch.call_claude = fake_call_claude

    symbol = mt5c.resolve_symbol(PAIR, config)
    tf_cfg = config.get("screener_timeframes", {})
    df_structure = mt5c.get_candles(symbol, tf_cfg.get("structure_tf", "H1"),
                                     count=tf_cfg.get("structure_lookback", 200))
    df_timing = mt5c.get_candles(symbol, tf_cfg.get("timing_tf", "M5"),
                                  count=tf_cfg.get("timing_lookback", 20) + 40)

    current_price = float(df_structure["close"].iloc[-2])
    snap = get_confirmed_snapshot(df_structure, config)
    atr_val = snap["atr_current"]

    zone = zs.find_or_create_zone(
        PAIR, TEST_ZONE_TYPE, "bullish",
        round(current_price - 0.3 * atr_val, 5),
        round(current_price - 0.9 * atr_val, 5))
    candidate = {
        "zone_id": zone["zone_id"], "zone_type": TEST_ZONE_TYPE,
        "direction": "bullish",
        "zone_top": zone["zone_top"], "zone_bottom": zone["zone_bottom"],
        "touch_count": 0, "is_revisit": False,
        "last_verdict": None, "last_confidence": None, "last_reasoning": None,
        "bias": "bullish", "is_transitional": False,
        "structure_phase": "kontinuitas", "current_price": current_price,
        "supporting_data": snap, "source_detail": {},
    }

    print(f"\n[PROSES] evaluate_candidate() dengan Claude DI-MOCK -> paksa execute...")
    result = orch.evaluate_candidate(candidate, PAIR, symbol, df_structure,
                                      df_timing, [candidate], config)

    print(f"\noutcome  : {result['outcome']}")
    print(f"log_id   : {result['log_id']}")

    log_id = result["log_id"]

    if result["outcome"] != "execute":
        # Bukan integrasi Step 6 yang gagal -- kemungkinan risk gate
        # (circuit breaker/exposure/blacklist) menolak. Bersihkan jejak
        # tier2_packages/zone_state yang mungkin sudah tercatat, lalu
        # laporkan alasan penolakan apa adanya.
        print(f"\n[GAGAL] outcome bukan 'execute': {result['outcome']}")
        if result.get("risk"):
            print(f"  Detail risk gate: {result['risk']}")
        cleanup(None, log_id)
        mt5c.shutdown()
        sys.exit(1)

    # --- Validasi INTI Step 6: baris BARU harus muncul di paper_trade_outcomes ---
    rows = pot.get_outcomes(pair=PAIR, status="pending", limit=20)
    matching = [r for r in rows if r["log_id"] == log_id]

    if len(matching) != 1:
        print(f"\n[GAGAL KRITIS] Tidak ditemukan baris paper_trade_outcomes "
              f"dengan log_id={log_id} -- integrasi record_pending_trade() "
              f"TIDAK berjalan atau log_id salah.")
        cleanup(None, log_id)
        mt5c.shutdown()
        sys.exit(1)

    row = matching[0]
    print(f"\n[OK] Baris baru ditemukan di paper_trade_outcomes:")
    print(f"  id={row['id']} log_id={row['log_id']} pair={row['pair']} "
          f"direction={row['direction']}")
    print(f"  entry_price={row['entry_price']} sl_price={row['sl_price']} "
          f"tp_price={row['tp_price']} risk_pct={row['risk_pct']}")
    print(f"  entry_time_utc={row['entry_time_utc']} status={row['status']}")

    errors = []
    if row["risk_pct"] is None:
        errors.append("risk_pct kosong (gate['risk_pct'] tidak sampai)")
    if row["sl_price"] != result["risk"]["sl_price"]:
        errors.append(f"sl_price tidak cocok: DB={row['sl_price']} vs gate={result['risk']['sl_price']}")
    if row["tp_price"] != result["risk"]["tp_price"]:
        errors.append(f"tp_price tidak cocok: DB={row['tp_price']} vs gate={result['risk']['tp_price']}")

    expected_entry_time = str(df_timing["time"].iloc[-2])
    if row["entry_time_utc"] != expected_entry_time:
        errors.append(
            f"entry_time_utc tidak cocok: DB={row['entry_time_utc']!r} vs "
            f"df_timing.iloc[-2]={expected_entry_time!r} -- integrasi "
            f"kemungkinan memakai sumber jam yang salah."
        )

    cleanup(row["id"], log_id)
    print(f"[CLEANUP] Baris test (paper_trade_outcomes id={row['id']}, "
          f"tier2_packages log_id={log_id}, zone_state {TEST_ZONE_TYPE}) dihapus.")

    mt5c.shutdown()

    if errors:
        print("\n[GAGAL] Ditemukan ketidakcocokan field:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    print("\n[OK] Semua field integrasi (risk_pct, sl_price, tp_price, "
          "entry_time_utc) cocok dengan yang dikirim orchestrator.")
    print("\n[SELESAI] Step 6 -- integrasi paper_outcome_tracker.py TERVALIDASI.")
