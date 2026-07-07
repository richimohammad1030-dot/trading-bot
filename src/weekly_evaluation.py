"""
weekly_evaluation.py - Fase 5, Step 9

Agregasi MINGGUAN (7 hari kalender WIB trailing) dari tier2_packages +
paper_trade_outcomes -> week_summary, PLUS cumulative_since_live
(SELURUH histori paper_trade_outcomes.status='closed', bukan cuma
minggu ini -- KHUSUS untuk guardrail sample size P-03) -> payload
Task 7 (weekly_evaluation) PERSIS sesuai "Input contoh" di
docs/system_prompt.md -> call_claude() -> simpan hasil ke tabel baru
weekly_evaluations.

Dijadwalkan Minggu 23:30 WIB (03_trading_rules.md). Scheduler-nya
sendiri BELUM dibangun (fase terpisah, lihat kurikulum Step 12).

CATATAN: modul ini murni agregasi SQL + pemetaan field ke skema yang
SUDAH DIKUNCI (sama seperti daily_evaluation.py), formula win_rate_pct/
expectancy_pct SUDAH didefinisikan Claude sendiri di system_prompt.md
(dihitung Claude dari cumulative_since_live yang dikirim, bukan
dihitung ulang di Python) -- jadi tidak ada riset algoritma baru yang
relevan di sini.

daily_flags di week_summary DIAMBIL dari tabel daily_evaluations
(Step 7) -- artinya weekly_evaluation.py PALING AKURAT kalau
daily_evaluation.py sudah dijalankan tiap hari dalam rentang minggu
ini. Hari yang belum pernah dievaluasi akan muncul dengan
flags_for_weekly_review=[] (bukan error, cuma belum ada datanya).
"""

import sys
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import ROOT_DIR
from claude_client import call_claude
from daily_evaluation import WIB, get_wib_day_range_utc, get_daily_evaluation

DB_PATH = ROOT_DIR / "data" / "trading_bot.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Buat tabel weekly_evaluations jika belum ada."""
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS weekly_evaluations (
            week_end                   TEXT PRIMARY KEY,  -- 'YYYY-MM-DD' WIB
            week_start                 TEXT,
            performance_summary_json   TEXT,
            recommendations_json       TEXT,
            requires_human_approval    INTEGER,
            raw_payload_json           TEXT,
            created_at                 TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def aggregate_week(week_start_str: str, week_end_str: str) -> dict:
    """
    Kumpulkan performa 7 hari (week_start s.d. week_end, inklusif,
    kalender WIB) dari tier2_packages + paper_trade_outcomes. Trade
    dengan verdict='execute' tapi TANPA baris paper_trade_outcomes
    (ditolak execution gate risk_engine) DIKECUALIKAN, sama seperti
    daily_evaluation.py.
    """
    start_utc, _ = get_wib_day_range_utc(week_start_str)
    _, end_utc = get_wib_day_range_utc(week_end_str)

    conn = _connect()

    packages = conn.execute("""
        SELECT id, pair, zone_type, claude_verdict
        FROM tier2_packages
        WHERE timestamp_utc >= ? AND timestamp_utc < ?
    """, (start_utc, end_utc)).fetchall()

    candidates_seen = len(packages)
    executed = 0
    result_breakdown = {"TP": 0, "SL": 0, "pending": 0}
    by_pair = {}
    by_zone_type = {}
    rr_values = []

    for pkg in packages:
        if pkg["claude_verdict"] != "execute":
            continue

        outcome_row = conn.execute(
            "SELECT * FROM paper_trade_outcomes WHERE log_id=?", (pkg["id"],)
        ).fetchone()
        if outcome_row is None:
            continue  # risk_blocked -- sama perlakuan dgn daily_evaluation.py

        pair = pkg["pair"]
        zt = pkg["zone_type"]

        executed += 1
        result = outcome_row["result"] if outcome_row["status"] == "closed" else "pending"
        result_breakdown[result] = result_breakdown.get(result, 0) + 1

        by_pair.setdefault(pair, {"executed": 0, "TP": 0, "SL": 0})
        by_pair[pair]["executed"] += 1
        by_zone_type.setdefault(zt, {"executed": 0, "TP": 0, "SL": 0})
        by_zone_type[zt]["executed"] += 1

        if result in ("TP", "SL"):
            by_pair[pair][result] += 1
            by_zone_type[zt][result] += 1
            if outcome_row["rr_achieved"] is not None:
                rr_values.append(outcome_row["rr_achieved"])

    conn.close()

    total_closed = result_breakdown["TP"] + result_breakdown["SL"]
    win_rate_pct = round(result_breakdown["TP"] / total_closed * 100, 2) if total_closed > 0 else 0.0
    avg_rr_achieved = round(sum(rr_values) / len(rr_values), 3) if rr_values else 0.0

    # daily_flags: rangkai dari daily_evaluations (Step 7) tiap hari
    # dalam rentang minggu ini.
    daily_flags = []
    d = datetime.strptime(week_start_str, "%Y-%m-%d")
    end_d = datetime.strptime(week_end_str, "%Y-%m-%d")
    while d <= end_d:
        date_str = d.strftime("%Y-%m-%d")
        daily = get_daily_evaluation(date_str)
        daily_flags.append({
            "date": date_str,
            "flags_for_weekly_review": daily["flags_for_weekly_review"] if daily else [],
        })
        d += timedelta(days=1)

    return {
        "week_start": week_start_str,
        "week_end": week_end_str,
        "candidates_seen": candidates_seen,
        "executed": executed,
        "result_breakdown": result_breakdown,
        "win_rate_pct": win_rate_pct,
        "avg_rr_achieved": avg_rr_achieved,
        "by_pair": by_pair,
        "by_zone_type": by_zone_type,
        "daily_flags": daily_flags,
    }


def aggregate_cumulative_since_live() -> dict:
    """
    Akumulasi SELURUH histori paper_trade_outcomes.status='closed'
    (BUKAN cuma minggu ini) -- KHUSUS untuk guardrail sample size P-03
    di Task 7 (system_prompt.md). total_be SELALU 0 -- Model A saat
    ini tidak mensimulasikan breakeven (field dipertahankan untuk
    kompatibilitas skema, lihat catatan di system_prompt.md).
    """
    conn = _connect()
    row = conn.execute("""
        SELECT
            COUNT(*) as total_trades,
            SUM(CASE WHEN result='TP' THEN 1 ELSE 0 END) as total_tp,
            SUM(CASE WHEN result='SL' THEN 1 ELSE 0 END) as total_sl,
            AVG(CASE WHEN result='TP' THEN pnl_pct END) as avg_win_pct,
            AVG(CASE WHEN result='SL' THEN ABS(pnl_pct) END) as avg_loss_pct
        FROM paper_trade_outcomes
        WHERE status='closed'
    """).fetchone()
    conn.close()

    return {
        "total_trades": row["total_trades"] or 0,
        "total_tp": row["total_tp"] or 0,
        "total_sl": row["total_sl"] or 0,
        "total_be": 0,
        "avg_win_pct": round(row["avg_win_pct"], 4) if row["avg_win_pct"] is not None else 0.0,
        "avg_loss_pct": round(row["avg_loss_pct"], 4) if row["avg_loss_pct"] is not None else 0.0,
    }


def run_weekly_evaluation(week_end_str: str = None, persist: bool = True) -> dict:
    """
    Jalankan evaluasi mingguan lengkap: agregasi 7 hari trailing
    (berakhir di week_end_str, kalender WIB) + cumulative_since_live
    -> call_claude -> simpan.

    Args:
        week_end_str: 'YYYY-MM-DD' (kalender WIB). Default None -> HARI
                       INI (WIB). week_start dihitung otomatis = 6 hari
                       sebelumnya (rentang 7 hari inklusif).
        persist: simpan ke tabel weekly_evaluations. Set False untuk
                 dry-run/test.

    Return: dict {"week_start", "week_end", "payload", "response"}
    """
    if week_end_str is None:
        week_end_str = datetime.now(WIB).strftime("%Y-%m-%d")
    week_end_d = datetime.strptime(week_end_str, "%Y-%m-%d")
    week_start_d = week_end_d - timedelta(days=6)
    week_start_str = week_start_d.strftime("%Y-%m-%d")

    week_summary = aggregate_week(week_start_str, week_end_str)
    cumulative = aggregate_cumulative_since_live()

    payload = {
        "week_summary": week_summary,
        "cumulative_since_live": cumulative,
    }

    response = call_claude("weekly_evaluation", payload)

    if persist:
        conn = _connect()
        conn.execute("""
            INSERT INTO weekly_evaluations
                (week_end, week_start, performance_summary_json,
                 recommendations_json, requires_human_approval,
                 raw_payload_json)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(week_end) DO UPDATE SET
                week_start=excluded.week_start,
                performance_summary_json=excluded.performance_summary_json,
                recommendations_json=excluded.recommendations_json,
                requires_human_approval=excluded.requires_human_approval,
                raw_payload_json=excluded.raw_payload_json,
                created_at=datetime('now')
        """, (
            week_end_str, week_start_str,
            json.dumps(response.get("performance_summary", {}), ensure_ascii=False),
            json.dumps(response.get("recommendations", []), ensure_ascii=False),
            int(bool(response.get("requires_human_approval", True))),
            json.dumps(payload, ensure_ascii=False, default=str),
        ))
        conn.commit()
        conn.close()

    return {"week_start": week_start_str, "week_end": week_end_str,
            "payload": payload, "response": response}


def get_weekly_evaluation(week_end_str: str) -> dict:
    """Ambil hasil evaluasi mingguan yang tersimpan. Return None kalau belum ada."""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM weekly_evaluations WHERE week_end=?", (week_end_str,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    result = dict(row)
    result["performance_summary"] = json.loads(result["performance_summary_json"] or "{}")
    result["recommendations"] = json.loads(result["recommendations_json"] or "[]")
    return result


if __name__ == "__main__":
    print("=" * 60)
    print("TEST: weekly_evaluation.py (Fase 5, Step 9)")
    print("=" * 60)

    init_db()
    print("[OK] init_db() -- tabel weekly_evaluations siap.")

    today_wib = datetime.now(WIB).strftime("%Y-%m-%d")
    print(f"\n[PROSES] Agregasi 7 hari trailing berakhir {today_wib} (WIB) "
          f"+ cumulative_since_live -> evaluasi mingguan")
    print("[PERINGATAN] Ini memanggil Claude API sungguhan (1x, tanpa gambar).")

    result = run_weekly_evaluation(today_wib, persist=True)

    print("\n--- PAYLOAD terkirim ke Claude ---")
    print(json.dumps(result["payload"], indent=2, ensure_ascii=False, default=str))

    print("\n--- RESPONSE Claude ---")
    print(json.dumps(result["response"], indent=2, ensure_ascii=False))

    # --- Validasi skema dasar (sama seperti test_task6_task7.py) ---
    resp = result["response"]
    assert "performance_summary" in resp, "FAIL: 'performance_summary' hilang"
    ps = resp["performance_summary"]
    for field in ("total_trades", "win_rate_pct", "expectancy_pct",
                  "sample_size_warning", "best_pair", "worst_pair"):
        assert field in ps, f"FAIL: performance_summary.{field} hilang"

    cumulative_total = result["payload"]["cumulative_since_live"]["total_trades"]
    expected_warning = cumulative_total < 30
    assert ps["sample_size_warning"] == expected_warning, (
        f"FAIL KRITIS: cumulative_since_live.total_trades={cumulative_total} "
        f"({'<' if expected_warning else '>='}30) tapi "
        f"sample_size_warning={ps['sample_size_warning']!r}, "
        f"seharusnya {expected_warning}. Guardrail P-03 TIDAK dipatuhi."
    )

    assert "recommendations" in resp and isinstance(resp["recommendations"], list), \
        "FAIL: 'recommendations' hilang/bukan list"
    for rec in resp["recommendations"]:
        assert rec.get("confidence_label") in ("tentative", "confirmed"), \
            f"FAIL: confidence_label tidak valid: {rec.get('confidence_label')!r}"
        if expected_warning:
            assert rec["confidence_label"] != "confirmed", (
                f"FAIL KRITIS: rekomendasi 'confirmed' padahal n<30: {rec}"
            )

    assert resp.get("requires_human_approval") is True, \
        "FAIL KRITIS: requires_human_approval harus selalu true"

    print(f"\n[OK] Skema valid. cumulative_since_live.total_trades="
          f"{cumulative_total} -> sample_size_warning={ps['sample_size_warning']} "
          f"(sesuai guardrail n>=30).")

    saved = get_weekly_evaluation(today_wib)
    assert saved is not None, "FAIL: baris tidak ditemukan setelah persist=True"
    print("\n[SELESAI] weekly_evaluation.py -- Step 9 tervalidasi.")
