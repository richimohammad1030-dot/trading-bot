"""
risk_engine.py - Step 9

Position sizing + risk management sesuai blueprint v16 Bagian 11.

Referensi:
- Formula lot size: MQL5 forum (mql5.com/en/forum/346363) + 
  iamforextrader.com/en/tools/position-size-calculator
  Menggunakan mt5.symbol_info().trade_tick_value dan trade_tick_size
  yang sudah dalam account currency (universal, tidak perlu konversi)
- Struktur risk counter: linuzri/mt5-trading (risk/sizing.py + limits.py)
  SQLite persistence untuk restart-safe counter
- Daily/weekly/monthly circuit breaker: 
  Astralchemist/Expert-Advisor-trading-bot + xPOURY4/ML-SuperTrend-MT5

BLUEPRINT v16 Bagian 11:
- Group A (EUR/GBP/AUD): 1 pair=1%, 2 pair=0.5%, 3 pair=0.33%
- Group B (JPY): 1% independen
- XAU: 1% independen
- Pre-entry universal: total_risk_terbuka + floating_loss + risk_baru ≤ 2%
- Daily: 2%, Weekly: 10%, Monthly: 20% (rolling 30 hari)
- Resume priority: Monthly > Weekly > Daily
- BE = 0% counter, level blacklist
- SL = -loss counter, OB mitigated

REVISI (Step 5, perbaikan normalisasi pip/point, Addendum v2.3 Bab 3.6):
is_level_blacklisted() SEBELUMNYA hardcode tolerance=0.0001 langsung di
signature -- ditemukan saat audit sebagai implementasi pip KE-5 yang tidak
tercatat di TEMUAN_normalisasi_pip_point.md awal (baru ketahuan saat
Step 0 grep langsung ke file ini). SALAH untuk XAUUSD (0.0001 nyaris nol
dibanding harga ~$4000) dan SALAH untuk USDJPY (3-digit, 1 pip riil =
0.01, bukan 0.0001). Sekarang default tolerance diturunkan dari
mt5_connector.pips_to_price(1, pair) -- "level dianggap sama jika dalam
radius 1 pip RIIL symbol tsb", bukan angka tetap. calculate_lot() TIDAK
diubah -- sudah benar sejak awal (pakai trade_tick_size/trade_tick_value
langsung dari symbol_info, referensi kanonik yang justru dipakai sebagai
acuan desain fungsi pip terpusat di mt5_connector.py).

REVISI (Fase 6, Step 3 -- ditambah, bukan diubah):
Kolom `mt5_ticket` ditambahkan ke trade_log (ALTER TABLE guard, aman
terhadap data lama) supaya posisi MT5 sungguhan bisa dicocokkan balik
saat modifikasi SL (breakeven) dan deteksi closure
(position_monitor.py, dipakai mt5.positions_get()/history_deals_get()
dengan ticket ini sebagai kunci -- referensi resmi MQL5: deal.reason
DEAL_REASON_SL/DEAL_REASON_TP untuk klasifikasi hasil closure akurat,
BUKAN tebak dari harga seperti paper_outcome_tracker.py Fase 5).
log_trade_open() sekarang menerima mt5_ticket (opsional, default
None -- tetap kompatibel untuk pemanggilan lama/paper-mode kalau ada).
Fungsi baru get_open_trades() ditambahkan untuk kebutuhan
position_monitor.py. TIDAK ADA logika risk/lot/circuit-breaker yang
diubah.
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import MetaTrader5 as mt5

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import load_config, ROOT_DIR
import mt5_connector as mt5c

DB_PATH = ROOT_DIR / "data" / "trading_bot.db"

# Group mapping sesuai blueprint
GROUPS = {
    "EURUSD": "A", "GBPUSD": "A", "AUDUSD": "A",
    "USDJPY": "B",
    "XAUUSD": "independent",
}


# =====================================================
# DATABASE INIT (risk tables)
# =====================================================
def init_risk_db():
    """Inisialisasi tabel risk counter dan blacklist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Risk counters (daily, weekly, monthly)
    c.execute("""
        CREATE TABLE IF NOT EXISTS risk_counters (
            id           INTEGER PRIMARY KEY,
            daily_loss   REAL DEFAULT 0.0,
            weekly_loss  REAL DEFAULT 0.0,
            monthly_loss REAL DEFAULT 0.0,
            daily_date   TEXT,
            weekly_start TEXT,
            monthly_start TEXT,
            updated_at   TEXT DEFAULT (datetime('now'))
        )
    """)
    # Buat 1 row jika belum ada
    c.execute("INSERT OR IGNORE INTO risk_counters (id) VALUES (1)")

    # Level blacklist (re-entry block)
    c.execute("""
        CREATE TABLE IF NOT EXISTS level_blacklist (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            pair        TEXT NOT NULL,
            level_price REAL NOT NULL,
            blacklist_date TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    # Trade log (untuk menghitung exposure yang sudah terbuka)
    c.execute("""
        CREATE TABLE IF NOT EXISTS trade_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            pair         TEXT NOT NULL,
            direction    TEXT NOT NULL,
            entry_price  REAL NOT NULL,
            sl_price     REAL NOT NULL,
            tp_price     REAL NOT NULL,
            lot          REAL NOT NULL,
            risk_pct     REAL NOT NULL,
            status       TEXT DEFAULT 'open',
            result       TEXT,
            pnl_pct      REAL DEFAULT 0.0,
            ob_id        INTEGER,
            opened_at    TEXT DEFAULT (datetime('now')),
            closed_at    TEXT
        )
    """)

    # --- Fase 6, Step 3: ALTER TABLE guard -- tambah mt5_ticket tanpa
    # menyentuh/menghapus data lama kalau tabel sudah ada dari sebelumnya.
    existing_cols = {row[1] for row in
                      c.execute("PRAGMA table_info(trade_log)").fetchall()}
    if "mt5_ticket" not in existing_cols:
        c.execute("ALTER TABLE trade_log ADD COLUMN mt5_ticket INTEGER")

    conn.commit()
    conn.close()


# =====================================================
# LOT SIZE CALCULATION
# Referensi: MQL5 forum mql5.com/en/forum/346363
# Formula: (balance × risk_pct) / (ticks_at_risk × tick_value)
# trade_tick_value sudah dalam account currency (universal)
#
# CATATAN Step 5: fungsi ini TIDAK diubah -- sudah benar sejak awal.
# Riset lot sizing cent vs standard (forum MQL5) mengonfirmasi
# trade_tick_value SUDAH dihitung MT5 dalam mata uang deposit akun
# (otomatis konsisten cent/standard, tidak perlu konversi manual).
# Ini justru jadi ACUAN desain fungsi pip terpusat mt5_connector.py.
# =====================================================
def calculate_lot(symbol: str, entry_price: float,
                   sl_price: float, risk_pct: float,
                   config: dict) -> float:
    """
    Hitung lot berdasarkan risk percentage dari balance saat ini.

    Menggunakan formula dari MQL5 forum (teruji untuk semua pair):
    ticks_at_risk = abs(entry - sl) / tick_size
    lot = (balance × risk_pct) / (ticks_at_risk × tick_value)

    Hasilnya di-clamp ke min/max volume dan dibulatkan ke volume_step.

    Args:
        symbol: nama symbol di broker (misal EURUSDm)
        entry_price: harga entry
        sl_price: harga stop loss
        risk_pct: persentase risk (0.01 = 1%)
        config: config dict

    Return: lot size (float, sudah normalized)
    """
    acc = mt5.account_info()
    sym = mt5.symbol_info(symbol)

    if acc is None or sym is None:
        return sym.volume_min if sym else 0.01

    balance = acc.balance

    # Untuk cent account, balance sudah dalam USC (cents)
    # tick_value juga sudah dalam USC → konsisten, tidak perlu konversi

    tick_size  = sym.trade_tick_size   # misal 0.00001 untuk EURUSD
    tick_value = sym.trade_tick_value  # nilai per tick per 1 lot (dalam akun currency)

    sl_distance = abs(entry_price - sl_price)
    if sl_distance < tick_size:
        sl_distance = tick_size  # minimal 1 tick

    ticks_at_risk = sl_distance / tick_size
    risk_amount   = balance * risk_pct

    if ticks_at_risk <= 0 or tick_value <= 0:
        return sym.volume_min

    raw_lot = risk_amount / (ticks_at_risk * tick_value)

    # Normalize ke volume_step, min, max
    step = sym.volume_step
    raw_lot = round(raw_lot / step) * step
    lot = max(sym.volume_min, min(sym.volume_max, raw_lot))

    return round(lot, 2)


def get_risk_pct_for_group(pair: str, config: dict,
                             open_group_a_count: int = 0) -> float:
    """
    Tentukan risk % sesuai group dan jumlah pair Group A yang terbuka.

    Blueprint Bagian 11.1:
    - Group A: 1 pair=1%, 2 pair=0.5%, 3 pair=0.33%
    - Group B: 1% independen
    - XAU: 1% independen
    """
    base = config["risk"]["per_trade_pct"] / 100.0
    dual = config["risk"]["per_trade_dual_pct"] / 100.0
    triple = config["risk"]["per_trade_triple_pct"] / 100.0

    group = GROUPS.get(pair.replace("m", "").replace(".raw", ""), "independent")

    if group == "A":
        # open_group_a_count = jumlah Group A yang SUDAH terbuka
        # (belum termasuk trade baru ini)
        if open_group_a_count == 0:
            return base    # 1%
        elif open_group_a_count == 1:
            return dual    # 0.5%
        else:
            return triple  # 0.33%
    else:
        return base  # Group B dan XAU: selalu 1%


# =====================================================
# PRE-ENTRY CHECK (universal)
# Blueprint: total_risk_terbuka + floating_loss + risk_baru ≤ 2%
# =====================================================
def pre_entry_check(new_risk_pct: float, config: dict) -> dict:
    """
    Cek apakah boleh entry berdasarkan total exposure.

    Formula blueprint:
    total_risk_terbuka + floating_loss + risk_baru ≤ 2%

    Return: dict {
        "allowed": bool,
        "total_risk_open": float,
        "floating_loss_pct": float,
        "new_risk_pct": float,
        "total_projected": float,
        "max_allowed": float,
    }
    """
    max_exposure = config["risk"]["max_total_exposure_pct"] / 100.0

    # Hitung total risk dari posisi terbuka (dari trade_log)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT SUM(risk_pct) FROM trade_log WHERE status='open'")
    row = c.fetchone()
    conn.close()
    total_risk_open = float(row[0]) if row[0] else 0.0

    # Hitung floating loss dari MT5
    acc = mt5.account_info()
    floating_loss_pct = 0.0
    if acc:
        positions = mt5.positions_get()
        if positions:
            total_floating_loss = sum(
                p.profit for p in positions if p.profit < 0
            )
            if acc.balance > 0:
                floating_loss_pct = abs(total_floating_loss) / acc.balance

    total_projected = total_risk_open + floating_loss_pct + new_risk_pct

    return {
        "allowed": total_projected <= max_exposure,
        "total_risk_open": round(total_risk_open * 100, 2),
        "floating_loss_pct": round(floating_loss_pct * 100, 2),
        "new_risk_pct": round(new_risk_pct * 100, 2),
        "total_projected": round(total_projected * 100, 2),
        "max_allowed": round(max_exposure * 100, 2),
    }


# =====================================================
# RISK COUNTERS (SQLite, restart-safe)
# Referensi: linuzri/mt5-trading risk/limits.py
# =====================================================
def _get_counters() -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT daily_loss, weekly_loss, monthly_loss,
               daily_date, weekly_start, monthly_start
        FROM risk_counters WHERE id=1
    """)
    row = c.fetchone()
    conn.close()
    if not row:
        return {"daily_loss": 0.0, "weekly_loss": 0.0,
                "monthly_loss": 0.0, "daily_date": None,
                "weekly_start": None, "monthly_start": None}
    return {
        "daily_loss": row[0] or 0.0,
        "weekly_loss": row[1] or 0.0,
        "monthly_loss": row[2] or 0.0,
        "daily_date": row[3],
        "weekly_start": row[4],
        "monthly_start": row[5],
    }


def _reset_counters_if_period_changed(counters: dict) -> dict:
    """Reset counter jika periode sudah berganti."""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    # Weekly: Senin = weekday 0
    monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")

    changed = False

    # Daily reset
    if counters["daily_date"] != today:
        counters["daily_loss"] = 0.0
        counters["daily_date"] = today
        changed = True

    # Weekly reset (setiap Senin)
    if counters["weekly_start"] != monday:
        counters["weekly_loss"] = 0.0
        counters["weekly_start"] = monday
        changed = True

    # Monthly reset (rolling 30 hari)
    if counters["monthly_start"]:
        start = datetime.strptime(counters["monthly_start"], "%Y-%m-%d")
        if (now - start).days >= 30:
            counters["monthly_loss"] = 0.0
            counters["monthly_start"] = today
            changed = True
    else:
        counters["monthly_start"] = today
        changed = True

    if changed:
        _save_counters(counters)

    return counters


def _save_counters(counters: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        UPDATE risk_counters SET
            daily_loss=?, weekly_loss=?, monthly_loss=?,
            daily_date=?, weekly_start=?, monthly_start=?,
            updated_at=datetime('now')
        WHERE id=1
    """, (counters["daily_loss"], counters["weekly_loss"],
          counters["monthly_loss"], counters["daily_date"],
          counters["weekly_start"], counters["monthly_start"]))
    conn.commit()
    conn.close()


def add_loss(loss_pct: float):
    """
    Tambahkan loss ke counter.
    BE (0%) tidak ditambahkan (dipanggil dengan loss_pct=0 = no-op).
    """
    if loss_pct <= 0:
        return
    counters = _get_counters()
    counters = _reset_counters_if_period_changed(counters)
    # Hitung loss% real-time dari balance saat ini
    acc = mt5.account_info()
    if acc and acc.balance > 0:
        actual_pct = loss_pct
    else:
        actual_pct = loss_pct
    counters["daily_loss"]   += actual_pct
    counters["weekly_loss"]  += actual_pct
    counters["monthly_loss"] += actual_pct
    _save_counters(counters)


def check_circuit_breaker(config: dict) -> dict:
    """
    Cek apakah ada circuit breaker yang aktif.
    Resume priority: Monthly > Weekly > Daily.

    Return: dict {
        "trading_allowed": bool,
        "blocked_by": "monthly"|"weekly"|"daily"|None,
        "daily_loss_pct": float,
        "weekly_loss_pct": float,
        "monthly_loss_pct": float,
    }
    """
    counters = _get_counters()
    counters = _reset_counters_if_period_changed(counters)

    # Loss% dihitung real-time dari balance saat ini
    acc = mt5.account_info()
    balance = acc.balance if acc and acc.balance > 0 else 1

    daily_pct   = counters["daily_loss"]
    weekly_pct  = counters["weekly_loss"]
    monthly_pct = counters["monthly_loss"]

    daily_limit   = config["risk"]["daily_limit_pct"] / 100.0
    weekly_limit  = config["risk"]["weekly_limit_pct"] / 100.0
    monthly_limit = config["risk"]["monthly_limit_pct"] / 100.0

    # Priority check: Monthly > Weekly > Daily
    if monthly_pct >= monthly_limit:
        return {"trading_allowed": False, "blocked_by": "monthly",
                "daily_loss_pct": round(daily_pct*100, 2),
                "weekly_loss_pct": round(weekly_pct*100, 2),
                "monthly_loss_pct": round(monthly_pct*100, 2)}

    if weekly_pct >= weekly_limit:
        return {"trading_allowed": False, "blocked_by": "weekly",
                "daily_loss_pct": round(daily_pct*100, 2),
                "weekly_loss_pct": round(weekly_pct*100, 2),
                "monthly_loss_pct": round(monthly_pct*100, 2)}

    if daily_pct >= daily_limit:
        return {"trading_allowed": False, "blocked_by": "daily",
                "daily_loss_pct": round(daily_pct*100, 2),
                "weekly_loss_pct": round(weekly_pct*100, 2),
                "monthly_loss_pct": round(monthly_pct*100, 2)}

    return {"trading_allowed": True, "blocked_by": None,
            "daily_loss_pct": round(daily_pct*100, 2),
            "weekly_loss_pct": round(weekly_pct*100, 2),
            "monthly_loss_pct": round(monthly_pct*100, 2)}


# =====================================================
# LEVEL BLACKLIST (re-entry protection)
# =====================================================
def add_to_blacklist(pair: str, level_price: float):
    """Tambahkan level ke blacklist hari ini."""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO level_blacklist (pair, level_price, blacklist_date)
        VALUES (?,?,?)
    """, (pair, level_price, today))
    conn.commit()
    conn.close()


def is_level_blacklisted(pair: str, level_price: float,
                          tolerance: float = None) -> bool:
    """
    Cek apakah level ini sudah di-blacklist hari ini.
    Tolerance: jika level dalam ±tolerance dari blacklisted level,
    dianggap sama.

    REVISI Step 5 (perbaikan normalisasi pip/point, Addendum v2.3
    Bab 3.6): tolerance TIDAK LAGI hardcode 0.0001 -- itu SALAH untuk
    XAUUSD (nyaris nol dibanding harga ~$4000) dan SALAH untuk USDJPY
    (1 pip riil = 0.01, bukan 0.0001). Sekarang default None -> dihitung
    otomatis via mt5_connector.pips_to_price(1, pair) (1 pip RIIL symbol
    tsb, fungsi pip TERPUSAT). Caller BOLEH override eksplisit dengan
    tolerance dalam harga absolut jika diperlukan kasus khusus.

    Args:
        pair: nama pair (boleh dgn atau tanpa suffix broker -- fungsi
              pip_size butuh symbol PERSIS broker, jadi jika pair belum
              ada suffix, coba tambahkan "m" -- KONSISTEN dgn pola
              resolve_symbol() lain di codebase; kalau symbol_info tetap
              gagal, fallback ke tolerance lama 0.0001 dgn WARNING supaya
              terlihat, bukan silent gagal total pada fungsi keamanan ini.
        level_price: harga level yang dicek
        tolerance: override manual (harga absolut). Default None ->
              1 pip riil symbol via pips_to_price().
    """
    if tolerance is None:
        # pair yang masuk ke fungsi ini historisnya TANPA suffix broker
        # (dipanggil dari tier2_orchestrator dgn pair_clean). Coba
        # resolve ke symbol broker (+ suffix "m") dulu -- KONSISTEN
        # dgn D-06 (symbol_suffix "m" tidak berubah).
        symbol_guess = pair if pair.endswith("m") else pair + "m"
        try:
            tolerance = mt5c.pips_to_price(1, symbol_guess)
        except RuntimeError:
            # Fallback SENGAJA terlihat (print WARNING), bukan silent --
            # ini fungsi keamanan (re-entry protection), gagal diam-diam
            # lebih berbahaya daripada gagal berisik.
            print(f"[WARNING is_level_blacklisted] Tidak bisa hitung "
                  f"pip_size utk '{symbol_guess}' -- fallback ke "
                  f"tolerance lama 0.0001. Cek symbol_info/suffix broker.")
            tolerance = 0.0001

    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT level_price FROM level_blacklist
        WHERE pair=? AND blacklist_date=?
    """, (pair, today))
    rows = c.fetchall()
    conn.close()
    for row in rows:
        if abs(row[0] - level_price) <= tolerance:
            return True
    return False


def cleanup_blacklist():
    """Hapus blacklist yang sudah kedaluwarsa (sebelum hari ini)."""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM level_blacklist WHERE blacklist_date < ?", (today,))
    conn.commit()
    conn.close()


# =====================================================
# TRADE LOG
# =====================================================
def log_trade_open(pair: str, direction: str, entry_price: float,
                    sl_price: float, tp_price: float,
                    lot: float, risk_pct: float, ob_id: int = None,
                    mt5_ticket: int = None):
    """
    Log trade baru saat dibuka.

    Args (BARU Fase 6): mt5_ticket -- ticket posisi asli dari
        order_send() (result.order), dipakai position_monitor.py untuk
        mencocokkan balik posisi MT5 saat modifikasi SL (breakeven) dan
        deteksi closure. Default None untuk kompatibilitas pemanggilan
        lama (kalau ada), TAPI di Fase 6 SEHARUSNYA selalu diisi --
        catat trade_log HANYA SETELAH order_send() sukses (urutan:
        order dulu, baru log_trade_open, bukan sebaliknya).
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO trade_log
        (pair, direction, entry_price, sl_price, tp_price,
         lot, risk_pct, ob_id, mt5_ticket)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (pair, direction, entry_price, sl_price, tp_price,
          lot, risk_pct, ob_id, mt5_ticket))
    trade_id = c.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def log_trade_close(trade_id: int, result: str, pnl_pct: float):
    """
    Update trade log saat ditutup.
    result: "TP" | "SL" | "BE" | "manual"
    pnl_pct: positif = profit, negatif = loss, 0 = BE
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        UPDATE trade_log SET
            status='closed', result=?, pnl_pct=?,
            closed_at=datetime('now')
        WHERE id=?
    """, (result, pnl_pct, trade_id))
    conn.commit()
    conn.close()

    # Update risk counter — BE = 0% (tidak ditambahkan)
    if result == "SL" and pnl_pct < 0:
        add_loss(abs(pnl_pct))


def get_open_trades() -> list:
    """
    (BARU Fase 6) Ambil semua trade_log berstatus 'open' beserta
    mt5_ticket -- dipakai position_monitor.py untuk mencocokkan balik
    ke mt5.positions_get() (deteksi masih terbuka) dan
    mt5.history_deals_get() (deteksi closure + alasan SL/TP via
    deal.reason, lihat catatan modul di atas).

    Return: list of dict {id, pair, direction, entry_price, sl_price,
    tp_price, lot, risk_pct, mt5_ticket, opened_at}
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT id, pair, direction, entry_price, sl_price, tp_price,
               lot, risk_pct, mt5_ticket, opened_at
        FROM trade_log WHERE status='open'
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


# =====================================================
# HELPER: count Group A open positions
# =====================================================
def count_open_group_a(config: dict) -> int:
    """Hitung jumlah posisi Group A yang masih terbuka."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT pair FROM trade_log WHERE status='open'
    """)
    rows = c.fetchall()
    conn.close()
    group_a = config["pairs"]["group_a"]
    count = 0
    for row in rows:
        pair = row[0].replace("m", "").replace(".raw", "")
        if pair in group_a:
            count += 1
    return count


if __name__ == "__main__":
    config = load_config()
    init_risk_db()
    cleanup_blacklist()

    print("=" * 55)
    print("TEST: risk_engine.py (Fase 6 -- kolom mt5_ticket ditambahkan)")
    print("=" * 55)

    if not mt5c.connect():
        sys.exit(1)

    acc = mt5.account_info()
    print(f"\nAkun: {acc.login} | Balance: {acc.balance} {acc.currency}")

    # Test 1: Circuit breaker
    print("\n--- TEST 1: Circuit Breaker ---")
    cb = check_circuit_breaker(config)
    print(f"  Trading allowed : {cb['trading_allowed']}")
    print(f"  Blocked by      : {cb['blocked_by']}")
    print(f"  Daily loss      : {cb['daily_loss_pct']}%")
    print(f"  Weekly loss     : {cb['weekly_loss_pct']}%")
    print(f"  Monthly loss    : {cb['monthly_loss_pct']}%")

    # Test 2: Lot calculation dengan debug info tick_size & tick_value
    print("\n--- TEST 2: Lot Calculation + Symbol Specs (risk=1%) ---")
    pairs_test = {
        "EURUSDm": 1.15650,
        "GBPUSDm": 1.34100,
        "AUDUSDm": 0.64500,
        "USDJPYm": 155.50,
        "XAUUSDm": 2345.00,
    }
    for sym_name, entry in pairs_test.items():
        sym = mt5.symbol_info(sym_name)
        if not sym:
            print(f"  {sym_name}: tidak tersedia di broker")
            continue

        # SL dummy: 1.2 ATR untuk forex, $3 untuk XAU, 0.5 untuk JPY
        if "XAU" in sym_name:
            sl = entry - 3.00
        elif "JPY" in sym_name:
            sl = entry - 0.50
        else:
            sl = entry - 0.00120

        lot = calculate_lot(sym_name, entry, sl, 0.01, config)

        print(f"\n  {sym_name}:")
        print(f"    tick_size    : {sym.trade_tick_size}")
        print(f"    tick_value   : {sym.trade_tick_value}")
        print(f"    contract_size: {sym.trade_contract_size}")
        print(f"    volume_min   : {sym.volume_min}")
        print(f"    volume_step  : {sym.volume_step}")
        sl_distance = abs(entry - sl)
        ticks = sl_distance / sym.trade_tick_size
        risk_usd = acc.balance * 0.01
        raw = risk_usd / (ticks * sym.trade_tick_value)
        print(f"    SL distance  : {sl_distance:.5f}")
        print(f"    Ticks at risk: {ticks:.1f}")
        print(f"    Risk amount  : {risk_usd:.2f} {acc.currency}")
        print(f"    Raw lot      : {raw:.4f}")
        print(f"    Final lot    : {lot:.2f}")

    # Test 3: Group A risk splitting
    print("\n--- TEST 3: Group A Risk Splitting ---")
    for n_open, expected in [(0, 1.0), (1, 0.5), (2, 0.33)]:
        pct = get_risk_pct_for_group("EURUSD", config, n_open)
        print(f"  Group A open={n_open}: risk={pct*100:.2f}% "
              f"(expected={expected}%)")

    # Test 4: Pre-entry check
    print("\n--- TEST 4: Pre-Entry Check ---")
    result = pre_entry_check(0.01, config)
    status = "ALLOW OK" if result["allowed"] else "BLOCK X"
    print(f"  New risk 1%     : {status}")
    print(f"  Open risk       : {result['total_risk_open']}%")
    print(f"  Floating loss   : {result['floating_loss_pct']}%")
    print(f"  Total projected : {result['total_projected']}%")
    print(f"  Max allowed     : {result['max_allowed']}%")

    # Test 5 (BARU Fase 6): mt5_ticket round-trip + get_open_trades()
    print("\n--- TEST 5: mt5_ticket kolom baru + get_open_trades() ---")
    test_id = log_trade_open(
        pair="EURUSDm", direction="bullish", entry_price=1.15000,
        sl_price=1.14700, tp_price=1.15600, lot=0.01, risk_pct=0.01,
        ob_id=None, mt5_ticket=999999999,  # ticket dummy, jelas bukan riil
    )
    open_trades = get_open_trades()
    matching = [t for t in open_trades if t["id"] == test_id]
    assert len(matching) == 1, "FAIL: baris test tidak ditemukan get_open_trades()"
    assert matching[0]["mt5_ticket"] == 999999999, "FAIL: mt5_ticket tidak tersimpan"
    print(f"  [OK] trade_log id={test_id} tersimpan dengan "
          f"mt5_ticket={matching[0]['mt5_ticket']}")

    # Cleanup baris test
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM trade_log WHERE id=?", (test_id,))
    conn.commit()
    conn.close()
    print(f"  [CLEANUP] Baris test id={test_id} dihapus.")

    mt5c.shutdown()
    print("\n[SELESAI] risk_engine.py (Fase 6, Step 3) berhasil.")
