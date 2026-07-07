"""
orderblock.py - REBUILD v5

Engine: smartmoneyconcepts smc.ob() (1.7k stars, teruji komunitas)

RIWAYAT BUG DAN FIX:
v3→v4: Pakai 300 candle, filter NaN, swing_length=5
v4→v5: Multiple bug fixes ditemukan saat investigasi mendalam:
  1. Deduplication pakai ob_index (tidak stabil antar window) → fix ke harga
  2. Gap desain: OB menumpuk tanpa mekanisme cleanup → tambah cleanup_stale_obs()
  3. cleanup_stale_obs() expire OB, tapi detect_and_save_obs() simpan ulang
     karena cek duplikat hanya ke status='active' (OB baru di-expire tidak ada 
     lagi di daftar aktif) → fix: DELETE OB expired (bukan hanya ubah status),
     sehingga deteksi berikutnya bisa menyimpan fresh jika masih terdeteksi
  4. price_tolerance hardcode 0.00005 — sangat kecil untuk XAUUSD ($4000+)
     → fix: gunakan ATR-relative tolerance
  5. display width_pip hardcode *10000 → fix awal: pip_divisor per pair
     (v5 lama) -- REVISI Step 5 (perbaikan normalisasi pip/point, Addendum
     v2.3 Bab 3.6): _pip_divisor() sendiri TERBUKTI BUG (docstring bilang
     "USDJPY digits=3 -> divisor=100" tapi kode return 10000, grouping
     salah dgn digits=5 -- dikonfirmasi via test_pip_regression.py: 100x
     salah untuk USDJPY & XAUUSD). _pip_divisor() DIHAPUS, satu-satunya
     pemanggilnya (diagnostic __main__ di bawah) sekarang pakai
     mt5_connector.pip_size() -- fungsi pip TERPUSAT, satu-satunya sumber
     kebenaran di seluruh codebase. TIDAK ada dampak ke logika deteksi/
     filter produksi (detect_and_save_obs, cleanup_stale_obs) -- keduanya
     sudah pakai ATR-relative tolerance, bukan pip_divisor (lihat fix #4).
"""

import os, sys, sqlite3
from pathlib import Path
from datetime import datetime

os.environ["SMC_CREDIT"] = "0"
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import load_config, ROOT_DIR
from indicators import get_confirmed_snapshot, atr as calc_atr, _prep
import mt5_connector as mt5c

from smartmoneyconcepts import smc as SMC

import MetaTrader5 as mt5

DB_PATH = ROOT_DIR / "data" / "trading_bot.db"


# =====================================================
# DATABASE
# =====================================================
def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS order_blocks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            pair         TEXT NOT NULL,
            direction    TEXT NOT NULL,
            ob_top       REAL NOT NULL,
            ob_bottom    REAL NOT NULL,
            ob_width     REAL NOT NULL,
            ob_index     INTEGER NOT NULL,
            status       TEXT DEFAULT 'active',
            mitigated_by TEXT,
            created_at   TEXT DEFAULT (datetime('now')),
            trade_count  INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def get_active_obs(pair: str) -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, direction, ob_top, ob_bottom, ob_width, ob_index, trade_count
        FROM order_blocks WHERE pair=? AND status='active'
        ORDER BY created_at DESC
    """, (pair,))
    rows = c.fetchall()
    conn.close()
    return [{"id":r[0],"direction":r[1],"ob_top":r[2],
             "ob_bottom":r[3],"ob_width":r[4],
             "ob_index":r[5],"trade_count":r[6]} for r in rows]


def _save_ob(pair: str, ob: dict):
    """Simpan OB ke DB. Deduplikasi ditangani oleh caller."""
    import math
    for key in ["ob_top", "ob_bottom", "ob_width", "ob_index"]:
        val = ob.get(key)
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return  # Skip — data tidak valid
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO order_blocks
        (pair,direction,ob_top,ob_bottom,ob_width,ob_index)
        VALUES (?,?,?,?,?,?)
    """, (pair, ob["direction"], ob["ob_top"], ob["ob_bottom"],
          ob["ob_width"], ob["ob_index"]))
    conn.commit()
    conn.close()


def mitigate_ob(ob_id: int, reason: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        UPDATE order_blocks SET status='mitigated', mitigated_by=?
        WHERE id=?
    """, (reason, ob_id))
    conn.commit()
    conn.close()


def _delete_ob(ob_id: int):
    """Hapus OB dari DB — dipakai oleh cleanup_stale_obs() supaya
    OB yang di-expire bisa terdeteksi ulang secara fresh di run berikutnya.
    Berbeda dengan mitigate_ob() yang hanya ubah status (OB tetap ada di DB
    dan mencegah re-insert oleh deduplication logic)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM order_blocks WHERE id=?", (ob_id,))
    conn.commit()
    conn.close()


def cleanup_stale_obs(pair: str, current_price: float, atr_val: float,
                       max_distance_multiplier: float = 5.0,
                       max_age_days: int = 14):
    """
    HAPUS OB yang sudah terlalu jauh dari harga ATAU terlalu lama.

    KENAPA DELETE bukan UPDATE status:
    - mitigate_ob() (UPDATE status='mitigated') = OB ini sudah TERKENA harga,
      tidak boleh muncul lagi sebagai zona entry baru.
    - _delete_ob() (DELETE) = OB ini belum terkena harga, tapi sudah tidak
      relevan karena harga sudah bergerak jauh. Kalau harga kembali ke area
      itu, OB akan terdeteksi ulang secara fresh oleh smc.ob() dan disimpan
      lagi — ini perilaku yang BENAR.

    Dua kriteria expire (OR):
    1. JARAK: center OB > 5xATR dari harga sekarang
    2. USIA: OB berumur > max_age_days hari
    """
    max_distance = atr_val * max_distance_multiplier
    now = datetime.now()
    active = get_active_obs(pair)
    deleted_count = 0

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    for ob in active:
        center = (ob["ob_top"] + ob["ob_bottom"]) / 2
        distance = abs(current_price - center)
        too_far = distance > max_distance

        c.execute("SELECT created_at FROM order_blocks WHERE id=?", (ob["id"],))
        row = c.fetchone()
        too_old = False
        if row and row[0]:
            try:
                created = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                age_days = (now - created).days
                too_old = age_days > max_age_days
            except Exception:
                pass

        if too_far or too_old:
            reason = "too_far" if too_far else "too_old"
            _delete_ob(ob["id"])
            deleted_count += 1

    conn.close()
    return deleted_count


def increment_trade_count(ob_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE order_blocks SET trade_count=trade_count+1 WHERE id=?", (ob_id,))
    conn.commit()
    conn.close()


# =====================================================
# OB DETECTION
# =====================================================
def detect_and_save_obs(df: pd.DataFrame, pair: str,
                         config: dict) -> list:
    """
    Deteksi OB menggunakan smc.ob(), simpan yang baru ke DB.

    Deduplication: bandingkan berdasarkan HARGA (bukan ob_index yang
    tidak stabil antar window candle). Tolerance ATR-relative supaya
    bekerja benar untuk semua pair (EURUSD, XAUUSD, USDJPY, dll) --
    TIDAK bergantung pip_divisor sama sekali (fix #4 lama), jadi TIDAK
    terdampak penghapusan _pip_divisor() di Step 5 ini.
    """
    atr_val = float(calc_atr(df, config["atr"]["period"]).iloc[-2])
    # REVISI (ditemukan Fase 3, verifikasi visual chart): max_width
    # 2.0xATR terlalu longgar -- meloloskan OB selebar ~19-20 pip di
    # EURUSD H1, jauh dari definisi SMC "satu candle opposite terakhir
    # sebelum displacement". Sekarang bisa dikalibrasi via config,
    # default diperketat ke 1.0xATR.
    ob_cfg = config.get("orderblock", {})
    max_width = atr_val * ob_cfg.get("max_width_atr_multiplier", 1.0)
    # Tolerance deduplication: 5% ATR — cukup besar untuk absorb floating
    # point diff antar run, cukup kecil untuk tidak menggabungkan OB berbeda
    price_tolerance = atr_val * 0.05

    df_closed = _prep(df)  # exclude candle[-1] + rename tick_volume
    swing = SMC.swing_highs_lows(df_closed, swing_length=5)
    ob_df = SMC.ob(df_closed, swing, close_mitigation=True)

    valid_obs = ob_df[ob_df["OB"].notna() & (ob_df["OB"] != 0)]

    # Cek duplikat terhadap OB yang SEDANG AKTIF saja.
    # OB yang sudah di-cleanup (dihapus dari DB) boleh terdeteksi ulang.
    existing = get_active_obs(pair)
    new_obs = []

    for i, row in valid_obs.iterrows():
        top    = float(row["Top"])
        bottom = float(row["Bottom"])
        direction = "bullish" if row["OB"] == 1 else "bearish"
        width  = top - bottom
        mit_idx = float(row["MitigatedIndex"]) if row["MitigatedIndex"] else 0

        # Skip jika sudah dimitigasi library
        if mit_idx > 0:
            continue
        # Skip jika terlalu lebar
        if width > max_width:
            continue
        # Skip duplikat (berbasis harga dengan ATR-relative tolerance)
        is_duplicate = any(
            e["direction"] == direction
            and abs(e["ob_top"] - top) <= price_tolerance
            and abs(e["ob_bottom"] - bottom) <= price_tolerance
            for e in existing
        )
        if is_duplicate:
            continue

        ob = {"direction": direction,
              "ob_top": round(top, 5),
              "ob_bottom": round(bottom, 5),
              "ob_width": round(width, 5),
              "ob_index": int(i)}
        _save_ob(pair, ob)
        new_obs.append(ob)

    return new_obs


# =====================================================
# MITIGASI CHECK
# =====================================================
def check_mitigation(pair: str, df: pd.DataFrame):
    """
    Cek OB aktif — mitigasi (via mitigate_ob) jika library atau harga
    mengindikasikan OB sudah DITEMBUS harga (berbeda dari cleanup_stale_obs
    yang DELETE OB karena sudah jauh/lama tanpa pernah ditembus).
    """
    active = get_active_obs(pair)
    if not active:
        return

    df_closed = _prep(df)
    swing = SMC.swing_highs_lows(df_closed, swing_length=5)
    ob_df = SMC.ob(df_closed, swing, close_mitigation=True)
    last_close = float(df_closed["close"].iloc[-1])

    for ob in active:
        idx = ob["ob_index"]
        # A: Library mark mitigated
        if idx < len(ob_df):
            row = ob_df.iloc[idx]
            if row["MitigatedIndex"] and float(row["MitigatedIndex"]) > 0:
                mitigate_ob(ob["id"], "library_mitigated")
                continue

        # B: SATU candle close menembus SELURUH OB → mitigated (standar ICT)
        if ob["direction"] == "bearish" and last_close > ob["ob_top"]:
            mitigate_ob(ob["id"], "price_closed_above_bearish_ob")
        elif ob["direction"] == "bullish" and last_close < ob["ob_bottom"]:
            mitigate_ob(ob["id"], "price_closed_below_bullish_ob")


# =====================================================
# FILTER JARAK
# =====================================================
def filter_by_distance(obs: list, current_price: float,
                        atr_val: float) -> list:
    result = []
    for ob in obs:
        center = (ob["ob_top"] + ob["ob_bottom"]) / 2
        in_range = abs(current_price - center) <= 2 * atr_val
        result.append({**ob, "in_range": in_range})
    return result


if __name__ == "__main__":
    import mt5_connector as mt5c
    from config_loader import load_config

    config = load_config()
    print("=" * 60)
    print("DEBUG: orderblock.py raw detection inspection")
    print("=" * 60)

    if not mt5c.connect():
        sys.exit(1)

    pair = "EURUSD"
    symbol = mt5c.resolve_symbol(pair, config)
    df = mt5c.get_candles(symbol, "H1", count=300)

    current_price = float(df["close"].iloc[-2])
    atr_val = float(calc_atr(df, config["atr"]["period"]).iloc[-2])
    ob_cfg = config.get("orderblock", {})
    max_width = atr_val * ob_cfg.get("max_width_atr_multiplier", 1.0)
    # REVISI Step 5: _pip_divisor() lokal DIHAPUS (terbukti bug via
    # test_pip_regression.py -- 100x salah utk USDJPY/XAUUSD). Sekarang
    # pakai mt5_connector.pip_size() -- fungsi pip TERPUSAT.
    pip_sz = mt5c.pip_size(symbol)

    print(f"\nHarga saat ini (candle[-2]): {current_price:.5f}")
    print(f"ATR H1: {atr_val:.5f}")
    mult = ob_cfg.get("max_width_atr_multiplier", 1.0)
    print(f"max_width filter ({mult}xATR): {max_width:.5f}")
    print(f"pip_size (mt5_connector terpusat): {pip_sz}")

    df_closed = _prep(df)
    swing = SMC.swing_highs_lows(df_closed, swing_length=5)
    ob_df = SMC.ob(df_closed, swing, close_mitigation=True)
    valid_obs = ob_df[ob_df["OB"].notna() & (ob_df["OB"] != 0)]

    print(f"\nTotal OB mentah dari smc.ob(): {len(valid_obs)}")
    print(f"  {'idx':>5} {'dir':8} {'top':>12} {'bottom':>12} "
          f"{'width':>8} {'width_pip':>10} {'mit_idx':>8} {'dist':>8}  "
          f"{'lolos_w':>8} {'lolos_m':>8}")

    for i, row in valid_obs.iterrows():
        top = float(row["Top"])
        bottom = float(row["Bottom"])
        direction = "bullish" if row["OB"] == 1 else "bearish"
        width = top - bottom
        # REVISI Step 5: width_pip sekarang via mt5c.price_to_pips()
        # (fungsi terpusat), bukan width * pip_divisor lama.
        width_pip = mt5c.price_to_pips(width, symbol)
        mit_idx = float(row["MitigatedIndex"]) if row["MitigatedIndex"] else 0
        center = (top + bottom) / 2
        dist = abs(current_price - center)
        lolos_w = "YA" if width <= max_width else "TIDAK"
        lolos_m = "YA" if mit_idx == 0 else "TIDAK"
        print(f"  {i:>5} {direction:8} {top:>12.5f} {bottom:>12.5f} "
              f"{width:>8.5f} {width_pip:>10.2f} {mit_idx:>8.0f} {dist:>8.5f}  "
              f"{lolos_w:>8} {lolos_m:>8}")

    print(f"\n--- OB di database (status='active') ---")
    active = get_active_obs(pair)
    for ob in active:
        center = (ob["ob_top"] + ob["ob_bottom"]) / 2
        dist = abs(current_price - center)
        in_range = dist <= (2 * atr_val)
        print(f"  id={ob['id']} {ob['direction']:8s} "
              f"{ob['ob_bottom']:.5f}-{ob['ob_top']:.5f} "
              f"dist={dist:.5f} in_range={in_range}")

    mt5c.shutdown()
