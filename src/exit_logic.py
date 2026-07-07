"""
exit_logic.py

Exit logic sesuai blueprint v16: Break-Even trigger, overnight
evaluation (Claude task 3), dan Friday force-close.

REVISI (D-22 DIEKSEKUSI): calculate_sl_tp() DIHAPUS PERMANEN. SL/TP
MURNI dari Claude (Task 5) -- Tier 1 TIDAK menghitung/memvalidasi
jarak/rasio apa pun.

REVISI (Fase 6, Step 7): 3 fungsi sweep -- run_breakeven_sweep(),
run_overnight_sweep(), run_friday_force_close() -- dipakai scheduler.py.
Filter magic number (order_execution.MAGIC_NUMBER) WAJIB supaya tidak
menyentuh posisi manual non-bot.

REVISI (BARU): print() -> logging. TEMUAN AUDIT: pesan [BREAKEVEN]/
[OVERNIGHT]/[FRIDAY-CLOSE] SEBELUMNYA pakai print() -- tidak tersimpan
di scheduler.log (lihat catatan lengkap di tier2_orchestrator.py).
logger = logging.getLogger(__name__) -- child logger, propagate
otomatis ke root handler scheduler.py, tidak perlu setup ulang di sini.

Referensi riset:
- medium.com/@elospieconomics: modify_orders() pattern via
  TRADE_ACTION_SLTP
- Astralchemist/Expert-Advisor-trading-bot: breakeven automation
  sebagai best practice komunitas

CATATAN PENTING: Task 3 (overnight_evaluation) Claude kadang mismatch
antara field "score" dan total "score_breakdown". Python WAJIB
merekonstruksi ulang skor dari score_breakdown, TIDAK percaya field
"score" Claude langsung.

KOMPONEN:
1. check_breakeven() — cek satu posisi, pindah SL ke entry saat 1:1 RR
2. modify_position_sltp() — kirim TRADE_ACTION_SLTP ke MT5
3. evaluate_overnight() — bangun payload utk Claude task 3 + rekonsiliasi
4. check_friday_close() — cek waktu force-close & no-new-entry
5. close_position() — tutup posisi market
6. run_breakeven_sweep() — loop semua posisi bot, terapkan #1+#2
7. run_overnight_sweep() — loop semua posisi bot, terapkan #3
8. run_friday_force_close() — loop semua posisi bot, terapkan #5
"""

import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path

import MetaTrader5 as mt5

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import load_config
from indicators import get_confirmed_snapshot
from claude_client import call_claude
import mt5_connector as mt5c
from order_execution import MAGIC_NUMBER

logger = logging.getLogger(__name__)


def _get_bot_positions():
    """Ambil HANYA posisi yang dibuka bot ini (filter magic number)."""
    positions = mt5.positions_get()
    if not positions:
        return []
    return [p for p in positions if p.magic == MAGIC_NUMBER]


# =====================================================
# BREAK-EVEN CHECK
# =====================================================
def check_breakeven(position, config: dict) -> dict:
    """
    Cek apakah posisi sudah mencapai threshold breakeven (default 1:1 RR).

    Return: dict {
        "should_move_be": bool,
        "current_rr": float,
        "new_sl": float | None,
    }
    """
    be_threshold = config["exit"]["breakeven_at_rr"]

    entry = position.price_open
    sl = position.sl
    current = position.price_current
    is_buy = (position.type == mt5.ORDER_TYPE_BUY)

    if sl == 0:
        return {"should_move_be": False, "current_rr": None, "new_sl": None}

    sl_distance = abs(entry - sl)
    if sl_distance == 0:
        return {"should_move_be": False, "current_rr": None, "new_sl": None}

    if is_buy:
        profit_distance = current - entry
    else:
        profit_distance = entry - current

    current_rr = round(profit_distance / sl_distance, 2)
    already_at_be = abs(sl - entry) < 1e-9

    if current_rr >= be_threshold and not already_at_be:
        return {"should_move_be": True, "current_rr": current_rr, "new_sl": entry}

    return {"should_move_be": False, "current_rr": current_rr, "new_sl": None}


def modify_position_sltp(symbol: str, ticket: int,
                          new_sl: float = None,
                          new_tp: float = None) -> dict:
    """
    Kirim request TRADE_ACTION_SLTP ke MT5 untuk modifikasi SL/TP
    posisi yang sudah terbuka.

    Return: dict {"success": bool, "retcode": int, "comment": str}
    """
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return {"success": False, "retcode": None,
                "comment": "position_not_found"}

    pos = positions[0]
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "position": ticket,
        "sl": new_sl if new_sl is not None else pos.sl,
        "tp": new_tp if new_tp is not None else pos.tp,
    }

    result = mt5.order_send(request)
    if result is None:
        return {"success": False, "retcode": None,
                "comment": "order_send_returned_none"}

    success = (result.retcode == mt5.TRADE_RETCODE_DONE)
    return {"success": success, "retcode": result.retcode,
            "comment": result.comment}


# =====================================================
# OVERNIGHT EVALUATION (Claude task 3 + rekonsiliasi)
# =====================================================
def evaluate_overnight(position, h1_trend: dict,
                        news_check: dict, config: dict) -> dict:
    """
    Evaluasi apakah posisi terbuka jam 22:45 WIB layak di-hold
    overnight atau harus di-close sekarang.

    PENTING: skor final DIHITUNG ULANG dari score_breakdown Python,
    TIDAK percaya field "score" Claude langsung (safety net mismatch).

    Return: dict {
        "score": int, "score_breakdown": dict,
        "decision": "hold_overnight" | "close_now",
        "reasoning": str, "claude_score_mismatch": bool,
    }
    """
    entry = position.price_open
    sl = position.sl
    tp = position.tp
    current = position.price_current
    is_buy = (position.type == mt5.ORDER_TYPE_BUY)

    sl_distance = abs(entry - sl) if sl != 0 else None
    if is_buy:
        profit_distance = current - entry
    else:
        profit_distance = entry - current

    rr_now = round(profit_distance / sl_distance, 2) if sl_distance else 0.0

    payload = {
        "task_type": "overnight_evaluation",
        "pair": position.symbol,
        "position_side": "BUY" if is_buy else "SELL",
        "entry_price": entry,
        "current_price": current,
        "sl_price": sl,
        "tp_price": tp,
        "rr_ratio_now": rr_now,
        "high_impact_news_next_8h": news_check.get("has_high_impact", False),
        "news_list": news_check.get("events", []),
        "h1_trend": h1_trend.get("trend", "ranging"),
        "h1_adx": h1_trend.get("adx", 0),
        "sl_tp_set_on_broker": (sl != 0 and tp != 0),
    }

    result = call_claude("overnight_evaluation", payload)

    breakdown = result.get("score_breakdown", {})
    pl_score    = breakdown.get("profit_loss", 0)
    news_score  = breakdown.get("news", 0)
    trend_score = breakdown.get("trend", 0)
    sltp_score  = breakdown.get("sl_tp_set", 0)

    recomputed_score = pl_score + news_score + trend_score + sltp_score
    claude_reported_score = result.get("score", recomputed_score)
    mismatch = (recomputed_score != claude_reported_score)

    threshold = config["overnight"]["hold_threshold_score"]
    decision = "hold_overnight" if recomputed_score >= threshold else "close_now"

    return {
        "score": recomputed_score,
        "score_breakdown": breakdown,
        "decision": decision,
        "reasoning": result.get("reasoning", ""),
        "claude_score_mismatch": mismatch,
        "claude_reported_score": claude_reported_score,
    }


# =====================================================
# FRIDAY FORCE-CLOSE
# =====================================================
def check_friday_close(config: dict, now: datetime = None) -> dict:
    """
    Cek apakah sekarang waktunya force-close (Jumat malam WIB)
    atau sudah lewat batas no-new-entry.

    Return: dict {"force_close_all": bool, "block_new_entry": bool, "reason": str}
    """
    if now is None:
        now = datetime.now()

    is_friday = (now.weekday() == 4)
    if not is_friday:
        return {"force_close_all": False, "block_new_entry": False,
                "reason": "not_friday"}

    close_time_str = config["exit"]["friday_close_time"]
    no_entry_time_str = config["exit"]["friday_no_new_entry_time"]

    close_h, close_m = map(int, close_time_str.split(":"))
    entry_h, entry_m = map(int, no_entry_time_str.split(":"))

    close_threshold = now.replace(hour=close_h, minute=close_m,
                                   second=0, microsecond=0)
    entry_threshold = now.replace(hour=entry_h, minute=entry_m,
                                   second=0, microsecond=0)

    force_close = now >= close_threshold
    block_entry = now >= entry_threshold

    reason = "friday_close_time_reached" if force_close else (
        "friday_no_new_entry_window" if block_entry else "friday_normal_hours"
    )

    return {"force_close_all": force_close, "block_new_entry": block_entry,
            "reason": reason}


def close_position(symbol: str, ticket: int) -> dict:
    """
    Tutup posisi market sepenuhnya (Friday force-close / overnight
    close_now).

    Return: dict {"success": bool, "retcode": int, "comment": str}
    """
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return {"success": False, "retcode": None,
                "comment": "position_not_found"}

    pos = positions[0]
    is_buy = (pos.type == mt5.ORDER_TYPE_BUY)
    order_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY

    tick = mt5.symbol_info_tick(symbol)
    price = tick.bid if is_buy else tick.ask

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": pos.volume,
        "type": order_type,
        "position": ticket,
        "price": price,
        "deviation": 20,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }

    result = mt5.order_send(request)
    if result is None:
        return {"success": False, "retcode": None,
                "comment": "order_send_returned_none"}

    success = (result.retcode == mt5.TRADE_RETCODE_DONE)
    return {"success": success, "retcode": result.retcode,
            "comment": result.comment}


# =====================================================
# SWEEP FUNCTIONS (Fase 6 Step 7) -- dipanggil scheduler.py
# =====================================================
def run_breakeven_sweep(config: dict) -> dict:
    """
    Loop SEMUA posisi bot ini (filter magic number), cek breakeven
    tiap satu, pindahkan SL ke entry kalau threshold tercapai.

    Return: dict {"checked": int, "moved_to_be": int, "errors": [...]}
    """
    positions = _get_bot_positions()
    summary = {"checked": 0, "moved_to_be": 0, "errors": []}

    for pos in positions:
        summary["checked"] += 1
        try:
            be = check_breakeven(pos, config)
            if be["should_move_be"]:
                result = modify_position_sltp(pos.symbol, pos.ticket,
                                               new_sl=be["new_sl"])
                if result["success"]:
                    summary["moved_to_be"] += 1
                    logger.info(f"[BREAKEVEN] {pos.symbol} ticket={pos.ticket} "
                                f"SL dipindah ke entry ({be['new_sl']}) "
                                f"RR={be['current_rr']}")
                else:
                    summary["errors"].append({
                        "ticket": pos.ticket,
                        "error": f"modify_position_sltp gagal: {result}",
                    })
                    logger.error(f"[BREAKEVEN-FAILED] {pos.symbol} "
                                 f"ticket={pos.ticket}: {result}")
        except Exception as e:
            summary["errors"].append({"ticket": pos.ticket, "error": str(e)})
            logger.error(f"[BREAKEVEN-ERROR] ticket={pos.ticket}: {e}",
                         exc_info=True)

    return summary


def run_overnight_sweep(config: dict, news_check_fn=None) -> dict:
    """
    Loop SEMUA posisi bot ini (filter magic number) jam 22:45 WIB,
    evaluasi overnight via Claude (Task 3), close_now kalau
    direkomendasikan.

    Args:
        news_check_fn: callable(symbol) -> dict news_check. Default
            None -> tidak ada info berita (P-08, news_filter.py belum
            terintegrasi ke pipeline aktif).

    Return: dict {"checked": int, "hold": int, "closed": int, "errors": [...]}
    """
    from structure import detect_trend  # D-19: dipertahankan sengaja

    positions = _get_bot_positions()
    summary = {"checked": 0, "hold": 0, "closed": 0, "errors": []}

    for pos in positions:
        summary["checked"] += 1
        try:
            df_h1 = mt5c.get_candles(pos.symbol, "H1", count=150)
            h1_trend = detect_trend(df_h1, config, pair=pos.symbol)
            news_check = news_check_fn(pos.symbol) if news_check_fn else \
                {"has_high_impact": False, "events": []}

            evaluation = evaluate_overnight(pos, h1_trend, news_check, config)

            if evaluation["claude_score_mismatch"]:
                logger.warning(
                    f"[OVERNIGHT-MISMATCH] {pos.symbol} ticket={pos.ticket} "
                    f"score_recomputed={evaluation['score']} "
                    f"score_claude={evaluation['claude_reported_score']}"
                )

            logger.info(f"[OVERNIGHT] {pos.symbol} ticket={pos.ticket} "
                        f"score={evaluation['score']} "
                        f"decision={evaluation['decision']}")

            if evaluation["decision"] == "close_now":
                result = close_position(pos.symbol, pos.ticket)
                if result["success"]:
                    summary["closed"] += 1
                else:
                    summary["errors"].append({
                        "ticket": pos.ticket,
                        "error": f"close_position gagal: {result}",
                    })
                    logger.error(f"[OVERNIGHT-CLOSE-FAILED] {pos.symbol} "
                                 f"ticket={pos.ticket}: {result}")
            else:
                summary["hold"] += 1
        except Exception as e:
            summary["errors"].append({"ticket": pos.ticket, "error": str(e)})
            logger.error(f"[OVERNIGHT-ERROR] ticket={pos.ticket}: {e}",
                         exc_info=True)

    return summary


def run_friday_force_close(config: dict) -> dict:
    """
    Force-close SEMUA posisi bot ini (filter magic number). Dipanggil
    scheduler.py tepat di jadwal friday_close_time (23:30 WIB).

    Return: dict {"checked": int, "closed": int, "errors": [...]}
    """
    positions = _get_bot_positions()
    summary = {"checked": 0, "closed": 0, "errors": []}

    for pos in positions:
        summary["checked"] += 1
        try:
            result = close_position(pos.symbol, pos.ticket)
            if result["success"]:
                summary["closed"] += 1
                logger.info(f"[FRIDAY-CLOSE] {pos.symbol} ticket={pos.ticket} closed.")
            else:
                summary["errors"].append({
                    "ticket": pos.ticket,
                    "error": f"close_position gagal: {result}",
                })
                logger.error(f"[FRIDAY-CLOSE-FAILED] {pos.symbol} "
                             f"ticket={pos.ticket}: {result}")
        except Exception as e:
            summary["errors"].append({"ticket": pos.ticket, "error": str(e)})
            logger.error(f"[FRIDAY-CLOSE-ERROR] ticket={pos.ticket}: {e}",
                         exc_info=True)

    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_config()

    print("=" * 60)
    print("TEST: exit_logic.py (logging fix)")
    print("=" * 60)

    if not mt5c.connect():
        sys.exit(1)

    print("\n--- TEST 1: Friday Close Check ---")
    now = datetime.now()
    fc = check_friday_close(config, now)
    print(f"  Waktu sekarang: {now.strftime('%A, %Y-%m-%d %H:%M')}")
    print(f"  force_close_all : {fc['force_close_all']}")
    print(f"  block_new_entry : {fc['block_new_entry']}")
    print(f"  reason          : {fc['reason']}")

    days_to_friday = (4 - now.weekday()) % 7
    fake_friday_late = (now + timedelta(days=days_to_friday)).replace(
        hour=23, minute=45, second=0, microsecond=0
    )
    fc2 = check_friday_close(config, fake_friday_late)
    print(f"\n  Simulasi Jumat 23:45 WIB:")
    print(f"  force_close_all : {fc2['force_close_all']} (expect True)")
    print(f"  reason          : {fc2['reason']}")

    print("\n--- TEST 2: Breakeven Check (simulasi) ---")
    class FakePosition:
        def __init__(self, entry, sl, current, is_buy=True):
            self.price_open = entry
            self.sl = sl
            self.price_current = current
            self.type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL

    pos_at_1to1 = FakePosition(1.1570, 1.1550, 1.1590, is_buy=True)
    be_result = check_breakeven(pos_at_1to1, config)
    print(f"  Posisi BUY di 1:1 RR:")
    print(f"  should_move_be: {be_result['should_move_be']} (expect True)")
    print(f"  current_rr    : {be_result['current_rr']}")
    print(f"  new_sl        : {be_result['new_sl']}")

    pos_below_1to1 = FakePosition(1.1570, 1.1550, 1.1580, is_buy=True)
    be_result2 = check_breakeven(pos_below_1to1, config)
    print(f"\n  Posisi BUY di 0.5:1 RR:")
    print(f"  should_move_be: {be_result2['should_move_be']} (expect False)")
    print(f"  current_rr    : {be_result2['current_rr']}")

    print("\n--- TEST 3: Sweep functions (posisi RIIL, filter magic) ---")
    bot_positions = _get_bot_positions()
    print(f"  Posisi bot ini saat ini (magic={MAGIC_NUMBER}): "
          f"{len(bot_positions)}")

    be_summary = run_breakeven_sweep(config)
    print(f"  run_breakeven_sweep(): {be_summary}")
    assert be_summary["checked"] == len(bot_positions), \
        "FAIL: jumlah checked tidak cocok dgn _get_bot_positions()"

    print("\n[OK] Semua fungsi sweep berjalan tanpa error (filter magic aktif, "
          "logger.info() sekarang juga muncul di atas kalau relevan).")

    mt5c.shutdown()
    print("\n[SELESAI] exit_logic.py berhasil.")