"""
paper_outcome_tracker.py - Fase 5 (prasyarat Learning Ledger)

Simulasi outcome (TP/SL) untuk verdict "execute" dari Task 5 SELAMA
paper mode (config.execution.enabled=false, Fase 4/5) -- karena
trade_log (risk_engine.py) HANYA terisi saat execution.enabled=true
(Fase 6+), Task 6/7 (daily_evaluation/weekly_evaluation) tidak akan
pernah punya data hasil trade tanpa modul ini.

METODOLOGI (riset, referensi di bawah):
- Cek historis pakai candle M1 (paling granular yang tersedia dari
  mt5_connector.get_candles()), jalan maju candle demi candle dari
  entry_time_utc.
- Anti-repaint WAJIB: HANYA candle M1 yang sudah CLOSE yang dicek --
  df.iloc[:-1] (konsisten dengan aturan project di 05_session_guide.md).
- "Same-bar ambiguity": kalau SL & TP sama-sama tertembus di SATU
  candle M1, urutan kejadian tidak bisa dipastikan dari data OHLC
  saja. Diambil asumsi PALING KONSERVATIF -- anggap SL kena duluan,
  bukan condong ke sisi profit. Konsisten dengan ATURAN UMUM #3 di
  system_prompt.md ("asumsi paling konservatif").
- SL/TP STATIS (TIDAK simulasikan breakeven/trailing) -- sesuai
  keputusan eksplisit: manajemen exit dinamis adalah wewenang
  Tier 2, modul observasional ini memakai sl_price/tp_price dari
  Task 5 apa adanya tanpa modifikasi.

Referensi metodologi same-bar ambiguity:
- Claeys, M. (2026) "When Backtests Guess: How Trading Platforms
  Silently Fabricate Results", SSRN 6240638 -- mengukur ambiguitas
  OHLC same-bar (~18% bar pada setup SL/TP sempit), merekomendasikan
  granularitas lebih halus + asumsi konservatif saat tetap ambigu.
- kernc/backtesting.py, GitHub discussion #989 & issue #1224 --
  konfirmasi praktik umum: urutan TP/SL dalam satu bar OHLC tidak
  bisa dipastikan tanpa data tick, harus diputuskan eksplisit
  (bukan default optimis ke sisi profit).

REVISI (menambah risk_pct & pnl_pct): formula expectancy_pct Task 7
di system_prompt.md butuh avg_win_pct/avg_loss_pct (persentase
saldo per trade), bukan cuma R-multiple. risk_pct SUDAH dihitung
tier2_orchestrator.py (dict `gate`) di paper mode juga -- cuma belum
pernah disimpan. Modul ini sekarang menerima risk_pct dari pemanggil
(diisi saat integrasi ke tier2_orchestrator.py, Step 5) dan menghitung
pnl_pct = risk_pct * 100 * rr_achieved otomatis saat trade closed.

STATUS INTEGRASI: modul ini BELUM terhubung otomatis ke
tier2_orchestrator.py (butuh tambahan panggilan record_pending_trade()
setelah verdict "execute" dicatat, dengan risk_pct dari `gate['risk_pct']`
dan entry_time_utc dari JAM SERVER BROKER MT5 -- bukan
package["timestamp_utc"] yang UTC asli, lihat catatan di
_check_single_trade()). tier2_orchestrator.py adalah file inti
orkestrasi -- MINTA KONFIRMASI dulu sebelum diedit.
"""

import sys
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import load_config, ROOT_DIR
import mt5_connector as mt5c

DB_PATH = ROOT_DIR / "data" / "trading_bot.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Buat tabel paper_trade_outcomes jika belum ada."""
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_trade_outcomes (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            log_id                  INTEGER NOT NULL,
            pair                    TEXT NOT NULL,
            symbol                  TEXT NOT NULL,
            direction               TEXT NOT NULL,
            entry_price             REAL NOT NULL,
            sl_price                REAL NOT NULL,
            tp_price                REAL NOT NULL,
            risk_pct                REAL,
            entry_time_utc          TEXT NOT NULL,
            status                  TEXT NOT NULL DEFAULT 'pending',
            result                  TEXT,
            closed_price            REAL,
            closed_time_utc         TEXT,
            rr_achieved             REAL,
            pnl_pct                 REAL,
            last_checked_time_utc   TEXT,
            created_at              TEXT DEFAULT (datetime('now'))
        )
    """)
    # ALTER TABLE guard -- kalau tabel sudah ada dari versi SEBELUM
    # revisi ini (tanpa kolom risk_pct/pnl_pct), tambahkan kolomnya
    # tanpa menghapus data yang sudah ada.
    existing_cols = {row["name"] for row in
                      conn.execute("PRAGMA table_info(paper_trade_outcomes)")}
    if "risk_pct" not in existing_cols:
        conn.execute("ALTER TABLE paper_trade_outcomes ADD COLUMN risk_pct REAL")
    if "pnl_pct" not in existing_cols:
        conn.execute("ALTER TABLE paper_trade_outcomes ADD COLUMN pnl_pct REAL")
    conn.commit()
    conn.close()


def record_pending_trade(log_id: int, pair: str, symbol: str, direction: str,
                          entry_price: float, sl_price: float, tp_price: float,
                          risk_pct: float, entry_time_utc: str = None) -> int:
    """
    Catat trade paper baru sebagai 'pending'.

    Dipanggil SETELAH verdict "execute" dicatat di tier2_orchestrator.py
    (integrasi otomatis BELUM dipasang -- lihat catatan modul di atas).

    Args:
        log_id: id dari tier2_packages (package_log.py) -- link ke
                paket & verdict asal trade ini.
        risk_pct: FRAKSI (0.01 = 1%), sama konvensi dengan gate['risk_pct']
                  di tier2_orchestrator.py. Dipakai hitung pnl_pct saat
                  trade closed.
        entry_time_utc: WAJIB pakai jam server broker MT5 (sumber yang
                sama dengan df["time"] di mt5_connector.get_candles()),
                BUKAN datetime.now(timezone.utc) atau package["timestamp_utc"]
                -- keduanya beda beberapa jam dari jam server broker
                dan akan membuat window pengecekan candle M1 offset.
                Default None -> pakai waktu sekarang (UTC asli) HANYA
                untuk kasus test/manual, bukan untuk integrasi live.

    Return: id baris baru di paper_trade_outcomes.
    """
    if entry_time_utc is None:
        entry_time_utc = datetime.now(timezone.utc).isoformat()

    conn = _connect()
    cur = conn.execute("""
        INSERT INTO paper_trade_outcomes
            (log_id, pair, symbol, direction, entry_price, sl_price,
             tp_price, risk_pct, entry_time_utc, status)
        VALUES (?,?,?,?,?,?,?,?,?,'pending')
    """, (log_id, pair, symbol, direction, entry_price, sl_price,
          tp_price, risk_pct, entry_time_utc))
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def _check_single_trade(row: sqlite3.Row) -> dict:
    """
    Cek satu trade 'pending' terhadap candle M1 sejak checkpoint
    terakhir (last_checked_time_utc, atau entry_time_utc kalau baru
    pertama kali dicek).

    PENTING: entry_time_utc di baris DB harus dalam domain jam yang
    SAMA dengan df["time"] dari mt5_connector.get_candles() (jam
    server broker MT5) -- lihat catatan di record_pending_trade().

    Return:
        None                         -> belum ada candle M1 baru yang
                                         closed sejak terakhir dicek.
        {"status": "pending", ...}   -> sudah dicek, belum kena SL/TP,
                                         checkpoint diupdate.
        {"status": "closed", ...}    -> SL atau TP kena, detail terisi.
    """
    symbol = row["symbol"]
    direction = row["direction"]
    sl_price = row["sl_price"]
    tp_price = row["tp_price"]

    # count besar supaya cover dari entry_time sampai sekarang meski
    # entry sudah beberapa hari lalu (mt5.copy_rates_from_pos ambil N
    # candle TERAKHIR, bukan by-date-range).
    df = mt5c.get_candles(symbol, "M1", count=10000)

    # Anti-repaint WAJIB: buang candle terakhir (masih berjalan, belum close)
    df_closed = df.iloc[:-1]

    start_from = row["last_checked_time_utc"] or row["entry_time_utc"]
    start_ts = pd.Timestamp(start_from)
    if start_ts.tzinfo is not None:
        start_ts = start_ts.tz_localize(None)

    window = df_closed[df_closed["time"] > start_ts]

    if len(window) == 0:
        return None

    is_bullish = (direction == "bullish")

    for _, candle in window.iterrows():
        hi = float(candle["high"])
        lo = float(candle["low"])

        if is_bullish:
            hit_tp = hi >= tp_price
            hit_sl = lo <= sl_price
        else:
            hit_tp = lo <= tp_price
            hit_sl = hi >= sl_price

        if hit_tp and hit_sl:
            # Same-bar ambiguity -- KONSERVATIF: anggap SL duluan
            # (lihat referensi metodologi di docstring modul).
            result = "SL"
            closed_price = sl_price
        elif hit_sl:
            result = "SL"
            closed_price = sl_price
        elif hit_tp:
            result = "TP"
            closed_price = tp_price
        else:
            continue  # candle ini tidak menyentuh SL maupun TP

        entry_price = row["entry_price"]
        sl_distance = abs(entry_price - sl_price)
        if sl_distance == 0:
            rr_achieved = 0.0
        else:
            price_move = (closed_price - entry_price) if is_bullish \
                else (entry_price - closed_price)
            rr_achieved = round(price_move / sl_distance, 3)

        risk_pct = row["risk_pct"]
        pnl_pct = round(risk_pct * 100 * rr_achieved, 4) if risk_pct is not None else None

        return {
            "status": "closed",
            "result": result,
            "closed_price": closed_price,
            "closed_time_utc": str(candle["time"]),
            "rr_achieved": rr_achieved,
            "pnl_pct": pnl_pct,
            "last_checked_time_utc": str(candle["time"]),
        }

    # Semua candle baru sudah dicek, belum ada yang kena SL/TP --
    # tetap pending, checkpoint dimajukan supaya run berikutnya
    # tidak scan ulang dari nol.
    last_time = str(window["time"].iloc[-1])
    return {
        "status": "pending",
        "last_checked_time_utc": last_time,
    }


def run_outcome_check() -> dict:
    """
    Jalankan pengecekan untuk SEMUA trade paper berstatus 'pending'.

    Return ringkasan:
    {"checked": int, "closed_tp": int, "closed_sl": int,
     "still_pending": int, "no_new_data": int, "errors": [...]}
    """
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM paper_trade_outcomes WHERE status='pending'"
    ).fetchall()

    summary = {"checked": 0, "closed_tp": 0, "closed_sl": 0,
               "still_pending": 0, "no_new_data": 0, "errors": []}

    for row in rows:
        summary["checked"] += 1
        try:
            update = _check_single_trade(row)
        except Exception as e:
            # Satu trade gagal dicek TIDAK BOLEH menjatuhkan trade
            # lain (pola sama dengan run_screener_cycle() di
            # screener.py) -- tapi kegagalan HARUS tercatat, bukan
            # ditelan diam-diam.
            summary["errors"].append({"id": row["id"], "error": str(e)})
            continue

        if update is None:
            summary["no_new_data"] += 1
            continue

        if update["status"] == "closed":
            conn.execute("""
                UPDATE paper_trade_outcomes
                SET status=?, result=?, closed_price=?, closed_time_utc=?,
                    rr_achieved=?, pnl_pct=?, last_checked_time_utc=?
                WHERE id=?
            """, (update["status"], update["result"], update["closed_price"],
                  update["closed_time_utc"], update["rr_achieved"],
                  update["pnl_pct"], update["last_checked_time_utc"], row["id"]))
            if update["result"] == "TP":
                summary["closed_tp"] += 1
            else:
                summary["closed_sl"] += 1
        else:
            conn.execute("""
                UPDATE paper_trade_outcomes
                SET last_checked_time_utc=?
                WHERE id=?
            """, (update["last_checked_time_utc"], row["id"]))
            summary["still_pending"] += 1

    conn.commit()
    conn.close()
    return summary


def get_outcomes(pair: str = None, status: str = None, limit: int = 100) -> list:
    """
    Ambil riwayat outcome -- fondasi query untuk daily_evaluation.py /
    weekly_evaluation.py (Step 7 & 9, belum dibuat).
    """
    conn = _connect()
    query = "SELECT * FROM paper_trade_outcomes WHERE 1=1"
    params = []
    if pair:
        query += " AND pair=?"
        params.append(pair)
    if status:
        query += " AND status=?"
        params.append(status)
    query += " ORDER BY entry_time_utc DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    print("=" * 60)
    print("TEST: paper_outcome_tracker.py (Fase 5 -- prasyarat Learning Ledger)")
    print("=" * 60)

    init_db()
    print("[OK] init_db() -- tabel paper_trade_outcomes siap (kolom risk_pct/pnl_pct ada).")

    if not mt5c.connect():
        sys.exit(1)

    config = load_config()
    symbol = mt5c.resolve_symbol("EURUSD", config)
    df_now = mt5c.get_candles(symbol, "M1", count=200)
    ref_price = float(df_now["close"].iloc[-2])
    entry_time_test = str(df_now["time"].iloc[-150])  # ~150 menit lalu

    print(f"\n--- TEST 1: record_pending_trade() (dengan risk_pct) ---")
    print(f"  symbol={symbol} ref_price={ref_price:.5f} entry_time={entry_time_test}")

    # SL/TP sengaja LEBAR -- test ini murni memverifikasi MEKANISME
    # jalan tanpa error, bukan menebak arah pasar yang pasti.
    test_id = record_pending_trade(
        log_id=999999,  # dummy, tidak ada FK constraint nyata di skema ini
        pair="EURUSD", symbol=symbol, direction="bullish",
        entry_price=ref_price,
        sl_price=round(ref_price - 0.01000, 5),
        tp_price=round(ref_price + 0.01000, 5),
        risk_pct=0.01,  # dummy 1%, sesuai konvensi gate['risk_pct']
        entry_time_utc=entry_time_test,
    )
    print(f"  [OK] Tercatat sebagai id={test_id}, status=pending, risk_pct=0.01")

    print(f"\n--- TEST 2: run_outcome_check() ---")
    result = run_outcome_check()
    print(f"  Ringkasan: {result}")

    print(f"\n--- TEST 3: get_outcomes() ---")
    outcomes = get_outcomes(pair="EURUSD", limit=5)
    for o in outcomes:
        print(f"  id={o['id']} status={o['status']} result={o['result']} "
              f"rr_achieved={o['rr_achieved']} pnl_pct={o['pnl_pct']}")

    # Bersihkan baris test -- log_id dummy (999999) bukan trade nyata,
    # jangan sampai ikut teragregasi nanti oleh daily/weekly_evaluation.py.
    conn = _connect()
    conn.execute("DELETE FROM paper_trade_outcomes WHERE id=?", (test_id,))
    conn.commit()
    conn.close()
    print(f"  [CLEANUP] Baris test id={test_id} dihapus.")

    mt5c.shutdown()
    print("\n[SELESAI] paper_outcome_tracker.py -- mekanisme + risk_pct/pnl_pct tervalidasi.")
