"""
position_monitor.py - Fase 6, Step 6

Deteksi closure posisi LIVE (Fase 6, execution.enabled=true) dan
update trade_log + risk counter. Analog paper_outcome_tracker.py
(Fase 5), tapi untuk posisi MT5 SUNGGUHAN -- bukan simulasi candle M1.

METODOLOGI (riset, referensi resmi MQL5):
- Bandingkan trade_log status='open' (risk_engine.get_open_trades())
  terhadap mt5.positions_get() -- kalau ticket TIDAK ADA lagi di
  positions_get(), posisi sudah closed.
- Ambil deal closing via mt5.history_deals_get(position=ticket),
  filter entry==DEAL_ENTRY_OUT.
- Klasifikasi hasil (TP/SL/manual) via deal.reason -- CARA RESMI
  (forum resmi MQL5, mql5.com/en/forum/457565), BUKAN menebak dari
  harga.
- pnl_pct dihitung dari deal.profit dibagi balance SAAT INI (bukan
  balance saat posisi dibuka -- keterbatasan yang didokumentasikan,
  bukan disembunyikan).

REVISI (BARU): print() -> logging. TEMUAN AUDIT: pesan [CLOSED]
SEBELUMNYA pakai print() -- tidak tersimpan di scheduler.log (lihat
catatan lengkap di tier2_orchestrator.py). logger = logging.getLogger
(__name__) -- child logger, propagate otomatis ke root handler
scheduler.py.

Dijadwalkan tiap 5 menit di scheduler.py.
"""

import sys
import logging
from pathlib import Path
from datetime import datetime

import MetaTrader5 as mt5

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import load_config
import mt5_connector as mt5c
from risk_engine import get_open_trades, log_trade_close

logger = logging.getLogger(__name__)


def _classify_closure(ticket: int) -> dict:
    """
    Ambil deal closing utk `ticket`, klasifikasi TP/SL/manual via
    deal.reason, hitung pnl_pct dari deal.profit.

    Return: dict {"result", "pnl_pct", "close_price", "close_time"}
            atau None kalau deal closing belum ditemukan (race
            condition -- retry run berikutnya).
    """
    deals = mt5.history_deals_get(position=ticket)
    if not deals:
        return None

    closing_deals = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
    if not closing_deals:
        return None

    closing_deal = sorted(closing_deals, key=lambda d: d.time)[-1]

    reason_map = {
        mt5.DEAL_REASON_SL: "SL",
        mt5.DEAL_REASON_TP: "TP",
    }
    result = reason_map.get(closing_deal.reason, "manual")

    acc = mt5.account_info()
    balance = acc.balance if acc and acc.balance > 0 else 1.0
    pnl_pct = round((closing_deal.profit / balance) * 100, 4)

    return {
        "result": result,
        "pnl_pct": pnl_pct,
        "close_price": closing_deal.price,
        "close_time": str(datetime.fromtimestamp(closing_deal.time)),
    }


def check_closed_positions() -> dict:
    """
    Bandingkan trade_log 'open' vs positions_get(), update yang sudah
    closed via log_trade_close().

    Return: dict {"checked", "still_open", "closed_tp", "closed_sl",
                   "closed_manual", "not_found_in_history", "errors"}
    """
    open_trades = get_open_trades()
    live_tickets = {p.ticket for p in (mt5.positions_get() or [])}

    summary = {"checked": 0, "still_open": 0, "closed_tp": 0,
               "closed_sl": 0, "closed_manual": 0,
               "not_found_in_history": 0, "errors": []}

    for trade in open_trades:
        summary["checked"] += 1
        ticket = trade["mt5_ticket"]

        if ticket is None:
            summary["errors"].append({"trade_id": trade["id"],
                                       "error": "mt5_ticket_kosong"})
            logger.error(f"[POSITION-MONITOR-ERROR] trade_id={trade['id']} "
                         f"tidak punya mt5_ticket -- tidak bisa dicocokkan.")
            continue

        if ticket in live_tickets:
            summary["still_open"] += 1
            continue

        try:
            closure = _classify_closure(ticket)
        except Exception as e:
            summary["errors"].append({"trade_id": trade["id"],
                                       "ticket": ticket, "error": str(e)})
            logger.error(f"[POSITION-MONITOR-ERROR] trade_id={trade['id']} "
                         f"ticket={ticket}: {e}", exc_info=True)
            continue

        if closure is None:
            summary["not_found_in_history"] += 1
            logger.warning(f"[POSITION-MONITOR] trade_id={trade['id']} "
                            f"ticket={ticket} tidak ada di positions_get() "
                            f"tapi deal closing belum ketemu -- retry run berikutnya.")
            continue

        log_trade_close(trade["id"], closure["result"], closure["pnl_pct"])

        if closure["result"] == "TP":
            summary["closed_tp"] += 1
        elif closure["result"] == "SL":
            summary["closed_sl"] += 1
        else:
            summary["closed_manual"] += 1

        logger.info(f"[CLOSED] trade_id={trade['id']} ticket={ticket} "
                    f"{trade['pair']} result={closure['result']} "
                    f"pnl_pct={closure['pnl_pct']}% "
                    f"close_price={closure['close_price']}")

    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=" * 60)
    print("TEST: position_monitor.py (logging fix)")
    print("=" * 60)

    config = load_config()
    if not mt5c.connect():
        sys.exit(1)

    result = check_closed_positions()
    print(f"\nRingkasan: {result}")

    if result["checked"] == 0:
        print("\n[INFO] checked=0 -- BENAR, bukan bug kalau belum ada posisi "
              "live dengan mt5_ticket terisi.")

    mt5c.shutdown()
    print("\n[SELESAI] position_monitor.py berhasil.")