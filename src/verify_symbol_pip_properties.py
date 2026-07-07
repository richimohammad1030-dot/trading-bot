"""
verify_symbol_pip_properties.py - Step 2 (Perbaikan Normalisasi Pip/Point)

Verifikasi properti symbol_info() RIIL dari akun Exness Cent Anda untuk
5 pair aktif, sebelum mendesain fungsi pip terpusat (Step 3).

Kenapa perlu ini SEBELUM menulis fungsi terpusat:
- Referensi kanonik (MQL5 forum, docs.mql5.com) mengasumsikan pola umum
  (digits 5/3 -> pip=10*point, digits 4/2 -> pip=point). Tapi Exness
  Cent account BISA punya digits/point yang tidak standar (khususnya
  suffix "m" bisa berarti skala berbeda) -- harus diverifikasi NYATA,
  bukan diasumsikan dari referensi umum.
- risk_engine.py::calculate_lot() SUDAH benar (pakai trade_tick_size/
  trade_tick_value langsung) -- skrip ini juga mencetak keduanya
  supaya konsisten dipakai sebagai referensi desain.

TIDAK mengubah apa pun -- murni membaca & mencetak. Aman dijalankan
kapan saja, tidak menyentuh file kritis.

Jalankan: python src/verify_symbol_pip_properties.py
Lalu KIRIM seluruh output ke chat.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import MetaTrader5 as mt5
from config_loader import load_config, get_all_pairs
import mt5_connector as mt5c


def _expected_pip_multiplier(digits: int) -> float:
    """Formula kanonik referensi (MQL5 forum, docs.mql5.com/python_metatrader5):
    digits 3 atau 5 -> pip = 10 x point
    selain itu      -> pip = 1 x point
    Dicetak sebagai PEMBANDING saja -- keputusan final menunggu data riil
    di bawah, bukan diasumsikan otomatis benar untuk broker Anda."""
    return 10.0 if digits in (3, 5) else 1.0


if __name__ == "__main__":
    config = load_config()

    print("=" * 78)
    print("VERIFIKASI symbol_info() RIIL -- Exness Cent (Step 2)")
    print("=" * 78)

    if not mt5c.connect():
        sys.exit(1)

    pairs = get_all_pairs(config)
    rows = []

    for pair in pairs:
        symbol = mt5c.resolve_symbol(pair, config)
        info = mt5.symbol_info(symbol)
        if info is None:
            print(f"\n[ERROR] symbol_info({symbol}) = None -- symbol tidak "
                  f"ditemukan/tidak visible. Cek broker_symbol_suffix.")
            continue

        digits = info.digits
        point = info.point
        expected_mult = _expected_pip_multiplier(digits)
        expected_pip = point * expected_mult

        row = {
            "pair": pair, "symbol": symbol, "digits": digits,
            "point": point, "expected_pip_multiplier": expected_mult,
            "expected_pip_price": expected_pip,
            "trade_tick_size": info.trade_tick_size,
            "trade_tick_value": info.trade_tick_value,
            "volume_min": info.volume_min,
            "volume_step": info.volume_step,
            "volume_max": info.volume_max,
            "spread_points_now": info.spread,
        }
        rows.append(row)

        print(f"\n--- {pair} ({symbol}) ---")
        print(f"  digits             : {digits}")
        print(f"  point              : {point}")
        print(f"  expected_pip_mult  : {expected_mult}  "
              f"(formula kanonik: digits 3/5 -> x10, lainnya -> x1)")
        print(f"  expected_pip_price : {expected_pip}  "
              f"(= point x expected_pip_mult)")
        print(f"  trade_tick_size    : {info.trade_tick_size}")
        print(f"  trade_tick_value   : {info.trade_tick_value}")
        print(f"  volume_min         : {info.volume_min}")
        print(f"  volume_step        : {info.volume_step}")
        print(f"  volume_max         : {info.volume_max}")
        print(f"  spread_points_now  : {info.spread}  "
              f"(-> {info.spread * expected_pip:.6f} price units, "
              f"jika formula kanonik benar utk pair ini)")

        # Bandingkan dengan asumsi LAMA yang sudah ada di 5 lokasi kode
        # (spread_filter, mt5_connector, orderblock x2, exit_logic):
        old_spread_filter = point * (10.0 if digits in (5, 3) else 1.0)
        old_mt5_connector = 10.0 if point <= 0.001 else 1.0  # divisor, bukan pip_price
        old_orderblock_divisor = (10000.0 if digits in (5, 3)
                                   else (100.0 if digits in (4, 2) else 10000.0))
        old_exitlogic_pip = 0.0001 if digits in (5, 4) else (
            0.01 if digits in (3, 2) else 0.0001)

        print(f"  [BANDING] spread_filter.py hasil    : {old_spread_filter}")
        print(f"  [BANDING] orderblock.py divisor      : "
              f"{old_orderblock_divisor}  "
              f"(-> pip_price tersirat = {1/old_orderblock_divisor:.6f})")
        print(f"  [BANDING] exit_logic.py pip_size      : {old_exitlogic_pip}")
        mismatch = []
        if abs(old_spread_filter - expected_pip) > 1e-12:
            mismatch.append("spread_filter")
        if abs((1/old_orderblock_divisor) - expected_pip) > 1e-12:
            mismatch.append("orderblock")
        if abs(old_exitlogic_pip - expected_pip) > 1e-12:
            mismatch.append("exit_logic")
        if mismatch:
            print(f"  [!] MISMATCH vs formula kanonik di: {', '.join(mismatch)}")
        else:
            print(f"  [OK] Semua implementasi lama SEPAKAT dgn formula kanonik "
                  f"utk pair ini")

    print(f"\n{'='*78}")
    print("RINGKASAN TABEL (copy-paste friendly)")
    print("=" * 78)
    header = (f"{'pair':8s} {'digits':6s} {'point':12s} "
              f"{'expected_pip':14s} {'tick_size':12s} {'tick_value':12s} "
              f"{'vol_min':8s} {'vol_step':8s}")
    print(header)
    for r in rows:
        print(f"{r['pair']:8s} {r['digits']:<6d} {r['point']:<12} "
              f"{r['expected_pip_price']:<14} {r['trade_tick_size']:<12} "
              f"{r['trade_tick_value']:<12} {r['volume_min']:<8} "
              f"{r['volume_step']:<8}")

    mt5c.shutdown()
    print("\n[SELESAI] Kirim SELURUH output ini (termasuk bagian [BANDING] "
          "dan [!] MISMATCH jika ada) ke chat untuk lanjut ke Step 3 (desain "
          "fungsi terpusat).")
