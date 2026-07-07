"""
tier2_orchestrator.py - Fase 4 (risk wiring) + Fase 5 (Learning Ledger) + Fase 6 (order eksekusi)

Ini "lem" yang menyambungkan semua modul:
  screener.py            -> temukan kandidat (gate terbuka)
  package_builder.py     -> rakit paket lengkap + chart + simpan ke DB
  claude_client.py       -> kirim paket ke Claude (Tier 2)
  risk_engine.py         -> gate risiko MEKANIS (circuit breaker + exposure + lot)
  zone_state.py          -> catat verdict (cooldown/throttle/execute)
  package_log.py         -> update baris paket dengan verdict final
  paper_outcome_tracker.py -> catat trade paper utk Learning Ledger (Fase 5)
  order_execution.py     -> kirim order MT5 sungguhan (Fase 6)

=============================================================================
REVISI (BARU): print() -> logging
=============================================================================
TEMUAN AUDIT: semua pesan [SKIP]/[EXECUTE]/[ORDER-SENT]/[CIRCUIT BREAKER]/dst
SEBELUMNYA pakai print() biasa -- tidak pernah tersimpan di scheduler.log
karena print() menulis langsung ke stdout, melewati seluruh sistem
logging yang dikonfigurasi scheduler.py (FileHandler+StreamHandler).
Akibatnya: setelah scheduler.py jalan berjam-jam tanpa diawasi, TIDAK ADA
jejak SKIP/EXECUTE tersimpan sama sekali di file log -- cuma ringkasan
kasar "X hasil across Y pair" dari job wrapper.

FIX: logger = logging.getLogger(__name__) (child logger, otomatis
propagate ke root handler yang dikonfigurasi scheduler.py -- TIDAK perlu
setup ulang di sini). Semua print() diganti logger.info()/logger.warning()/
logger.error() sesuai tingkat keparahan. __main__ tetap panggil
logging.basicConfig() sendiri supaya output tetap terlihat di konsol
saat file ini dijalankan MANDIRI (bukan lewat scheduler.py).

=============================================================================
REVISI: INTEGRASI risk_engine.py (menutup disconnection Fase 4)
=============================================================================
risk_engine adalah lapisan MEKANIS (organ), bukan penilaian (otak) -- jadi
tidak pernah dikirim ke Claude, hanya dijalankan di orchestrator. Dua titik:

  TITIK 1 -- CIRCUIT BREAKER (sebelum panggil Claude):
    Gate portofolio global. Kalau daily/weekly/monthly limit kena, TIDAK ada
    gunanya screening/membakar token Claude. Dicek di awal run_tier2_for_pair
    (skip pair) + guard tipis di awal evaluate_candidate (halt akumulasi dalam
    satu siklus -- penting saat Fase 6 sudah bisa eksekusi di tengah siklus).

  TITIK 2 -- EXECUTION GATE (setelah verdict 'execute' dari Claude):
    Harus SESUDAH Claude karena sl_price (untuk lot sizing) datang dari Claude,
    dan risk_pct bergantung posisi terbuka. Urutan:
      is_level_blacklisted -> get_risk_pct_for_group(count_open_group_a)
      -> pre_entry_check -> calculate_lot
    Jika gate menolak: verdict Claude TETAP dicatat jujur ('execute'), tapi
    posisi TIDAK ditandai open. Outcome = 'execute_blocked_by_risk'.

=============================================================================
REVISI (Fase 6, Step 5): order_execution.py diwire di titik TODO Fase 6
=============================================================================
Urutan WAJIB: kirim order DULU (order_execution.send_market_order()),
log_trade_open() HANYA kalau order SUKSES. Kalau order gagal (market
closed, requote 2x gagal, dst), verdict Claude & risk gate SUDAH lolos
tapi eksekusi sungguhan gagal di titik terakhir -- dicatat di
package_log sebagai jejak audit (outcome='order_failed'), TIDAK ada
baris trade_log (tidak ada posisi nyata yang perlu di-track).

entry_price yang disimpan ke trade_log untuk Fase 6 LIVE adalah
fill_price SUNGGUHAN dari order_execution (symbol_info_tick saat
order_send dieksekusi), BUKAN gate['entry_price'] yang cuma proxy
candle[-2] close (dipakai HANYA untuk lot sizing sebelum order dikirim).

FASE-AWARE (flag config execution.enabled, default False):
  - Fase 4/5 (execution.enabled=False, PAPER): circuit breaker + blacklist +
    pre_entry_check + calculate_lot AKTIF (read-only). log_trade_open() dan
    kirim order DITAHAN. SEBAGAI GANTINYA: dicatat ke paper_trade_outcomes
    (Fase 5) via paper_outcome_tracker.py.
  - Fase 6 (execution.enabled=True): order_execution.send_market_order()
    dikirim sungguhan, log_trade_open() dgn mt5_ticket + fill_price asli.
    position_monitor.py yang mendeteksi closure & panggil
    log_trade_close()+add_loss().

Internal risk_engine.py/paper_outcome_tracker.py/order_execution.py
TIDAK diubah logikanya -- hanya dipanggil.

PRINSIP (v2.1 Bab 0.1): orchestrator HANYA menyalurkan & menjalankan aturan
mekanis. Penilaian kualitas 100% milik Claude (Titik 3). Risiko 100% aturan.
"""

import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config_loader import load_config, get_all_pairs
import mt5_connector as mt5c
from screener import run_screener_for_pair
from package_builder import build_tier2_package
from claude_client import call_claude, load_image_as_base64
import zone_state as zs
import package_log as pkglog

# --- risk engine (dipanggil, internalnya TIDAK diubah logikanya) ---
from risk_engine import (init_risk_db, cleanup_blacklist,
                          check_circuit_breaker, get_risk_pct_for_group,
                          pre_entry_check, calculate_lot,
                          is_level_blacklisted, count_open_group_a,
                          log_trade_open)

# --- Fase 5: paper outcome tracker (Learning Ledger prasyarat) ---
import paper_outcome_tracker as pot

# --- Fase 6: eksekusi order sungguhan ---
from order_execution import send_market_order

import anthropic

logger = logging.getLogger(__name__)


# =====================================================
# HELPER: flag eksekusi (fase-aware)
# =====================================================
def _execution_enabled(config: dict) -> bool:
    """Fase 6 baru True. Sebelum itu paper-mode (tidak menulis trade_log,
    tidak kirim order)."""
    return bool(config.get("execution", {}).get("enabled", False))


# =====================================================
# EXECUTION GATE (Titik 2) -- dijalankan SETELAH Claude bilang 'execute'
# =====================================================
def _apply_execution_gate(candidate: dict, pair: str, symbol: str,
                           response: dict, config: dict) -> dict:
    """
    Jalankan gate risiko MEKANIS terhadap verdict 'execute' Claude.

    Return: dict {
        "allowed": bool,
        "reason": str | None,        # alasan blokir kalau allowed=False
        "risk_pct": float | None,
        "entry_price": float | None,
        "sl_price": float | None,
        "tp_price": float | None,
        "lot": float | None,
        "pre_entry": dict | None,    # detail pre_entry_check
    }
    """
    base = {"allowed": False, "reason": None, "risk_pct": None,
            "entry_price": None, "sl_price": None, "tp_price": None,
            "lot": None, "pre_entry": None}

    sl_price = response.get("sl_price")
    tp_price = response.get("tp_price")

    if sl_price is None:
        return {**base, "reason": "execute_tanpa_sl_price_dari_claude"}

    entry_price = candidate["current_price"]

    zone_center = (candidate["zone_top"] + candidate["zone_bottom"]) / 2
    pair_clean = pair.replace("m", "").replace(".raw", "").upper()
    if is_level_blacklisted(pair_clean, zone_center):
        return {**base, "reason": "level_blacklisted_hari_ini",
                "entry_price": entry_price, "sl_price": sl_price,
                "tp_price": tp_price}

    open_a = count_open_group_a(config)
    risk_pct = get_risk_pct_for_group(pair, config, open_a)

    pre_entry = pre_entry_check(risk_pct, config)
    if not pre_entry["allowed"]:
        return {**base, "reason": "total_exposure_exceeded",
                "risk_pct": risk_pct, "entry_price": entry_price,
                "sl_price": sl_price, "tp_price": tp_price,
                "pre_entry": pre_entry}

    lot = calculate_lot(symbol, entry_price, sl_price, risk_pct, config)

    return {"allowed": True, "reason": None, "risk_pct": risk_pct,
            "entry_price": entry_price, "sl_price": sl_price,
            "tp_price": tp_price, "lot": lot, "pre_entry": pre_entry}


def evaluate_candidate(candidate: dict, pair: str, symbol: str,
                        df_structure, df_timing, all_zones: list,
                        config: dict) -> dict:
    """
    Evaluasi SATU kandidat: (circuit breaker) -> rakit paket -> kirim Claude
    -> (execution gate) -> (order eksekusi jika Fase 6) -> proses verdict.

    Return: dict {
        "outcome": "execute" | "execute_blocked_by_risk" | "order_failed"
                   | "skip" | "api_failed" | "circuit_breaker",
        "log_id": int | None,
        "claude_response": dict | None,
        "risk": dict | None,
    }
    """
    zone_id = candidate["zone_id"]

    cb = check_circuit_breaker(config)
    if not cb["trading_allowed"]:
        logger.warning(f"[CIRCUIT BREAKER] {pair} {candidate['zone_type']} "
                        f"{candidate['direction']} -- blocked_by={cb['blocked_by']} "
                        f"(zone {zone_id} tidak dikirim ke Claude)")
        return {"outcome": "circuit_breaker", "log_id": None,
                "claude_response": None, "risk": {"circuit_breaker": cb}}

    package = build_tier2_package(candidate, pair, symbol, df_structure,
                                   df_timing, all_zones, config)
    log_id = package["log_id"]

    text_payload = {k: v for k, v in package.items()
                     if k not in ("charts", "log_id")}

    try:
        images = [
            load_image_as_base64(package["charts"]["h1_chart_path"], "H1_chart"),
            load_image_as_base64(package["charts"]["m5_chart_path"], "M5_chart"),
        ]
        response = call_claude("final_visual_review", text_payload,
                                images=images, timeout=20.0)
    except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
        logger.error(f"[GAGAL] API timeout/gagal utk zone {zone_id}: {e}")
        return {"outcome": "api_failed", "log_id": log_id,
                "claude_response": None, "risk": None}
    except ValueError as e:
        logger.error(f"[ERROR] Response Claude invalid utk zone {zone_id}: {e}")
        return {"outcome": "api_failed", "log_id": log_id,
                "claude_response": None, "risk": None}

    final_decision = response.get("final_decision")
    confidence = response.get("confidence")
    reasoning = response.get("additional_observations", "")

    if final_decision == "execute":
        gate = _apply_execution_gate(candidate, pair, symbol, response, config)

        if not gate["allowed"]:
            pkglog.update_verdict(log_id, "execute", confidence,
                                   reasoning + f" [RISK_BLOCKED: {gate['reason']}]")
            logger.warning(f"[EXECUTE-BLOCKED] {pair} {candidate['zone_type']} "
                            f"{candidate['direction']} conf={confidence} "
                            f"-> risk_gate: {gate['reason']}")
            return {"outcome": "execute_blocked_by_risk", "log_id": log_id,
                    "claude_response": response, "risk": gate}

        zs.record_execute_verdict(zone_id, confidence, reasoning)
        zs.set_open_position(zone_id, True)
        pkglog.update_verdict(log_id, "execute", confidence, reasoning)

        if _execution_enabled(config):
            order_result = send_market_order(
                pair=pair, symbol=symbol, direction=candidate["direction"],
                lot=gate["lot"], sl_price=gate["sl_price"],
                tp_price=gate["tp_price"], config=config,
                comment=f"claude_c{confidence}",
            )

            if not order_result["success"]:
                pkglog.update_verdict(
                    log_id, "execute", confidence,
                    reasoning + f" [ORDER_FAILED: {order_result['reason']}]"
                )
                logger.error(f"[ORDER-FAILED] {pair} {candidate['zone_type']} "
                             f"{candidate['direction']} conf={confidence} -> "
                             f"{order_result['reason']}")
                return {"outcome": "order_failed", "log_id": log_id,
                        "claude_response": response,
                        "risk": {**gate, "order_result": order_result}}

            trade_id = log_trade_open(
                pair=pair, direction=candidate["direction"],
                entry_price=order_result["fill_price"],
                sl_price=gate["sl_price"], tp_price=gate["tp_price"],
                lot=gate["lot"], risk_pct=gate["risk_pct"],
                ob_id=candidate.get("source_detail", {}).get("id"),
                mt5_ticket=order_result["ticket"],
            )
            logger.info(f"[ORDER-SENT] {pair} {candidate['zone_type']} "
                        f"{candidate['direction']} conf={confidence} "
                        f"ticket={order_result['ticket']} "
                        f"fill={order_result['fill_price']} "
                        f"SL={gate['sl_price']} TP={gate['tp_price']} "
                        f"lot={gate['lot']} filling={order_result['filling_mode_used']} "
                        f"trade_log_id={trade_id} [LIVE]")
            return {"outcome": "execute", "log_id": log_id,
                    "claude_response": response,
                    "risk": {**gate, "order_result": order_result}}

        else:
            pot.record_pending_trade(
                log_id=log_id, pair=pair, symbol=symbol,
                direction=candidate["direction"],
                entry_price=gate["entry_price"], sl_price=gate["sl_price"],
                tp_price=gate["tp_price"], risk_pct=gate["risk_pct"],
                entry_time_utc=str(df_timing["time"].iloc[-2]),
            )
            logger.info(f"[EXECUTE] {pair} {candidate['zone_type']} "
                        f"{candidate['direction']} conf={confidence} "
                        f"risk={gate['risk_pct']*100:.2f}% lot={gate['lot']} "
                        f"SL={gate['sl_price']} TP={gate['tp_price']} [PAPER]")
            return {"outcome": "execute", "log_id": log_id,
                    "claude_response": response, "risk": gate}

    elif final_decision == "skip":
        skip_reason_type = response.get("skip_reason_type", "setup_invalid")
        zs.apply_skip_cooldown(zone_id, skip_reason_type, "skip",
                                confidence, reasoning)
        pkglog.update_verdict(log_id, "skip", confidence, reasoning,
                               skip_reason_type=skip_reason_type)
        logger.info(f"[SKIP] {pair} {candidate['zone_type']} "
                    f"{candidate['direction']} conf={confidence} "
                    f"reason_type={skip_reason_type}")
        return {"outcome": "skip", "log_id": log_id,
                "claude_response": response, "risk": None}

    else:
        raise ValueError(
            f"final_decision tidak dikenali dari Claude: {final_decision!r} "
            f"(response lengkap: {response})"
        )


def run_tier2_for_pair(pair: str, config: dict) -> list:
    """
    Jalankan screener utk satu pair, evaluasi SEMUA kandidat yang gate-nya
    terbuka lewat Tier 2. Return list hasil evaluate_candidate().
    """
    cb = check_circuit_breaker(config)
    if not cb["trading_allowed"]:
        logger.warning(f"[CIRCUIT BREAKER] {pair} di-skip total -- "
                        f"blocked_by={cb['blocked_by']} "
                        f"(daily={cb['daily_loss_pct']}% weekly={cb['weekly_loss_pct']}% "
                        f"monthly={cb['monthly_loss_pct']}%)")
        return [{"outcome": "circuit_breaker", "log_id": None,
                 "claude_response": None, "risk": {"circuit_breaker": cb}}]

    symbol = mt5c.resolve_symbol(pair, config)
    tf_cfg = config.get("screener_timeframes", {})

    screener_result = run_screener_for_pair(pair, config)
    candidates = screener_result["candidates"]

    if not candidates:
        return []

    df_structure = mt5c.get_candles(
        symbol, tf_cfg.get("structure_tf", "H1"),
        count=tf_cfg.get("structure_lookback", 200))
    df_timing = mt5c.get_candles(
        symbol, tf_cfg.get("timing_tf", "M5"),
        count=tf_cfg.get("timing_lookback", 20) + 40)

    results = []
    for candidate in candidates:
        try:
            result = evaluate_candidate(candidate, pair, symbol,
                                         df_structure, df_timing,
                                         candidates, config)
        except Exception as e:
            logger.error(f"[ERROR] Kandidat {candidate.get('zone_id')} gagal "
                         f"diproses: {e}", exc_info=True)
            result = {"outcome": "error", "log_id": None,
                      "claude_response": None, "risk": None, "error": str(e)}
        results.append(result)

    return results


def run_tier2_cycle(config: dict) -> dict:
    """Jalankan Tier 2 utk SEMUA pair. Return dict {pair: [hasil, ...]}."""
    pairs = get_all_pairs(config)
    all_results = {}
    for pair in pairs:
        try:
            all_results[pair] = run_tier2_for_pair(pair, config)
        except Exception as e:
            logger.error(f"[ERROR] Pair {pair} gagal diproses total: {e}",
                         exc_info=True)
            all_results[pair] = []
    return all_results


if __name__ == "__main__":
    # basicConfig HANYA dipanggil saat file ini dijalankan MANDIRI
    # (bukan lewat scheduler.py yang sudah konfigurasi logging sendiri) --
    # supaya output logger tetap terlihat di konsol untuk testing manual.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_config()

    print("=" * 60)
    print("TEST: tier2_orchestrator.py (Fase 4 + 5 + 6 -- logging fix)")
    print("=" * 60)

    if not mt5c.connect():
        sys.exit(1)

    from orderblock import init_db as init_ob_db
    init_ob_db()
    zs.init_db()
    pkglog.init_db()
    init_risk_db()
    cleanup_blacklist()
    pot.init_db()

    mode = "LIVE" if _execution_enabled(config) else "PAPER"
    print(f"[MODE EKSEKUSI] {mode} "
          f"(execution.enabled={_execution_enabled(config)})")

    all_results = run_tier2_cycle(config)

    print(f"\n{'='*60}")
    print("RINGKASAN SIKLUS TIER 2")
    print("=" * 60)
    total_execute = total_blocked = total_skip = total_failed = 0
    total_cb = total_order_failed = 0
    for pair, results in all_results.items():
        if not results:
            print(f"  {pair}: tidak ada kandidat siklus ini")
            continue
        for r in results:
            print(f"  {pair}: outcome={r['outcome']} log_id={r['log_id']}")
            if r["outcome"] == "execute":
                total_execute += 1
            elif r["outcome"] == "execute_blocked_by_risk":
                total_blocked += 1
            elif r["outcome"] == "order_failed":
                total_order_failed += 1
            elif r["outcome"] == "skip":
                total_skip += 1
            elif r["outcome"] == "circuit_breaker":
                total_cb += 1
            else:
                total_failed += 1

    print(f"\nTotal: {total_execute} execute, {total_blocked} execute-blocked, "
          f"{total_order_failed} order-failed, {total_skip} skip, "
          f"{total_cb} circuit-breaker, {total_failed} gagal/error")

    if total_execute > 0 and not _execution_enabled(config):
        print(f"\n[FASE 5] {total_execute} trade paper tercatat ke "
              f"paper_trade_outcomes.")

    if total_execute > 0 and _execution_enabled(config):
        print(f"\n[FASE 6] {total_execute} order LIVE terkirim -- "
              f"position_monitor.py akan mendeteksi closure-nya nanti.")

    mt5c.shutdown()
    print("\n[SELESAI] tier2_orchestrator.py berhasil.")