"""
Diagnostik cepat: cek PERSIS apa yang tersimpan di DB untuk OB EURUSD
saat ini, supaya kita tahu pasti apakah OB lebar itu tersimpan
SEBELUM atau SESUDAH filter baru diterapkan.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path("data/trading_bot.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT id, pair, direction, ob_top, ob_bottom, ob_width, ob_index,
           status, created_at
    FROM order_blocks
    WHERE pair='EURUSD'
    ORDER BY created_at DESC
""").fetchall()
conn.close()

print(f"Total baris OB EURUSD di DB: {len(rows)}\n")
for r in rows:
    print(f"id={r['id']:<4} status={r['status']:<10} dir={r['direction']:<8} "
          f"top={r['ob_top']:.5f} bottom={r['ob_bottom']:.5f} "
          f"width={r['ob_width']:.5f} created={r['created_at']}")
