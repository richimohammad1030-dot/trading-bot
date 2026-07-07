"""
test_task6_task7.py - Fase 5, Step 4

Validasi skema Task 6 (daily_evaluation) dan Task 7 (weekly_evaluation)
di system_prompt.md SEBELUM daily_evaluation.py/weekly_evaluation.py
(Step 7 & 9) dibangun. Payload di sini PERSIS mengikuti "Input contoh"
di system_prompt.md final (field result: TP/SL/pending, pnl_pct,
week_summary lengkap, cumulative_since_live terpisah dari performa
minggu ini).

Jalankan: python src/test_task6_task7.py
"""

import sys
from pathlib import Path
import json

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_client import call_claude


def test_daily_evaluation():
    print("=" * 60)
    print("TEST: Task 6 - daily_evaluation")
    print("=" * 60)

    payload = {
        "date": "2026-07-03",
        "trades": [
            {
                "pair": "EURUSD", "zone_type": "OB", "direction": "bullish",
                "confidence": 8, "result": "TP", "rr_achieved": 2.1,
                "pnl_pct": 2.1,
                "entry_time_utc": "2026-07-03 08:15:00",
                "closed_time_utc": "2026-07-03 11:40:00",
            },
            {
                "pair": "XAUUSD", "zone_type": "SBR", "direction": "bearish",
                "confidence": 6, "result": "SL", "rr_achieved": -1.0,
                "pnl_pct": -1.0,
                "entry_time_utc": "2026-07-03 09:40:00",
                "closed_time_utc": "2026-07-03 10:05:00",
            },
            {
                "pair": "GBPUSD", "zone_type": "FVG", "direction": "bullish",
                "confidence": 7, "result": "pending", "rr_achieved": None,
                "pnl_pct": None,
                "entry_time_utc": "2026-07-03 14:20:00",
                "closed_time_utc": None,
            },
        ],
        "totals": {
            "candidates_seen": 12, "executed": 3, "skipped": 9,
            "skip_reason_breakdown": {"not_yet_formed": 5, "setup_invalid": 4},
            "result_breakdown": {"TP": 1, "SL": 1, "pending": 1},
        },
        "by_pair": {
            "EURUSD": {"candidates": 5, "executed": 1},
            "XAUUSD": {"candidates": 4, "executed": 1},
            "GBPUSD": {"candidates": 3, "executed": 1},
        },
    }

    print("Payload:", json.dumps(payload, indent=2))
    print("\nMemanggil Claude API...\n")

    result = call_claude("daily_evaluation", payload)

    print("Response (parsed dict):")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    # --- Validasi skema WAJIB (sesuai Output WAJIB Task 6) ---
    assert "summary" in result, "FAIL: field 'summary' hilang"
    assert isinstance(result["summary"], str), "FAIL: 'summary' bukan string"

    assert "patterns_observed" in result, "FAIL: field 'patterns_observed' hilang"
    assert isinstance(result["patterns_observed"], list), \
        "FAIL: 'patterns_observed' bukan list"

    assert "flags_for_weekly_review" in result, \
        "FAIL: field 'flags_for_weekly_review' hilang"
    assert isinstance(result["flags_for_weekly_review"], list), \
        "FAIL: 'flags_for_weekly_review' bukan list"

    print("\n[OK] Skema Task 6 (daily_evaluation) valid.")
    return result


def test_weekly_evaluation_guardrail():
    print("\n" + "=" * 60)
    print("TEST: Task 7 - weekly_evaluation (guardrail n<30)")
    print("=" * 60)

    payload = {
        "week_summary": {
            "week_start": "2026-06-29",
            "week_end": "2026-07-05",
            "candidates_seen": 68,
            "executed": 14,
            "result_breakdown": {"TP": 8, "SL": 5, "pending": 1},
            "win_rate_pct": 61.5,
            "avg_rr_achieved": 1.42,
            "by_pair": {
                "EURUSD": {"executed": 6, "TP": 3, "SL": 3},
                "XAUUSD": {"executed": 4, "TP": 3, "SL": 1},
                "GBPUSD": {"executed": 4, "TP": 2, "SL": 1},
            },
            "by_zone_type": {
                "OB": {"executed": 9, "TP": 6, "SL": 3},
                "SBR": {"executed": 3, "TP": 1, "SL": 2},
                "FVG": {"executed": 2, "TP": 1, "SL": 0},
            },
            "daily_flags": [
                {"date": "2026-06-29", "flags_for_weekly_review": []},
                {"date": "2026-06-30",
                 "flags_for_weekly_review": ["3 skip beruntun setup_invalid di XAUUSD"]},
            ],
        },
        # SENGAJA < 30 -- ini test KRITIS guardrail P-03
        # (04_design_decisions.md). week_summary.executed=14 terlihat
        # aktif, tapi cumulative_since_live.total_trades=22 (<30)
        # HARUS tetap memicu sample_size_warning=true.
        "cumulative_since_live": {
            "total_trades": 22,
            "total_tp": 12,
            "total_sl": 10,
            "total_be": 0,
            "avg_win_pct": 1.8,
            "avg_loss_pct": 0.9,
        },
    }

    print("Payload:", json.dumps(payload, indent=2))
    print("\nMemanggil Claude API...\n")

    result = call_claude("weekly_evaluation", payload)

    print("Response (parsed dict):")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    # --- Validasi skema WAJIB (sesuai Output WAJIB Task 7) ---
    assert "performance_summary" in result, "FAIL: field 'performance_summary' hilang"
    ps = result["performance_summary"]

    for field in ("total_trades", "win_rate_pct", "expectancy_pct",
                  "sample_size_warning", "best_pair", "worst_pair"):
        assert field in ps, f"FAIL: performance_summary.{field} hilang"

    # KRITIS (P-03, 04_design_decisions.md): cumulative_since_live.total_trades
    # =22 (<30) -> sample_size_warning WAJIB true, TERLEPAS dari
    # week_summary.executed=14 yang terlihat aktif minggu ini.
    assert ps["sample_size_warning"] is True, (
        "FAIL KRITIS: cumulative_since_live.total_trades=22 (<30) tapi "
        f"sample_size_warning={ps['sample_size_warning']!r}, seharusnya True. "
        "Guardrail P-03 (n>=30) TIDAK dipatuhi -- system_prompt Task 7 "
        "perlu diperkuat instruksinya."
    )

    assert "recommendations" in result, "FAIL: field 'recommendations' hilang"
    assert isinstance(result["recommendations"], list), \
        "FAIL: 'recommendations' bukan list"

    for rec in result["recommendations"]:
        for field in ("area", "suggestion", "rationale", "confidence_label"):
            assert field in rec, f"FAIL: recommendations[].{field} hilang"
        assert rec["confidence_label"] in ("tentative", "confirmed"), \
            f"FAIL: confidence_label tidak valid: {rec['confidence_label']!r}"
        # KRITIS: karena n<30, TIDAK BOLEH ada rekomendasi "confirmed"
        # untuk area yang bersifat struktural (P-03).
        assert rec["confidence_label"] != "confirmed", (
            f"FAIL KRITIS: rekomendasi 'confirmed' padahal n<30 total: {rec}. "
            "Guardrail P-03 TIDAK dipatuhi."
        )

    assert result.get("requires_human_approval") is True, (
        "FAIL KRITIS: requires_human_approval harus SELALU true, "
        f"didapat: {result.get('requires_human_approval')!r}"
    )

    print("\n[OK] Skema Task 7 (weekly_evaluation) valid, guardrail n<30 DIPATUHI.")
    return result


if __name__ == "__main__":
    try:
        test_daily_evaluation()
    except AssertionError as e:
        print(f"\n[GAGAL] Task 6: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] Task 6: {type(e).__name__}: {e}")
        sys.exit(1)

    try:
        test_weekly_evaluation_guardrail()
    except AssertionError as e:
        print(f"\n[GAGAL] Task 7: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] Task 7: {type(e).__name__}: {e}")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("[SELESAI] Task 6 & Task 7 skema tervalidasi. Aman lanjut ke")
    print("Step 5: integrasi paper_outcome_tracker.py ke tier2_orchestrator.py.")
    print("=" * 60)
