"""Verifikasi cepat: cek apakah verdict Claude tadi benar-benar
tersimpan di package_log DAN zone_state dengan konsisten."""
import sqlite3
from pathlib import Path

DB_PATH = Path("data/trading_bot.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

print("=== package_log (tier2_packages), 3 baris terakhir ===")
rows = conn.execute("""
    SELECT id, pair, zone_type, direction, claude_verdict, 
           claude_confidence, claude_skip_reason_type, responded_at
    FROM tier2_packages ORDER BY id DESC LIMIT 3
""").fetchall()
for r in rows:
    print(dict(r))

print("\n=== zone_state, zona EURUSD OB terkait ===")
rows2 = conn.execute("""
    SELECT zone_id, zone_type, direction, skip_count, cooldown_until,
           last_verdict, last_confidence, last_reasoning
    FROM zone_state WHERE pair='EURUSD' AND zone_type='OB'
""").fetchall()
for r in rows2:
    print(dict(r))

conn.close()
