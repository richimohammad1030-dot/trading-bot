"""
test_pip_regression.py - Step 4 (Perbaikan Normalisasi Pip/Point)

Bandingkan OUTPUT 4 implementasi pip LAMA vs fungsi TERPUSAT baru
(mt5_connector.pip_size / get_spread_pip) untuk 5 pair aktif, SEBELUM
pemanggil lama diganti (Step 5). Tujuan: pastikan tidak ada penyimpangan
tak terduga sebelum kode produksi diubah.

4 implementasi LAMA yang dibandingkan:
1. spread_filter.py::get_current_spread_pip() -- via digits in (5,3)->/10
2. mt5_connector.py::get_spread() (versi lama, masih ada di file)
3. orderblock.py::_pip_divisor() -- TERBUKTI BUG utk digits=3 (lihat audit)
4. exit_logic.py -- pip_size inline hardcode 0.0001/0.01

TIDAK mengubah file manapun -- murni baca & bandingkan. Aman dijalankan
kapan saja.

Jalankan: python src/test_pip_regression.py
Lalu KIRIM seluruh output ke chat SEBELUM lanjut ke Step 5.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import MetaTrader5 as mt5
from config_loader import load_config, get_all_pairs
import mt5_connector as mt5c
from spread_filter import get_current_spread_pip
from orderblock import _pip_divisor


def _old_exit_logic_pip_size(digits: int) -> float:
    """Replika PERSIS logic inline exit_logic.py::calculate_sl_tp()
    (hardcode -- ini yang mau dihapus di Step 5)."""
    return 0.0001 if digits in (5, 4) else (
        0.01 if digits in (3, 2) else 0.0001
    )


if __name__ == "__main__":
    config = load_config()

    print("=" * 90)
    print("REGRESI Step 4: 4 implementasi pip LAMA vs fungsi TERPUSAT BARU")
    print("=" * 90)

    if not mt5c.connect():
        sys.exit(1)

    pairs = get_all_pairs(config)
    total_mismatch = 0

    for pair in pairs:
        symbol = mt5c.resolve_symbol(pair, config)
        info = mt5.symbol_info(symbol)
        if info is None:
            print(f"\n[SKIP] {pair}: symbol_info None")
            continue

        digits = info.digits
        point = info.point

        # --- pip_size (harga per 1 pip) ---
        new_pip_size = mt5c.pip_size(symbol)
        old_exit_pip_size = _old_exit_logic_pip_size(digits)
        old_ob_divisor = _pip_divisor(symbol)
        old_ob_pip_size_implied = 1.0 / old_ob_divisor

        print(f"\n{'='*90}")
        print(f"PAIR: {pair} ({symbol})  digits={digits}  point={point}")
        print("=" * 90)
        print(f"  pip_size BARU (mt5_connector.pip_size)      : {new_pip_size}")
        print(f"  pip_size LAMA (exit_logic inline)             : {old_exit_pip_size}")
        print(f"  pip_size LAMA (orderblock, 1/divisor)        : "
              f"{old_ob_pip_size_implied}")

        mismatch_here = []
        if abs(new_pip_size - old_exit_pip_size) > 1e-12:
            mismatch_here.append("exit_logic")
        if abs(new_pip_size - old_ob_pip_size_implied) > 1e-12:
            mismatch_here.append("orderblock")

        # --- spread (pip) ---
        new_spread = mt5c.get_spread_pip(symbol)
        old_spread_filter_result = get_current_spread_pip(symbol)
        old_spread_filter_pip = old_spread_filter_result.get("spread_pip")
        old_mt5_spread = mt5c.get_spread(symbol)

        print(f"\n  spread_pip BARU (get_spread_pip)              : "
              f"{new_spread:.4f}")
        print(f"  spread_pip LAMA (spread_filter.py)            : "
              f"{old_spread_filter_pip}")
        print(f"  spread_pip LAMA (mt5_connector.get_spread)     : "
              f"{old_mt5_spread:.4f}")

        if old_spread_filter_pip is not None and \
                abs(new_spread - old_spread_filter_pip) > 0.01:
            mismatch_here.append("spread_filter(spread_now)")
        if abs(new_spread - old_mt5_spread) > 0.01:
            mismatch_here.append("mt5_connector.get_spread(spread_now)")

        if mismatch_here:
            total_mismatch += len(mismatch_here)
            print(f"\n  [!] MISMATCH terhadap fungsi BARU di: "
                  f"{', '.join(mismatch_here)}")
        else:
            print(f"\n  [OK] Semua implementasi LAMA sepakat dgn fungsi BARU "
                  f"utk {pair}")

    print(f"\n{'='*90}")
    print("RINGKASAN")
    print("=" * 90)
    if total_mismatch == 0:
        print("[OK] TIDAK ADA mismatch -- fungsi terpusat baru menghasilkan "
              "nilai IDENTIK dgn semua implementasi lama utk 5 pair aktif. "
              "AMAN lanjut ke Step 5 (ganti pemanggil).")
    else:
        print(f"[!] DITEMUKAN {total_mismatch} mismatch total -- ini "
              f"KEMUNGKINAN BESAR justru bukti bug lama yang sudah "
              f"diketahui (mis. orderblock._pip_divisor utk digits=3/JPY/XAU, "
              f"lihat TEMUAN_normalisasi_pip_point.md). Review detail di "
              f"atas SEBELUM lanjut Step 5 -- pastikan setiap mismatch "
              f"BISA DIJELASKAN, bukan penyimpangan baru yang tak terduga.")

    mt5c.shutdown()
    print("\n[SELESAI] Kirim SELURUH output ini ke chat sebelum lanjut Step 5.")
