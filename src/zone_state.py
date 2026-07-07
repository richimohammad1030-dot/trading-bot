"""
zone_state.py - Identitas Zona, Freshness, Debounce & Memori Penilaian

Modul BARU untuk Fase 1 (v2.0/v2.1/v2.2). Ini adalah "buku catatan" milik
Tier 1 -- murni mekanis, TIDAK PERNAH menilai kualitas zona. Tier 1 hanya:
  1. Mengenali "zona yang sama" lintas waktu (identitas via overlap area)
  2. Melacak touch_count (freshness) -- DATA untuk Tier 2, bukan filter
  3. Menyimpan state debounce (already_sent flag) agar tidak spam saat
     harga diam di dalam zona
  4. Menyimpan riwayat penilaian Tier 2 (verdict, confidence, reasoning)
     supaya kunjungan ulang bisa membawa riwayat (Opsi A, v2.1 Bab 5.2)
  5. Menyimpan cooldown escalating utk SKIP (v2.0 Bab 5.4, v2.2 Bab 1)

TIDAK ADA logika di modul ini yang menilai "zona ini bagus atau tidak".
Itu prinsip organ-otak yang tidak boleh dilanggar (v2.1 Bab 0.1).

Referensi desain:
- v2.0 Bab 5-6 (debounce, memori, anti-double-position)
- v2.1 Bab 1.4, 5.2 (skip_reason_type, memori Opsi A)
- v2.2 Bab 1 (throttle mini not_yet_formed)
"""

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import ROOT_DIR

# KONSISTEN dengan orderblock.py, risk_engine.py, news_filter.py, dll:
# SATU file trading_bot.db untuk semua modul (tabel berbeda per modul),
# bukan file terpisah per modul.
DB_PATH = ROOT_DIR / "data" / "trading_bot.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    # v2.3 Bab F: WAL mode untuk konkurensi baca/tulis (screener loop vs
    # evaluasi harian yang membaca ledger secara bersamaan). Aman
    # dipanggil berulang -- PRAGMA ini idempoten.

    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Buat tabel zone_state jika belum ada."""
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS zone_state (
            zone_id           TEXT PRIMARY KEY,
            pair              TEXT NOT NULL,
            zone_type         TEXT NOT NULL,   -- OB/FVG/SBR/RBS/SWING_HIGH/SWING_LOW/SD
            direction         TEXT NOT NULL,   -- bullish/bearish
            zone_top          REAL NOT NULL,
            zone_bottom       REAL NOT NULL,
            touch_count       INTEGER NOT NULL DEFAULT 0,
            already_sent      INTEGER NOT NULL DEFAULT 0,  -- state-flag debounce
            has_open_position INTEGER NOT NULL DEFAULT 0,  -- anti-double-position
            skip_count        INTEGER NOT NULL DEFAULT 0,  -- untuk cooldown escalating
            cooldown_until    TEXT,                        -- ISO datetime, NULL = tidak cooldown
            last_verdict      TEXT,                        -- execute/skip/NULL
            last_confidence   INTEGER,
            last_reasoning    TEXT,
            last_skip_reason_type TEXT,   -- not_yet_formed / setup_invalid (v2.1 Bab 5)
            throttle_until    TEXT,       -- ISO datetime, throttle mini not_yet_formed (v2.2)
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_zone_pair_type
        ON zone_state(pair, zone_type, direction)
    """)
    conn.commit()
    conn.close()


# =====================================================
# IDENTITAS ZONA (overlap >= 60%, v2.1 Bab 6.1)
# =====================================================
def _overlap_ratio(top_a, bottom_a, top_b, bottom_b) -> float:
    """Rasio overlap dua rentang [bottom, top] terhadap area yang lebih kecil."""
    lo = max(bottom_a, bottom_b)
    hi = min(top_a, top_b)
    if hi <= lo:
        return 0.0
    overlap = hi - lo
    smaller = min(top_a - bottom_a, top_b - bottom_b)
    if smaller <= 0:
        return 0.0
    return overlap / smaller


def _make_zone_id(pair: str, zone_type: str, direction: str, top: float, bottom: float) -> str:
    """ID awal (dipakai hanya jika benar-benar zona baru, belum ada match)."""
    raw = f"{pair}|{zone_type}|{direction}|{round((top+bottom)/2, 5)}|{datetime.now(timezone.utc).isoformat()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def find_or_create_zone(pair: str, zone_type: str, direction: str,
                         top: float, bottom: float,
                         overlap_threshold: float = 0.60) -> dict:
    """
    Cari zona yang SAMA (overlap >= threshold) untuk pair+type+direction ini.
    Jika ketemu -> return record lama (riwayat menyambung, v2.1 Bab 5.2).
    Jika tidak  -> buat record baru (riwayat kosong).

    Ini murni pencocokan geometris mekanis -- bukan penilaian.
    """
    conn = _connect()
    rows = conn.execute("""
        SELECT * FROM zone_state WHERE pair=? AND zone_type=? AND direction=?
    """, (pair, zone_type, direction)).fetchall()

    best_match = None
    best_ratio = 0.0
    for row in rows:
        ratio = _overlap_ratio(top, bottom, row["zone_top"], row["zone_bottom"])
        if ratio >= overlap_threshold and ratio > best_ratio:
            best_match = row
            best_ratio = ratio

    now = datetime.now(timezone.utc).isoformat()

    if best_match is not None:
        # Zona sama -> update batas (boleh sedikit bergeser antar deteksi)
        conn.execute("""
            UPDATE zone_state SET zone_top=?, zone_bottom=?, updated_at=?
            WHERE zone_id=?
        """, (top, bottom, now, best_match["zone_id"]))
        conn.commit()
        result = dict(best_match)
        result["zone_top"], result["zone_bottom"] = top, bottom
        result["is_new"] = False
    else:
        zone_id = _make_zone_id(pair, zone_type, direction, top, bottom)
        conn.execute("""
            INSERT INTO zone_state
                (zone_id, pair, zone_type, direction, zone_top, zone_bottom,
                 touch_count, already_sent, has_open_position, skip_count,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?)
        """, (zone_id, pair, zone_type, direction, top, bottom, now, now))
        conn.commit()
        result = {
            "zone_id": zone_id, "pair": pair, "zone_type": zone_type,
            "direction": direction, "zone_top": top, "zone_bottom": bottom,
            "touch_count": 0, "already_sent": 0, "has_open_position": 0,
            "skip_count": 0, "cooldown_until": None, "last_verdict": None,
            "last_confidence": None, "last_reasoning": None,
            "last_skip_reason_type": None, "throttle_until": None,
            "is_new": True,
        }
    conn.close()
    return result


# =====================================================
# GATE STATE (harga masuk/keluar zona) -- v2.0 Bab 5.1
# =====================================================
def mark_sent(zone_id: str):
    """Set already_sent=1 setelah paket dikirim ke Tier 2."""
    conn = _connect()
    conn.execute("UPDATE zone_state SET already_sent=1, updated_at=? WHERE zone_id=?",
                 (datetime.now(timezone.utc).isoformat(), zone_id))
    conn.commit()
    conn.close()


def reset_sent_flag(zone_id: str):
    """
    Reset already_sent=0 saat harga KELUAR zona melewati ambang (v2.1 Bab 5.3).
    Juga menaikkan touch_count -- kunjungan baru = sentuhan baru (freshness
    berkurang, tapi ini DATA, bukan filter -- lihat catatan modul).
    """
    conn = _connect()
    conn.execute("""
        UPDATE zone_state
        SET already_sent=0, touch_count=touch_count+1, updated_at=?
        WHERE zone_id=?
    """, (datetime.now(timezone.utc).isoformat(), zone_id))
    conn.commit()
    conn.close()


def is_price_beyond_exit_threshold(zone: dict, current_price: float,
                                    atr_val: float,
                                    exit_multiplier: float = 0.5) -> bool:
    """
    Ambang keluar zona = fungsi lebar zona (v2.1 Bab 5.3):
    harga dianggap "keluar" jika berjarak >= exit_multiplier * lebar_zona
    ATAU >= exit_multiplier * ATR dari batas zona (mana yang lebih besar,
    supaya zona sangat tipis tetap punya ambang wajar).
    """
    zone_width = zone["zone_top"] - zone["zone_bottom"]
    threshold = max(zone_width, atr_val) * exit_multiplier

    if current_price > zone["zone_top"]:
        return (current_price - zone["zone_top"]) >= threshold
    if current_price < zone["zone_bottom"]:
        return (zone["zone_bottom"] - current_price) >= threshold
    return False  # masih di dalam zona -> jelas belum keluar


# =====================================================
# ANTI-DOUBLE-POSITION (per-zona, v2.0 Bab 6)
# =====================================================
def set_open_position(zone_id: str, has_position: bool):
    conn = _connect()
    conn.execute("UPDATE zone_state SET has_open_position=?, updated_at=? WHERE zone_id=?",
                 (1 if has_position else 0, datetime.now(timezone.utc).isoformat(), zone_id))
    conn.commit()
    conn.close()


# =====================================================
# COOLDOWN & THROTTLE (v2.0 Bab 5.4, v2.2 Bab 1)
# =====================================================
def apply_skip_cooldown(zone_id: str, skip_reason_type: str,
                         verdict: str, confidence: int, reasoning: str):
    """
    Terapkan cooldown MEKANIS berdasarkan skip_reason_type dari Tier 2.
    Tier 1 hanya membaca flag ini dan menjalankan timer -- keputusan
    kualitas tetap 100% milik Tier 2 (v2.1 Bab 5, v2.2 Bab 1.4).

    not_yet_formed  -> throttle mini 2 candle M5 (10 menit)  [v2.2]
    setup_invalid   -> cooldown escalating 1 -> 3 candle H1 -> sampai
                       mitigasi/BOS baru [v2.0 Bab 5.4]
    """
    conn = _connect()
    now = datetime.now(timezone.utc)
    row = conn.execute("SELECT skip_count FROM zone_state WHERE zone_id=?",
                        (zone_id,)).fetchone()
    skip_count = (row["skip_count"] if row else 0) + 1

    cooldown_until = None
    throttle_until = None

    if skip_reason_type == "not_yet_formed":
        # Throttle mini -- TIDAK menambah skip_count "resmi" (bukan
        # penolakan setup, hanya belum matang) -- v2.2 Bab 1.3
        throttle_until = (now + timedelta(minutes=10)).isoformat()
        skip_count -= 1  # batalkan increment di atas untuk kasus ini
        skip_count = max(skip_count, 0)
    elif skip_reason_type == "setup_invalid":
        if skip_count <= 1:
            cooldown_until = (now + timedelta(hours=1)).isoformat()
        elif skip_count == 2:
            cooldown_until = (now + timedelta(hours=3)).isoformat()
        else:
            # skip ke-3+: sampai mitigasi/struktur berubah -> cooldown
            # sangat panjang, akan direset manual oleh reset_cooldown_on_new_bos()
            cooldown_until = (now + timedelta(days=3650)).isoformat()

    conn.execute("""
        UPDATE zone_state
        SET skip_count=?, cooldown_until=?, throttle_until=?,
            last_verdict=?, last_confidence=?, last_reasoning=?,
            last_skip_reason_type=?, already_sent=0, updated_at=?
        WHERE zone_id=?
    """, (skip_count, cooldown_until, throttle_until, verdict, confidence,
          reasoning, skip_reason_type, now.isoformat(), zone_id))
    conn.commit()
    conn.close()


def record_execute_verdict(zone_id: str, confidence: int, reasoning: str):
    """Catat verdict execute (tanpa cooldown -- posisi akan dibuka)."""
    conn = _connect()
    conn.execute("""
        UPDATE zone_state
        SET last_verdict='execute', last_confidence=?, last_reasoning=?,
            last_skip_reason_type=NULL, updated_at=?
        WHERE zone_id=?
    """, (confidence, reasoning, datetime.now(timezone.utc).isoformat(), zone_id))
    conn.commit()
    conn.close()


def reset_cooldown_on_new_structure(pair: str):
    """
    Reset cooldown SEMUA zona di pair ini saat ada BOS/CHoCH baru
    terdeteksi (v2.0 Bab 5.4: "kondisi berubah, layak dinilai ulang").
    Dipanggil dari screener.py setiap kali fase struktur berubah.
    """
    conn = _connect()
    conn.execute("""
        UPDATE zone_state SET cooldown_until=NULL, throttle_until=NULL,
        skip_count=0, updated_at=? WHERE pair=?
    """, (datetime.now(timezone.utc).isoformat(), pair))
    conn.commit()
    conn.close()


def is_blocked_by_cooldown(zone: dict) -> bool:
    """True jika zona masih dalam cooldown ATAU throttle mini."""
    now = datetime.now(timezone.utc).isoformat()
    cd = zone.get("cooldown_until")
    th = zone.get("throttle_until")
    if cd and cd > now:
        return True
    if th and th > now:
        return True
    return False


if __name__ == "__main__":
    print("=" * 55)
    print("TEST: zone_state.py")
    print("=" * 55)

    init_db()
    print(f"[OK] DB siap di: {DB_PATH}")

    # Simulasi: zona OB EURUSD terdeteksi pertama kali
    z1 = find_or_create_zone("EURUSDm", "OB", "bullish", top=1.08150, bottom=1.08000)
    print(f"\n[1] Zona baru: is_new={z1['is_new']} zone_id={z1['zone_id']}")

    # Simulasi: deteksi ulang, batas sedikit bergeser (overlap tinggi)
    z2 = find_or_create_zone("EURUSDm", "OB", "bullish", top=1.08155, bottom=1.08005)
    print(f"[2] Deteksi ulang (overlap): is_new={z2['is_new']} zone_id={z2['zone_id']} "
          f"(harus SAMA dengan [1])")

    # Simulasi: gate terpicu, kirim ke Tier 2
    mark_sent(z1["zone_id"])
    print("[3] Sudah dikirim -> already_sent=1")

    # Simulasi: Tier 2 SKIP not_yet_formed
    apply_skip_cooldown(z1["zone_id"], "not_yet_formed", "skip", 4, "reaksi belum matang")
    print("[4] SKIP not_yet_formed -> throttle mini diterapkan")

    conn = _connect()
    row = conn.execute("SELECT * FROM zone_state WHERE zone_id=?", (z1["zone_id"],)).fetchone()
    print(f"    throttle_until={row['throttle_until']}  skip_count={row['skip_count']}")
    conn.close()

    print("\n[SELESAI] zone_state.py OK")
