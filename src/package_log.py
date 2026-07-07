"""
package_log.py - Fase 3 (lanjutan): Simpan Paket Tier 1 -> Tier 2 (Opsi B)

Menyimpan snapshot RINGKAS dari tiap paket yang dirakit package_builder.py
ke database (trading_bot.db, tabel BARU: tier2_packages). Ini fondasi
Learning Ledger (v2.0 Bab 9) -- tanpa ini, riwayat konteks keputusan
hilang begitu program selesai jalan.

DESAIN DUA TAHAP:
1. save_package() dipanggil SAAT paket dirakit (SEBELUM dikirim ke
   Claude) -- claude_verdict/confidence/reasoning masih NULL.
2. update_verdict() dipanggil Fase 4 SETELAH Claude membalas --
   mengisi verdict/confidence/reasoning ke baris yang sama (via log_id
   yang dikembalikan save_package()).

Ini juga berguna untuk AUDIT kegagalan (v2.3 Bab 1, timeout API):
kalau panggilan Claude timeout/gagal, baris tetap ada dengan verdict
NULL -- bukti bahwa kita SUDAH bertanya tapi tidak dapat jawaban,
bukan diam-diam kehilangan konteks.

Chart TIDAK disimpan ulang sebagai blob -- hanya PATH-nya (file PNG
sudah permanen di data/charts/ lewat chart_generator.py). Field
kompleks (other_zones_nearby, fvg_context, liquidity_context, dst)
disimpan sebagai JSON text di full_package_json -- kolom utama untuk
query cepat, JSON untuk audit/analisis mendalam nanti.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import ROOT_DIR

# KONSISTEN dengan zone_state.py, orderblock.py: SATU file trading_bot.db
# untuk semua modul (tabel berbeda per modul).
DB_PATH = ROOT_DIR / "data" / "trading_bot.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Buat tabel tier2_packages jika belum ada."""
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tier2_packages (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_id                  TEXT NOT NULL,
            pair                     TEXT NOT NULL,
            symbol                   TEXT NOT NULL,
            timestamp_utc            TEXT NOT NULL,
            session                  TEXT,
            bias                     TEXT,
            is_transitional          INTEGER,
            structure_phase          TEXT,
            zone_type                TEXT,
            direction                TEXT,
            zone_top                 REAL,
            zone_bottom              REAL,
            current_price            REAL,
            touch_count              INTEGER,
            is_revisit                INTEGER,
            h1_chart_path            TEXT,
            m5_chart_path            TEXT,
            full_package_json        TEXT NOT NULL,
            claude_verdict           TEXT,
            claude_confidence        INTEGER,
            claude_reasoning         TEXT,
            claude_skip_reason_type  TEXT,
            responded_at             TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pkg_zone ON tier2_packages(zone_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pkg_pair ON tier2_packages(pair)")
    conn.commit()
    conn.close()


def save_package(package: dict) -> int:
    """
    Simpan paket SEBELUM dikirim ke Claude (verdict masih kosong).
    Return log_id -- WAJIB disimpan pemanggil untuk update_verdict()
    nanti setelah Claude membalas.
    """
    conn = _connect()
    cz = package["candidate_zone"]
    zh = package["zone_history"]
    cur = conn.execute("""
        INSERT INTO tier2_packages
            (zone_id, pair, symbol, timestamp_utc, session,
             bias, is_transitional, structure_phase,
             zone_type, direction, zone_top, zone_bottom, current_price,
             touch_count, is_revisit, h1_chart_path, m5_chart_path,
             full_package_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        cz["zone_id"], package["pair"], package["symbol"],
        package["timestamp_utc"], package["session"],
        package["bias"]["value"], int(package["bias"]["is_transitional"]),
        package["bias"]["structure_phase"],
        cz["zone_type"], cz["direction"], cz["zone_top"], cz["zone_bottom"],
        cz["current_price"],
        zh["touch_count"], int(zh["is_revisit"]),
        package["charts"]["h1_chart_path"], package["charts"]["m5_chart_path"],
        json.dumps(package, default=str),
    ))
    conn.commit()
    log_id = cur.lastrowid
    conn.close()
    return log_id


def update_verdict(log_id: int, verdict: str, confidence: int,
                    reasoning: str, skip_reason_type: str = None):
    """
    Isi hasil keputusan Claude ke baris yang sama (dipanggil Fase 4
    setelah dapat respons). skip_reason_type hanya diisi jika
    verdict='skip' (v2.1 Bab 5: not_yet_formed / setup_invalid).
    """
    conn = _connect()
    conn.execute("""
        UPDATE tier2_packages
        SET claude_verdict=?, claude_confidence=?, claude_reasoning=?,
            claude_skip_reason_type=?, responded_at=?
        WHERE id=?
    """, (verdict, confidence, reasoning, skip_reason_type,
          datetime.now(timezone.utc).isoformat(), log_id))
    conn.commit()
    conn.close()


def get_package_history(pair: str = None, zone_type: str = None,
                         limit: int = 50) -> list:
    """
    Ambil riwayat paket -- fondasi query untuk Learning Ledger (Fase 5).
    Contoh pemakaian nanti: "setup mirip ini, N kejadian terakhir,
    win rate berapa?" (v2.0 Bab 9.2).
    """
    conn = _connect()
    query = "SELECT * FROM tier2_packages WHERE 1=1"
    params = []
    if pair:
        query += " AND pair=?"
        params.append(pair)
    if zone_type:
        query += " AND zone_type=?"
        params.append(zone_type)
    query += " ORDER BY timestamp_utc DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_pending_responses() -> int:
    """
    Berapa paket yang SUDAH dikirim tapi BELUM dapat verdict --
    berguna untuk audit kegagalan API (v2.3 Bab 1: timeout/gagal
    HARUS meninggalkan jejak, bukan hilang diam-diam).
    """
    conn = _connect()
    row = conn.execute(
        "SELECT COUNT(*) as n FROM tier2_packages WHERE claude_verdict IS NULL"
    ).fetchone()
    conn.close()
    return row["n"]


if __name__ == "__main__":
    print("=" * 55)
    print("TEST: package_log.py")
    print("=" * 55)

    init_db()
    print(f"[OK] Tabel tier2_packages siap di: {DB_PATH}")

    # Simulasi paket minimal (struktur sesuai package_builder.py)
    fake_package = {
        "pair": "EURUSD", "symbol": "EURUSDm",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "session": "london",
        "bias": {"value": "bullish", "is_transitional": False,
                 "structure_phase": "kontinuitas"},
        "candidate_zone": {"zone_id": "test_zone_abc", "zone_type": "OB",
                            "direction": "bullish", "zone_top": 1.1050,
                            "zone_bottom": 1.1030, "current_price": 1.1042},
        "zone_history": {"is_revisit": False, "previous_verdict": None,
                          "previous_confidence": None,
                          "previous_reasoning": None, "touch_count": 0},
        "volume_context": {"ratio_vs_avg": 1.2},
        "liquidity_context": {"nearest_eqh": None, "nearest_eql": None},
        "fvg_context": [],
        "supporting_data": {"ema20": 1.1042, "ema50": 1.1035, "adx": 24.1},
        "charts": {"h1_chart_path": "/fake/h1.png", "m5_chart_path": "/fake/m5.png"},
        "other_zones_nearby": [],
    }

    log_id = save_package(fake_package)
    print(f"\n[1] Paket disimpan -> log_id={log_id} (verdict masih kosong)")

    pending = count_pending_responses()
    print(f"[2] Paket menunggu respons: {pending}")

    update_verdict(log_id, "execute", 8, "struktur kuat, reaksi M5 valid")
    print(f"[3] Verdict di-update (simulasi respons Claude)")

    history = get_package_history(pair="EURUSD", limit=5)
    print(f"\n[4] Riwayat EURUSD ({len(history)} baris terakhir):")
    for h in history:
        print(f"    id={h['id']} zone={h['zone_type']}/{h['direction']} "
              f"verdict={h['claude_verdict']} conf={h['claude_confidence']} "
              f"reasoning={h['claude_reasoning']}")

    pending_after = count_pending_responses()
    print(f"\n[5] Paket menunggu respons setelah update: {pending_after}")

    print("\n[SELESAI] package_log.py OK")
