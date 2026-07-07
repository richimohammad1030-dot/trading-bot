"""
order_execution.py - Fase 6, Step 4

Kirim order market sungguhan ke MT5 (TRADE_ACTION_DEAL) memakai
sl_price/tp_price MURNI dari Claude (Task 5) -- SL/TP TIDAK dihitung
ulang atau divalidasi jaraknya di sini (D-22, lihat exit_logic.py:
"SL/TP murni Tier 2, tidak disentuh Tier 1 sama sekali").

RISET (WAJIB dibaca sebelum ubah desain ini):
- Filling mode BROKER-SPESIFIK (bahkan bisa beda antar SYMBOL di
  broker yang sama), bukan satu nilai universal. Hardcode satu mode
  (mis. selalu ORDER_FILLING_IOC) menyebabkan retcode 10030
  "Unsupported filling mode" -- masalah SANGAT umum, dikonfirmasi
  puluhan thread independen forum resmi MQL5 (mql5.com/en/forum/368425,
  /487581, /508038, /456276, fxdreema.com forum) DAN referensi kode
  GitHub (dev.to/vital7777, medium.com/@elospieconomics). Fix
  universal yang disepakati SEMUA sumber: baca symbol_info.filling_mode
  (bitmask) saat runtime, coba FOK -> IOC -> RETURN berurutan, pakai
  yang PERTAMA didukung symbol tsb -- BUKAN hardcode satu mode.
- Pattern request dict (TRADE_ACTION_DEAL, price dari symbol_info_tick
  ask/bid, deviation, type_time=ORDER_TIME_GTC) dikonfirmasi identik
  di 6+ sumber independen: dokumentasi resmi MQL5
  (mql5.com/en/docs/python_metatrader5/mt5ordersend_py),
  tradepretty.com, GitHub Quantreo/MetaTrader-5-AUTOMATED-TRADING-using-Python,
  forum resmi MQL5 (mql5.com/en/forum/343594).
- retcode sukses = TRADE_RETCODE_DONE (10009). result.order = ticket
  posisi yang baru dibuka (dikonfirmasi contoh resmi & GitHub Quantreo).

PRE-EXECUTION SPREAD CHECK (P-07, Addendum v2.3 Bab 2.2): spread
di-cek ULANG lewat spread_filter.check_spread() TEPAT SEBELUM
order_send() -- spread saat screener menemukan kandidat bisa sudah
berubah beberapa detik/menit kemudian (candle M5 confirm + build
package + panggil Claude butuh waktu, bisa >10 detik). Kalau spread
melebar di luar threshold pas mau eksekusi, order DIBATALKAN (bukan
dipaksa jalan) -- ini menutup item pending P-07 di 04_design_decisions.md.

SL/TP MURNI Tier 2 (D-22): fungsi ini TIDAK menghitung/memvalidasi
sl_price/tp_price -- hanya meneruskan apa adanya dari execution gate
tier2_orchestrator.py ke request MT5.
"""

import sys
import time
from pathlib import Path

import MetaTrader5 as mt5

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import load_config
import mt5_connector as mt5c
from spread_filter import check_spread

# Identifier internal bot untuk order MT5 (bukan dari config, murni
# penanda supaya bisa dibedakan dari order manual di terminal).
MAGIC_NUMBER = 20260601


def _determine_filling_mode(symbol_info) -> int:
    """
    Deteksi filling mode yang didukung symbol INI (broker-spesifik).

    Referensi (konsensus semua sumber riset di docstring modul):
    urutan preferensi FOK -> IOC -> RETURN, dicek via bitmask
    symbol_info.filling_mode & mode_const. Raise RuntimeError kalau
    TIDAK ADA yang didukung (sangat tidak biasa, tapi jangan
    asumsikan default diam-diam -- filosofi "gagal harus terlihat",
    konsisten D-09).
    """
    modes = [
        (mt5.ORDER_FILLING_FOK, "FOK"),
        (mt5.ORDER_FILLING_IOC, "IOC"),
        (mt5.ORDER_FILLING_RETURN, "RETURN"),
    ]
    for mode_const, mode_name in modes:
        if symbol_info.filling_mode & mode_const:
            return mode_const
    raise RuntimeError(
        f"Tidak ada filling mode yang didukung symbol {symbol_info.name} "
        f"(filling_mode raw={symbol_info.filling_mode}). Tidak bisa kirim order."
    )


def send_market_order(pair: str, symbol: str, direction: str, lot: float,
                       sl_price: float, tp_price: float, config: dict,
                       deviation: int = 20,
                       comment: str = "claude_execute") -> dict:
    """
    Kirim order market (TRADE_ACTION_DEAL) ke MT5.

    Args:
        pair: nama pair TANPA suffix (hanya untuk logging/pesan --
              spread_filter.check_spread() menggunakan `symbol` broker
              penuh di dalamnya).
        symbol: nama symbol broker PERSIS (mis. "EURUSDm").
        direction: "bullish" (BUY) | "bearish" (SELL).
        lot: SUDAH dihitung risk_engine.calculate_lot() sebelumnya --
             TIDAK dihitung ulang di sini.
        sl_price, tp_price: MURNI dari Claude (Task 5) -- TIDAK
             divalidasi jarak/rasio di sini (D-22).
        config: config dict.

    Return: dict {
        "success": bool,
        "reason": str | None,        # alasan gagal (termasuk spread)
        "ticket": int | None,        # result.order kalau sukses
        "fill_price": float | None,  # result.price kalau sukses
        "retcode": int | None,
        "comment": str | None,       # comment dari MT5
        "filling_mode_used": str | None,
    }
    """
    base_fail = {"success": False, "reason": None, "ticket": None,
                 "fill_price": None, "retcode": None, "comment": None,
                 "filling_mode_used": None}

    # --- PRE-EXECUTION SPREAD CHECK (P-07) ---
    # Spread saat screener menemukan kandidat bisa sudah berubah di
    # titik ini (build package + panggil Claude butuh waktu). Cek
    # ULANG tepat sebelum order_send -- kalau melebar, batalkan.
    spread_check = check_spread(symbol, config)
    if not spread_check["allowed"]:
        return {**base_fail,
                "reason": f"spread_widened_pre_execution: {spread_check}"}

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        return {**base_fail, "reason": f"symbol_info({symbol})_none"}

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return {**base_fail, "reason": f"symbol_info_tick({symbol})_none"}

    is_buy = (direction == "bullish")
    order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
    price = tick.ask if is_buy else tick.bid

    try:
        filling_mode = _determine_filling_mode(symbol_info)
    except RuntimeError as e:
        return {**base_fail, "reason": str(e)}

    filling_name = {mt5.ORDER_FILLING_FOK: "FOK",
                     mt5.ORDER_FILLING_IOC: "IOC",
                     mt5.ORDER_FILLING_RETURN: "RETURN"}.get(filling_mode, "?")

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": sl_price,
        "tp": tp_price,
        "deviation": deviation,
        "magic": MAGIC_NUMBER,
        "comment": comment[:31],  # MT5 batasi comment maks 31 karakter
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode,
    }

    result = mt5.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        # SATU kali retry dengan harga di-refresh -- retcode requote
        # (10004)/off quotes (10021)/dst sering karena harga sudah basi
        # beberapa milidetik. Retry SEKALI SAJA (bukan loop tanpa
        # batas) -- kalau gagal lagi, terima kegagalan apa adanya,
        # "diam itu aman" (D-09) berlaku juga di eksekusi order.
        time.sleep(0.5)
        tick2 = mt5.symbol_info_tick(symbol)
        if tick2 is not None:
            request["price"] = tick2.ask if is_buy else tick2.bid
            result = mt5.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        retcode = result.retcode if result else None
        rcomment = result.comment if result else \
            f"order_send_returned_none (last_error={mt5.last_error()})"
        return {**base_fail, "reason": f"order_send_failed_retcode_{retcode}",
                "retcode": retcode, "comment": rcomment,
                "filling_mode_used": filling_name}

    return {
        "success": True, "reason": None,
        "ticket": result.order, "fill_price": result.price,
        "retcode": result.retcode, "comment": result.comment,
        "filling_mode_used": filling_name,
    }


if __name__ == "__main__":
    print("=" * 60)
    print("TEST: order_execution.py (Fase 6, Step 4)")
    print("=" * 60)
    print("[PERINGATAN KERAS] Ini akan mengirim ORDER SUNGGUHAN ke MT5")
    print("(volume MINIMAL broker, akun DEMO Exness Trial). Order akan")
    print("LANGSUNG ditutup lagi otomatis setelah terkonfirmasi terbuka --")
    print("ini murni test plumbing order_send, bukan niat trading.")
    confirm = input("\nLanjutkan kirim order sungguhan? (ketik 'ya' utk lanjut): ")
    if confirm.strip().lower() != "ya":
        print("Dibatalkan.")
        sys.exit(0)

    config = load_config()
    if not mt5c.connect():
        sys.exit(1)

    symbol = mt5c.resolve_symbol("EURUSD", config)
    sym_info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)

    lot = sym_info.volume_min  # WAJIB volume minimum untuk test
    entry_est = tick.ask
    sl_test = round(entry_est - 0.0050, 5)   # 50 pip, jauh -- test plumbing murni
    tp_test = round(entry_est + 0.0050, 5)

    print(f"\n[PROSES] Kirim BUY {symbol} lot={lot} SL={sl_test} TP={tp_test}...")
    result = send_market_order(
        pair="EURUSD", symbol=symbol, direction="bullish", lot=lot,
        sl_price=sl_test, tp_price=tp_test, config=config,
        comment="test_order_execution",
    )

    print(f"\nHasil: {result}")

    if not result["success"]:
        print(f"\n[GAGAL] {result['reason']}")
        mt5c.shutdown()
        sys.exit(1)

    print(f"\n[OK] Order sukses. Ticket={result['ticket']} "
          f"fill_price={result['fill_price']} "
          f"filling_mode={result['filling_mode_used']}")

    # Tutup lagi SEGERA -- ini murni test plumbing, bukan niat trading.
    print("\n[PROSES] Menutup posisi test...")
    from exit_logic import close_position
    close_result = close_position(symbol, result["ticket"])
    print(f"Hasil close: {close_result}")

    if not close_result["success"]:
        print(f"\n[GAGAL KRITIS] Posisi test TIDAK berhasil ditutup otomatis! "
              f"TUTUP MANUAL SEGERA via terminal MT5 -- "
              f"ticket={result['ticket']}, symbol={symbol}")
        mt5c.shutdown()
        sys.exit(1)

    print(f"\n[OK] Posisi test berhasil ditutup.")

    mt5c.shutdown()
    print("\n[SELESAI] order_execution.py -- plumbing order tervalidasi.")
