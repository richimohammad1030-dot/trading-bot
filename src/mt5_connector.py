"""
mt5_connector.py

Modul koneksi ke Exness via MetaTrader5 Python package.
Menyediakan fungsi dasar:
- connect()              : login ke akun MT5
- get_candles()           : ambil OHLCV untuk pair+timeframe
- get_spread()             : ambil spread real-time (LAMA -- lihat catatan di bawah)
- get_account_info()       : saldo, equity, dll
- get_open_positions()     : posisi yang sedang terbuka
- shutdown()               : tutup koneksi

BARU (Fase: perbaikan normalisasi pip/point, Addendum v2.3 Bab 3.6):
- pip_size(symbol)              : SATU-SATUNYA sumber kebenaran nilai 1 pip
- price_to_pips(diff, symbol)   : selisih harga -> jumlah pip
- pips_to_price(pips, symbol)   : jumlah pip -> selisih harga
- get_spread_pip(symbol)        : spread MT5 (points) -> pip, pakai pip_size()

KENAPA fungsi baru ini dibuat (audit temuan TEMUAN_normalisasi_pip_point.md):
Sebelumnya ADA 4 implementasi konversi pip berbeda tersebar (spread_filter.py,
mt5_connector.py::get_spread lama, orderblock.py::_pip_divisor,
exit_logic.py::calculate_sl_tp inline) -- salah satunya (exit_logic) bahkan
HARDCODE pip_size tanpa membaca symbol_info sama sekali. Diverifikasi juga
BUG NYATA di orderblock.py::_pip_divisor(): docstring-nya sendiri bilang
"USDJPY digits=3 -> divisor=100" tapi kodenya mengembalikan 10000 (grouping
salah dgn digits=5). Dikonfirmasi via skrip verifikasi symbol_info riil
Exness Cent (5 pair aktif) -- lihat riwayat chat audit.

FORMULA (referensi: pola kanonik forum MQL5 -- SymbolInfoDouble(SYMBOL_POINT)
dikombinasikan SymbolInfoInteger(SYMBOL_DIGITS), lihat mql5.com/en/forum/232387;
juga cocok dgn dokumentasi resmi mql5.com/en/docs/python_metatrader5):
    pip_multiplier = 10.0 jika digits in (3, 5), selain itu 1.0
    pip_size       = point * pip_multiplier

CATATAN PENTING SOAL XAUUSD (riset tambahan, forum MQL5 + Vantage/DailyForex):
Emas TIDAK mengikuti aturan forex "digit kedua dari belakang" secara semantik
-- konvensi pasar gold adalah "1 pip = $0.01" secara TETAP, independen dari
presisi kuotasi broker. Formula generik di atas KEBETULAN menghasilkan $0.01
untuk XAUUSDm Exness Cent Anda (digits=3 -> 0.001*10=0.01, cocok), TAPI ini
kebetulan berbasis digits broker SEKARANG, bukan derivasi konvensi gold yang
benar. Makanya pip_size() punya SANITY CHECK (bukan hardcode kedua -- murni
validasi) yang akan mem-print WARNING jika hasil utk symbol "XAU" menyimpang
signifikan dari $0.01, supaya kalau broker ubah presisi gold nanti, sistem
KELIHATAN berteriak alih-alih diam-diam salah skala.

PENTING - Symbol naming:
Exness kadang menambahkan suffix di nama symbol (misal
"EURUSDm" untuk akun cent, atau "EURUSD.raw"). Jalankan
list_available_symbols() dulu untuk cek nama PERSIS yang
dipakai akun kamu, lalu update config.yaml jika perlu
(field "broker_symbol_suffix").
"""

import sys
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import load_config, load_secrets


# Mapping timeframe string -> MT5 constant
TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


def connect() -> bool:
    """Inisialisasi MT5 dan login menggunakan kredensial dari .env"""
    secrets = load_secrets()

    if not mt5.initialize():
        print(f"[ERROR] mt5.initialize() gagal: {mt5.last_error()}")
        return False

    login = int(secrets["MT5_LOGIN"])
    password = secrets["MT5_PASSWORD"]
    server = secrets["MT5_SERVER"]

    authorized = mt5.login(login, password=password, server=server)
    if not authorized:
        print(f"[ERROR] Login gagal: {mt5.last_error()}")
        mt5.shutdown()
        return False

    print(f"[OK] Login berhasil ke akun {login} @ {server}")
    return True


def shutdown():
    mt5.shutdown()


def resolve_symbol(pair: str, config: dict) -> str:
    """
    Tambahkan suffix broker jika ada di config.yaml
    (field broker_symbol_suffix, default "").
    Contoh: pair="EURUSD" + suffix="m" -> "EURUSDm"
    """
    suffix = config.get("broker", {}).get("symbol_suffix", "")
    symbol = pair + suffix

    # Pastikan symbol tersedia & visible di Market Watch
    info = mt5.symbol_info(symbol)
    if info is None:
        print(f"[WARNING] Symbol '{symbol}' tidak ditemukan. "
              f"Cek list_available_symbols() untuk nama yang benar.")
        return symbol
    if not info.visible:
        mt5.symbol_select(symbol, True)
    return symbol


def list_available_symbols(filter_str: str = "") -> list:
    """List semua symbol yang tersedia di broker (untuk debugging
    penamaan, misal cari 'EURUSD' -> ketemu 'EURUSDm')"""
    symbols = mt5.symbols_get()
    names = [s.name for s in symbols]
    if filter_str:
        names = [n for n in names if filter_str.upper() in n.upper()]
    return names


def get_candles(symbol: str, timeframe: str, count: int = 100) -> pd.DataFrame:
    """
    Ambil candle OHLCV terakhir.
    Return DataFrame dengan kolom: time, open, high, low, close,
    tick_volume, spread, real_volume

    PENTING (anti-repaint): candle index -1 adalah candle yang
    SEDANG BERJALAN (belum close). Modul indikator (Step 4+)
    HARUS pakai index [-2] untuk ATR, [-6] untuk fractal, dst.
    Fungsi ini HANYA mengambil data mentah - logic anti-repaint
    diterapkan di modul pemanggil.
    """
    tf = TIMEFRAME_MAP.get(timeframe.upper())
    if tf is None:
        raise ValueError(f"Timeframe '{timeframe}' tidak dikenal. "
                          f"Pilihan: {list(TIMEFRAME_MAP.keys())}")

    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"Gagal ambil candle untuk {symbol} {timeframe}: "
                            f"{mt5.last_error()}")

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df


# =====================================================
# BARU -- NORMALISASI PIP/POINT TERPUSAT
# (Addendum v2.3 Bab 3.6 -- SATU-SATUNYA sumber konversi pip di seluruh
# codebase. Semua modul lain WAJIB memanggil ini, tidak boleh menghitung
# sendiri. Lihat TEMUAN_normalisasi_pip_point.md untuk audit lengkap.)
# =====================================================

# Toleransi sanity-check XAUUSD: seberapa jauh pip_size boleh menyimpang
# dari konvensi pasar $0.01 sebelum di-print sebagai WARNING. Bukan
# angka yang menentukan nilai (itu tetap symbol_info) -- murni ambang
# deteksi anomali.
_XAU_EXPECTED_PIP = 0.01
_XAU_SANITY_TOLERANCE = 0.005  # +-50% dari 0.01


def pip_size(symbol: str) -> float:
    """
    SATU-SATUNYA fungsi yang boleh menentukan "berapa harga 1 pip" di
    seluruh codebase (Addendum v2.3 Bab 3.6). Semua modul lain (spread
    check, SL/TP, blacklist tolerance, dst) WAJIB panggil ini -- tidak
    boleh hardcode atau menghitung ulang.

    Formula (referensi kanonik forum MQL5 -- lihat docstring modul):
        pip_multiplier = 10.0 jika digits in (3, 5), selain itu 1.0
        pip_size       = point * pip_multiplier

    Args:
        symbol: nama symbol broker (misal "EURUSDm", "XAUUSDm")

    Return: harga 1 pip dalam satuan harga absolut (float)

    Raises:
        RuntimeError jika symbol_info(symbol) None -- SENGAJA tidak
        di-silent-fallback ke angka default, karena pip yang salah
        bisa bikin SL/TP/lot salah skala tanpa error terlihat (persis
        risiko yang diperingatkan Addendum v2.3 Bab 3.2).
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(
            f"symbol_info({symbol}) = None -- tidak bisa hitung pip_size. "
            f"Cek apakah symbol terdaftar & visible di Market Watch "
            f"(lihat resolve_symbol())."
        )

    digits = info.digits
    point = info.point
    pip_multiplier = 10.0 if digits in (3, 5) else 1.0
    size = point * pip_multiplier

    # --- SANITY CHECK khusus XAU (bukan hardcode kedua, murni validasi) ---
    # Konvensi pasar gold: 1 pip = $0.01, TIDAK diturunkan dari digits
    # broker (riset: forum MQL5 + Vantage/DailyForex). Formula generik di
    # atas KEBETULAN cocok utk broker Anda SEKARANG (digits=3 -> 0.01).
    # Kalau broker ubah presisi gold nanti, print WARNING supaya terlihat,
    # BUKAN diam-diam salah skala.
    if "XAU" in symbol.upper():
        if abs(size - _XAU_EXPECTED_PIP) > _XAU_SANITY_TOLERANCE:
            print(f"[WARNING pip_size] {symbol}: hasil pip_size={size} "
                  f"menyimpang jauh dari konvensi pasar gold ($0.01). "
                  f"digits={digits}, point={point}. Kemungkinan broker "
                  f"mengubah presisi kuotasi gold -- VERIFIKASI ULANG "
                  f"sebelum lanjut trading (jalankan "
                  f"verify_symbol_pip_properties.py).")

    return size


def price_to_pips(price_diff: float, symbol: str) -> float:
    """Konversi selisih harga -> jumlah pip. Pakai pip_size() -- tidak
    ada perhitungan pip lain di luar fungsi ini."""
    return abs(price_diff) / pip_size(symbol)


def pips_to_price(pips: float, symbol: str) -> float:
    """Konversi jumlah pip -> selisih harga. Pakai pip_size() -- tidak
    ada perhitungan pip lain di luar fungsi ini."""
    return pips * pip_size(symbol)


def get_spread_pip(symbol: str) -> float:
    """
    Spread real-time MT5 (dalam points) dikonversi ke pip, pakai
    pip_size() sebagai satu-satunya sumber kebenaran.

    MENGGANTIKAN (Step 5, belum dieksekusi -- fungsi ini baru DITAMBAH,
    pemanggil lama belum diganti):
    - get_spread() (versi lama di bawah, akan dipensiunkan)
    - spread_filter.py::get_current_spread_pip() (logic konversi manual)

    Return: spread saat ini dalam pip (float). Raise RuntimeError jika
    symbol_info/tick tidak tersedia (via pip_size(), TIDAK silent).
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"symbol_info({symbol}) = None -- "
                            f"tidak bisa hitung spread.")
    spread_points = info.spread
    point = info.point
    spread_price = spread_points * point
    return price_to_pips(spread_price, symbol)


# =====================================================
# get_spread() LAMA -- DIHAPUS di Step 5 (File 5/5, pembersihan akhir).
# Terverifikasi via grep: NOL pemanggil di seluruh codebase (termasuk
# spread_filter.py yang sudah didelegasikan ke get_spread_pip() di File
# 1/5). Gunakan get_spread_pip() -- fungsi pip TERPUSAT di atas.
# =====================================================


def get_account_info() -> dict:
    """Ambil info akun: balance, equity, currency, leverage, dll"""
    info = mt5.account_info()
    if info is None:
        raise RuntimeError(f"Gagal ambil account info: {mt5.last_error()}")
    return {
        "login": info.login,
        "balance": info.balance,
        "equity": info.equity,
        "currency": info.currency,
        "leverage": info.leverage,
        "margin_free": info.margin_free,
    }


def get_open_positions() -> pd.DataFrame:
    """Ambil semua posisi terbuka saat ini"""
    positions = mt5.positions_get()
    if positions is None or len(positions) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(list(positions), columns=positions[0]._asdict().keys())
    return df


if __name__ == "__main__":
    # ===== TEST RUN =====
    config = load_config()

    print("=" * 50)
    print("TEST: Koneksi MT5")
    print("=" * 50)

    if not connect():
        print("\n[GAGAL] Koneksi tidak berhasil. Cek .env (MT5_LOGIN, "
              "MT5_PASSWORD, MT5_SERVER) dan pastikan MT5 terminal "
              "sudah pernah login manual minimal sekali.")
        sys.exit(1)

    print("\n--- Account Info ---")
    acc = get_account_info()
    for k, v in acc.items():
        print(f"{k:15s}: {v}")

    print("\n--- Cari Symbol EURUSD ---")
    matches = list_available_symbols("EURUSD")
    print(f"Symbol mengandung 'EURUSD': {matches}")

    print("\n--- Test Ambil Candle (EURUSD H1, 5 candle terakhir) ---")
    symbol = resolve_symbol("EURUSD", config)
    print(f"Menggunakan symbol: {symbol}")
    try:
        df = get_candles(symbol, "H1", count=5)
        print(df[["time", "open", "high", "low", "close", "tick_volume", "spread"]])
    except Exception as e:
        print(f"[ERROR] {e}")

    print("\n--- Test Spread (get_spread_pip terpusat) ---")
    try:
        spread_now = get_spread_pip(symbol)
        print(f"Spread {symbol}: {spread_now:.4f} pip")
    except Exception as e:
        print(f"[ERROR] {e}")

    print("\n--- Test pip_size() semua pair aktif ---")
    from config_loader import get_all_pairs
    for pair in get_all_pairs(config):
        sym = resolve_symbol(pair, config)
        try:
            ps = pip_size(sym)
            print(f"  {pair:8s} ({sym:10s}): pip_size={ps}")
        except Exception as e:
            print(f"  {pair:8s}: [ERROR] {e}")

    print("\n--- Test Open Positions ---")
    positions = get_open_positions()
    if positions.empty:
        print("Tidak ada posisi terbuka (normal untuk akun baru).")
    else:
        print(positions[["symbol", "type", "volume", "price_open", "profit"]])

    shutdown()
    print("\n[SELESAI] mt5_connector.py (Step 5 -- get_spread() lama "
          "dihapus, pip terpusat final) berhasil.")
