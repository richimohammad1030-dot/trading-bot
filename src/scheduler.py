"""
scheduler.py - Scheduler Terpusat

Menjalankan SEMUA siklus berulang bot dalam SATU proses persisten:
  1. market_cycle (30 menit): run_tier2_cycle() + paper_outcome_tracker.run_outcome_check()
     -- SEKARANG juga cek Friday block_new_entry (Fase 6, Step 7)
     sebelum screening; screener DILEWATI kalau sudah masuk window
     no-new-entry Jumat, tapi outcome check tetap jalan.
  2. daily_evaluation (23:00 WIB harian)
  3. weekly_evaluation (Minggu 23:30 WIB)
  4. position_management (5 menit, BARU Fase 6): position_monitor.check_closed_positions()
     + exit_logic.run_breakeven_sweep() -- HANYA relevan saat ada posisi
     live (execution.enabled=true), tapi aman dijalankan kapan saja
     (checked=0 kalau tidak ada posisi bot).
  5. overnight_evaluation (22:45 WIB, BARU Fase 6): exit_logic.run_overnight_sweep()
  6. friday_force_close (Jumat 23:30 WIB, BARU Fase 6): exit_logic.run_friday_force_close()

DIKECUALIKAN SENGAJA (bukan lupa -- audit arsitektur menemukan modul
ini TIDAK terintegrasi ke pipeline aktif):
  - news_filter.py: tidak pernah diimpor screener.py/tier2_orchestrator.py.
    Task 4 (news_blackout_classification) ADA di system_prompt.md tapi
    tidak ada pemanggilnya di kode aktif. run_overnight_sweep() dipanggil
    TANPA news_check_fn (default: tidak ada info berita) -- perlu topik/
    fase tersendiri: "integrasi news_filter.py" (P-08).

RISET (WAJIB dibaca sebelum ubah desain ini):
- APScheduler: BlockingScheduler direkomendasikan "when the scheduler
  is the only thing running in your process" -- cocok persis (satu
  proses Python persisten).
- MetaTrader5 Python API BERSIFAT SINGLE-THREADED -- "concurrent
  calls may cause issues" (artikel resmi MQL5, mql5.com/en/articles/21905).
  SEMUA job yang menyentuh MT5 WAJIB diserialize ke SATU worker
  thread -- executor ThreadPoolExecutor max_workers=1 di bawah.
- APScheduler 3.x (versi stabil yang dipakai di sini) masih pakai
  PYTZ secara internal, BUKAN zoneinfo -- mengoper objek
  zoneinfo.ZoneInfo ke parameter timezone= akan error "Only
  timezones from the pytz library are supported". FIX: oper STRING
  "Asia/Jakarta" -- APScheduler otomatis konversi ke pytz internal.
- max_instances=1 (default) mencegah job yang sama overlap dengan
  dirinya sendiri kalau run sebelumnya belum selesai.
- coalesce=True + misfire_grace_time: kalau proses sempat delay,
  jalankan jadwal yang terlewat SEKALI saja (bukan numpuk semua).
- deal.reason (DEAL_REASON_SL/DEAL_REASON_TP) dan magic number filter
  -- lihat docstring position_monitor.py dan exit_logic.py.

Jalankan: python src/scheduler.py
(proses WAJIB tetap terbuka -- Ctrl+C untuk shutdown dengan benar)
"""

import sys
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent))

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.executors.pool import ThreadPoolExecutor

from config_loader import load_config, ROOT_DIR
import mt5_connector as mt5c
from orderblock import init_db as init_ob_db
import zone_state as zs
import package_log as pkglog
from risk_engine import init_risk_db, cleanup_blacklist
import paper_outcome_tracker as pot
from tier2_orchestrator import run_tier2_cycle
import daily_evaluation as daily_eval
import weekly_evaluation as weekly_eval

# --- BARU (Fase 6, Step 7) ---
import position_monitor as posmon
import exit_logic
from exit_logic import check_friday_close

SCHEDULER_TIMEZONE = "Asia/Jakarta"  # STRING, bukan objek zoneinfo -- lihat riset di atas
LOG_PATH = ROOT_DIR / "data" / "scheduler.log"


def _setup_logging():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.getLogger("apscheduler").setLevel(logging.INFO)


logger = logging.getLogger("scheduler")


def _ensure_mt5_connected():
    """
    Pastikan koneksi MT5 masih hidup sebelum job yang butuh data pasar.
    mt5.terminal_info() return None kalau koneksi putus -- proses ini
    bisa hidup berhari-hari, terminal MT5/jaringan bisa putus
    sewaktu-waktu, reconnect otomatis kalau perlu.
    """
    import MetaTrader5 as mt5
    info = mt5.terminal_info()
    if info is None:
        logger.warning("Koneksi MT5 terputus -- mencoba reconnect...")
        if not mt5c.connect():
            raise ConnectionError("Reconnect MT5 GAGAL -- job dibatalkan.")
        logger.info("Reconnect MT5 berhasil.")


# =====================================================
# JOB 1: market_cycle (30 menit) -- screener + outcome check
# =====================================================
def job_market_cycle():
    """
    Siklus pasar utama: Tier 1 -> Tier 2 -> risk gate -> (Fase 6: order)
    (run_tier2_cycle), LALU cek outcome trade paper yang pending
    (run_outcome_check()).

    BARU (Fase 6, Step 7): cek Friday block_new_entry SEBELUM screening
    -- kalau sudah masuk window no-new-entry Jumat (default 21:00 WIB),
    screener DILEWATI (tidak ada entry baru dicari), tapi outcome check
    tetap jalan (posisi/paper-trade yang sudah ada tetap perlu dipantau).

    Exception di-catch DI SINI supaya kegagalan job ini TIDAK
    menghentikan scheduler -- job lain tetap harus bisa jalan.
    """
    try:
        config = load_config()
        _ensure_mt5_connected()

        fc = check_friday_close(config)
        if fc["block_new_entry"]:
            logger.info(f"[market_cycle] Friday block_new_entry aktif "
                        f"({fc['reason']}) -- screener DILEWATI, tidak "
                        f"ada entry baru dicari. Outcome check tetap jalan.")
        else:
            logger.info("[market_cycle] Mulai siklus screener + outcome check...")
            results = run_tier2_cycle(config)
            total = sum(len(v) for v in results.values())
            logger.info(f"[market_cycle] Siklus screener selesai: {total} "
                        f"hasil across {len(results)} pair.")

        outcome_summary = pot.run_outcome_check()
        logger.info(f"[market_cycle] Outcome check (paper): {outcome_summary}")

    except Exception as e:
        logger.error(f"[market_cycle] GAGAL: {type(e).__name__}: {e}",
                     exc_info=True)


# =====================================================
# JOB 2: daily_evaluation (23:00 WIB harian)
# =====================================================
def job_daily_evaluation():
    try:
        logger.info("[daily_evaluation] Mulai evaluasi harian...")
        result = daily_eval.run_daily_evaluation(persist=True)
        logger.info(f"[daily_evaluation] Selesai: date={result['date']} "
                    f"totals={result['payload']['totals']}")
    except Exception as e:
        logger.error(f"[daily_evaluation] GAGAL: {type(e).__name__}: {e}",
                     exc_info=True)


# =====================================================
# JOB 3: weekly_evaluation (Minggu 23:30 WIB)
# =====================================================
def job_weekly_evaluation():
    try:
        logger.info("[weekly_evaluation] Mulai evaluasi mingguan...")
        result = weekly_eval.run_weekly_evaluation(persist=True)
        warn = result["response"]["performance_summary"]["sample_size_warning"]
        logger.info(f"[weekly_evaluation] Selesai: week_end={result['week_end']} "
                    f"sample_size_warning={warn}")
    except Exception as e:
        logger.error(f"[weekly_evaluation] GAGAL: {type(e).__name__}: {e}",
                     exc_info=True)


# =====================================================
# JOB 4 (BARU Fase 6): position_management (5 menit)
# =====================================================
def job_position_management():
    """
    Deteksi closure posisi live (position_monitor.py) + cek breakeven
    (exit_logic.run_breakeven_sweep) untuk posisi yang masih terbuka.
    Interval LEBIH PENDEK dari market_cycle (30 menit) karena SL/TP
    broker atau threshold breakeven bisa tercapai kapan saja, tidak
    menunggu siklus screener.
    """
    try:
        config = load_config()
        _ensure_mt5_connected()

        logger.info("[position_management] Cek closure posisi...")
        closure_summary = posmon.check_closed_positions()
        logger.info(f"[position_management] Closure: {closure_summary}")

        logger.info("[position_management] Cek breakeven...")
        be_summary = exit_logic.run_breakeven_sweep(config)
        logger.info(f"[position_management] Breakeven: {be_summary}")

    except Exception as e:
        logger.error(f"[position_management] GAGAL: {type(e).__name__}: {e}",
                     exc_info=True)


# =====================================================
# JOB 5 (BARU Fase 6): overnight_evaluation (22:45 WIB)
# =====================================================
def job_overnight_evaluation():
    try:
        config = load_config()
        _ensure_mt5_connected()

        logger.info("[overnight_evaluation] Mulai evaluasi overnight...")
        summary = exit_logic.run_overnight_sweep(config)
        logger.info(f"[overnight_evaluation] Selesai: {summary}")

    except Exception as e:
        logger.error(f"[overnight_evaluation] GAGAL: {type(e).__name__}: {e}",
                     exc_info=True)


# =====================================================
# JOB 6 (BARU Fase 6): friday_force_close (Jumat 23:30 WIB)
# =====================================================
def job_friday_force_close():
    try:
        config = load_config()
        _ensure_mt5_connected()

        logger.info("[friday_force_close] Force-close semua posisi bot...")
        summary = exit_logic.run_friday_force_close(config)
        logger.info(f"[friday_force_close] Selesai: {summary}")

    except Exception as e:
        logger.error(f"[friday_force_close] GAGAL: {type(e).__name__}: {e}",
                     exc_info=True)


def init_all_db():
    """Inisialisasi SEMUA tabel yang dipakai job -- idempoten, aman
    dipanggil tiap start scheduler."""
    init_ob_db()
    zs.init_db()
    pkglog.init_db()
    init_risk_db()
    pot.init_db()
    daily_eval.init_db()
    weekly_eval.init_db()


def build_scheduler() -> BlockingScheduler:
    executors = {
        # max_workers=1 WAJIB -- MT5 Python API single-threaded (lihat
        # riset di docstring modul). SEMUA job diserialize lewat satu
        # worker, tidak pernah ada 2 job jalan bersamaan.
        "default": ThreadPoolExecutor(max_workers=1),
    }
    job_defaults = {
        "coalesce": True,           # jadwal terlewat -> jalankan 1x saja
        "max_instances": 1,         # job yang sama tidak boleh overlap dirinya sendiri
        "misfire_grace_time": 300,  # toleransi 5 menit kalau proses sempat delay
    }

    scheduler = BlockingScheduler(executors=executors,
                                   job_defaults=job_defaults,
                                   timezone=SCHEDULER_TIMEZONE)

    scheduler.add_job(job_market_cycle, "interval", minutes=30,
                       id="market_cycle")
    scheduler.add_job(job_daily_evaluation, "cron",
                       hour=23, minute=0, id="daily_evaluation")
    scheduler.add_job(job_weekly_evaluation, "cron",
                       day_of_week="sun", hour=23, minute=30,
                       id="weekly_evaluation")

    # --- BARU Fase 6 ---
    scheduler.add_job(job_position_management, "interval", minutes=5,
                       id="position_management")
    scheduler.add_job(job_overnight_evaluation, "cron",
                       hour=22, minute=45, id="overnight_evaluation")
    scheduler.add_job(job_friday_force_close, "cron",
                       day_of_week="fri", hour=23, minute=30,
                       id="friday_force_close")

    return scheduler


if __name__ == "__main__":
    _setup_logging()

    print("=" * 60)
    print("SCHEDULER TERPUSAT -- Trading Bot (Fase 4+5+6)")
    print("=" * 60)
    print("Job terdaftar:")
    print("  1. market_cycle          : 30 menit (screener + outcome check + Friday guard)")
    print("  2. daily_evaluation      : 23:00 WIB setiap hari")
    print("  3. weekly_evaluation     : Minggu 23:30 WIB")
    print("  4. position_management   : 5 menit (closure detect + breakeven)")
    print("  5. overnight_evaluation  : 22:45 WIB setiap hari")
    print("  6. friday_force_close    : Jumat 23:30 WIB")
    print("\nDIKECUALIKAN (belum terintegrasi ke pipeline aktif):")
    print("  - news_filter.py (P-08, overnight_evaluation jalan tanpa info berita)")
    print("=" * 60)

    logger.info("Scheduler starting...")

    if not mt5c.connect():
        logger.error("Koneksi MT5 awal GAGAL -- scheduler tidak dimulai.")
        sys.exit(1)

    init_all_db()
    logger.info("Semua tabel DB siap.")

    scheduler = build_scheduler()

    # Jalankan market_cycle SEKALI segera saat start (bukan tunggu 30
    # menit pertama) -- feedback cepat kalau ada masalah konfigurasi.
    logger.info("Menjalankan market_cycle awal (feedback cepat)...")
    job_market_cycle()

    try:
        logger.info(f"Scheduler berjalan (start: "
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} lokal). "
                    f"Tekan Ctrl+C untuk berhenti.")
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown diminta (Ctrl+C)...")
    finally:
        scheduler.shutdown(wait=False)
        mt5c.shutdown()
        logger.info("Scheduler dan koneksi MT5 ditutup dengan bersih.")
