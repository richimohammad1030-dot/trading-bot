"""
daily_evaluation.py - Fase 5, Step 7

Agregasi HARIAN dari tier2_packages (package_log.py) + paper_trade_outcomes
(paper_outcome_tracker.py) -> payload Task 6 (daily_evaluation) PERSIS
sesuai "Input contoh" di docs/system_prompt.md -> call_claude() -> simpan
hasil ke tabel baru daily_evaluations.

Dijadwalkan jam 23:00 WIB (03_trading_rules.md: "Evaluasi harian: 23:00
WIB") -- scheduler-nya sendiri BELUM dibangun (di luar scope Task 6/7,
fase terpisah setelah Learning Ledger core selesai, lihat kurikulum).

CATATAN: modul ini murni agregasi SQL + pemetaan field ke skema yang
SUDAH DIKUNCI (bukan algoritma baru), jadi tidak ada riset eksternal
yang relevan di sini -- berbeda dari paper_outcome_tracker.py yang
memang butuh riset metodologi same-bar ambiguity.

PENTING soal tanggal: satu "hari evaluasi" mengikuti KALENDER WIB
(UTC+7, tanpa DST), BUKAN kalender UTC -- konsisten dengan jadwal
23:00 WIB. tier2_packages.timestamp_utc adalah UTC ASLI (dari
package_builder.py, datetime.now(timezone.utc)) sehingga range query
dikonversi dari WIB ke UTC di get_wib_day_range_utc().

PENTING soal verdict 'execute' tanpa baris paper_trade_outcomes:
itu berarti execution gate (risk_engine.py) MENOLAK trade tsb
(outcome='execute_blocked_by_risk' di tier2_orchestrator.py) --
BUKAN eksekusi sungguhan. Dihitung terpisah sebagai "risk_blocked",
TIDAK masuk daftar "trades" atau "executed" (v2.1 Bab 0.1: jangan
campur keputusan Claude dengan aturan risiko mekanis saat melaporkan
performa -- performa hanya dihitung dari trade yang BENAR-BENAR jalan).
"""

import sys
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import ROOT_DIR
from claude_client import call_claude

DB_PATH = ROOT_DIR / "data" / "trading_bot.db"
WIB = timezone(timedelta(hours=7))


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Buat tabel daily_evaluations jika belum ada."""
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_evaluations (
            date                    TEXT PRIMARY KEY,  -- 'YYYY-MM-DD' kalender WIB
            summary                 TEXT,
            patterns_observed       TEXT,   -- JSON list
            flags_for_weekly_review TEXT,   -- JSON list
            candidates_seen         INTEGER,
            executed                INTEGER,
            skipped                 INTEGER,
            api_failed              INTEGER,
            risk_blocked            INTEGER,
            raw_payload_json        TEXT,
            created_at              TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def get_wib_day_range_utc(date_str: str) -> tuple:
    """
    Konversi tanggal kalender WIB 'YYYY-MM-DD' -> rentang UTC [start, end)
    dalam string ISO, untuk query timestamp_utc (UTC asli) di
    tier2_packages.

    Contoh: date_str="2026-07-03" (WIB) -> UTC range
    "2026-07-02T17:00:00+00:00" s.d. "2026-07-03T17:00:00+00:00"
    (WIB = UTC+7 tetap, tanpa DST).
    """
    day_wib = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=WIB)
    start_utc = day_wib.astimezone(timezone.utc)
    end_utc = (day_wib + timedelta(days=1)).astimezone(timezone.utc)
    return start_utc.isoformat(), end_utc.isoformat()


def aggregate_day(date_str: str) -> dict:
    """
    Kumpulkan semua data hari `date_str` (kalender WIB) dari
    tier2_packages + paper_trade_outcomes, bentuk PERSIS sesuai
    "Input contoh" Task 6 di system_prompt.md (field task_type TIDAK
    disertakan di sini -- call_claude() menambahkannya otomatis).
    """
    start_utc, end_utc = get_wib_day_range_utc(date_str)

    conn = _connect()

    packages = conn.execute("""
        SELECT id, pair, zone_type, direction, claude_verdict,
               claude_confidence, claude_skip_reason_type
        FROM tier2_packages
        WHERE timestamp_utc >= ? AND timestamp_utc < ?
    """, (start_utc, end_utc)).fetchall()

    trades = []
    executed = 0
    skipped = 0
    api_failed = 0
    risk_blocked = 0
    skip_reason_breakdown = {}
    result_breakdown = {"TP": 0, "SL": 0, "pending": 0}
    by_pair = {}

    for pkg in packages:
        pair = pkg["pair"]
        by_pair.setdefault(pair, {"candidates": 0, "executed": 0})
        by_pair[pair]["candidates"] += 1

        if pkg["claude_verdict"] is None:
            # Timeout/JSON invalid (v2.3 Bab 1) -- jangan ditelan
            # diam-diam, tapi juga bukan "skip" ataupun "execute" sungguhan.
            api_failed += 1
            continue

        if pkg["claude_verdict"] == "skip":
            skipped += 1
            reason = pkg["claude_skip_reason_type"] or "unknown"
            skip_reason_breakdown[reason] = skip_reason_breakdown.get(reason, 0) + 1
            continue

        if pkg["claude_verdict"] == "execute":
            outcome_row = conn.execute("""
                SELECT * FROM paper_trade_outcomes WHERE log_id=?
            """, (pkg["id"],)).fetchone()

            if outcome_row is None:
                # verdict='execute' tapi TIDAK ada baris paper_trade_outcomes
                # -> ditolak execution gate (risk_engine), lihat catatan
                # modul di atas.
                risk_blocked += 1
                continue

            executed += 1
            by_pair[pair]["executed"] += 1

            result = outcome_row["result"] if outcome_row["status"] == "closed" else "pending"
            result_breakdown[result] = result_breakdown.get(result, 0) + 1

            trades.append({
                "pair": pair,
                "zone_type": pkg["zone_type"],
                "direction": pkg["direction"],
                "confidence": pkg["claude_confidence"],
                "result": result,
                "rr_achieved": outcome_row["rr_achieved"],
                "pnl_pct": outcome_row["pnl_pct"],
                "entry_time_utc": outcome_row["entry_time_utc"],
                "closed_time_utc": outcome_row["closed_time_utc"],
            })

    conn.close()

    return {
        "date": date_str,
        "trades": trades,
        "totals": {
            "candidates_seen": len(packages),
            "executed": executed,
            "skipped": skipped,
            "skip_reason_breakdown": skip_reason_breakdown,
            "result_breakdown": result_breakdown,
            "api_failed": api_failed,
            "risk_blocked": risk_blocked,
        },
        "by_pair": by_pair,
    }


def run_daily_evaluation(date_str: str = None, persist: bool = True) -> dict:
    """
    Jalankan evaluasi harian lengkap: agregasi -> call_claude -> simpan.

    Args:
        date_str: 'YYYY-MM-DD' (kalender WIB). Default None -> HARI INI
                   (WIB, dari jam sistem saat dipanggil).
        persist: simpan ke tabel daily_evaluations. Set False untuk
                 dry-run/test tanpa menulis DB.

    Return: dict {"date": str, "payload": dict, "response": dict}
    """
    if date_str is None:
        date_str = datetime.now(WIB).strftime("%Y-%m-%d")

    payload = aggregate_day(date_str)
    response = call_claude("daily_evaluation", payload)

    if persist:
        conn = _connect()
        conn.execute("""
            INSERT INTO daily_evaluations
                (date, summary, patterns_observed, flags_for_weekly_review,
                 candidates_seen, executed, skipped, api_failed, risk_blocked,
                 raw_payload_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
                summary=excluded.summary,
                patterns_observed=excluded.patterns_observed,
                flags_for_weekly_review=excluded.flags_for_weekly_review,
                candidates_seen=excluded.candidates_seen,
                executed=excluded.executed,
                skipped=excluded.skipped,
                api_failed=excluded.api_failed,
                risk_blocked=excluded.risk_blocked,
                raw_payload_json=excluded.raw_payload_json,
                created_at=datetime('now')
        """, (
            date_str, response.get("summary"),
            json.dumps(response.get("patterns_observed", []), ensure_ascii=False),
            json.dumps(response.get("flags_for_weekly_review", []), ensure_ascii=False),
            payload["totals"]["candidates_seen"], payload["totals"]["executed"],
            payload["totals"]["skipped"], payload["totals"]["api_failed"],
            payload["totals"]["risk_blocked"],
            json.dumps(payload, ensure_ascii=False, default=str),
        ))
        conn.commit()
        conn.close()

    return {"date": date_str, "payload": payload, "response": response}


def get_daily_evaluation(date_str: str) -> dict:
    """
    Ambil hasil evaluasi harian yang tersimpan -- dipakai
    weekly_evaluation.py (Step 9) untuk merangkai daily_flags.
    Return None kalau belum pernah dievaluasi.
    """
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM daily_evaluations WHERE date=?", (date_str,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    result = dict(row)
    result["patterns_observed"] = json.loads(result["patterns_observed"] or "[]")
    result["flags_for_weekly_review"] = json.loads(result["flags_for_weekly_review"] or "[]")
    return result


if __name__ == "__main__":
    print("=" * 60)
    print("TEST: daily_evaluation.py (Fase 5, Step 7)")
    print("=" * 60)

    init_db()
    print("[OK] init_db() -- tabel daily_evaluations siap.")

    today_wib = datetime.now(WIB).strftime("%Y-%m-%d")
    print(f"\n[PROSES] Agregasi + evaluasi untuk tanggal WIB: {today_wib}")
    print("[PERINGATAN] Ini memanggil Claude API sungguhan (1x, tanpa gambar).")

    result = run_daily_evaluation(today_wib, persist=True)

    print("\n--- PAYLOAD terkirim ke Claude ---")
    print(json.dumps(result["payload"], indent=2, ensure_ascii=False, default=str))

    print("\n--- RESPONSE Claude ---")
    print(json.dumps(result["response"], indent=2, ensure_ascii=False))

    # Validasi skema dasar (sama seperti test_task6_task7.py)
    resp = result["response"]
    assert "summary" in resp and isinstance(resp["summary"], str), \
        "FAIL: 'summary' hilang/bukan string"
    assert "patterns_observed" in resp and isinstance(resp["patterns_observed"], list), \
        "FAIL: 'patterns_observed' hilang/bukan list"
    assert "flags_for_weekly_review" in resp and isinstance(resp["flags_for_weekly_review"], list), \
        "FAIL: 'flags_for_weekly_review' hilang/bukan list"
    print("\n[OK] Skema response valid.")

    saved = get_daily_evaluation(today_wib)
    print(f"\n--- Tersimpan di DB (get_daily_evaluation) ---")
    print(json.dumps(saved, indent=2, ensure_ascii=False, default=str))
    assert saved is not None, "FAIL: baris tidak ditemukan setelah persist=True"
    assert saved["summary"] == resp["summary"], "FAIL: summary tersimpan tidak cocok"

    print("\n[SELESAI] daily_evaluation.py -- Step 7 tervalidasi.")
