"""
chart_generator.py - REBUILD Fase 3

REVISI TOTAL dari versi Step 13 lama. Versi lama dirancang untuk
arsitektur H4/H1 dengan SATU OB dan entry/SL/TP yang SUDAH diputuskan
(pola lama signal_engine.py yang sudah kita buang sepenuhnya).

Versi baru ini:
- HANYA 2 timeframe: H1 (structure_tf) + M5 (timing_tf) -- H4 sudah
  dibuang dari arsitektur (revisi bias H1 via bos_choch, sesi Opus).
- ZONA-AGNOSTIK: bisa menggambar SEMUA tipe zona (OB/SBR/RBS/FVG/
  EQH/EQL/SD), bukan cuma OB.
- TIDAK menggambar entry/SL/TP -- itu keputusan Tier 2 (Model A,
  v2.0 Bab 8), Tier 1 hanya MENYAJIKAN, tidak menghakimi (v2.1 Bab 0.1).
- H1 = konteks struktur (zona kandidat + zona lain di sekitarnya).
- M5 = lensa reaksi (v2.0 Bab 2.2) -- Claude membaca rejection/
  liquidity grab/absorption dari sini.

Referensi teknis (dipertahankan dari versi lama, masih valid):
- mplfinance returnfig=True untuk dapat objek ax asli
- Zona digambar via ax.fill_between() (bukan alines trial-error,
  lihat GitHub issue #485 mplfinance)
"""

import sys
from pathlib import Path
from datetime import datetime

import matplotlib
matplotlib.use("Agg")  # Non-interactive backend, untuk server/VPS
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import load_config


def _prepare_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """Siapkan dataframe untuk mplfinance: DatetimeIndex, kolom capital."""
    df_plot = df.copy()
    df_plot = df_plot.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "tick_volume": "Volume", "volume": "Volume",
    })
    df_plot["time"] = pd.to_datetime(df_plot["time"])
    df_plot = df_plot.set_index("time")
    cols = ["Open", "High", "Low", "Close"]
    if "Volume" in df_plot.columns:
        cols.append("Volume")
    return df_plot[cols]


# Warna per tipe zona -- konsisten dipakai H1 & M5, memudahkan Claude
# mengenali tipe zona yang sama di kedua chart
_ZONE_COLORS = {
    "OB": "#4287f5", "FVG": "#9b59b6", "SBR": "#f54242", "RBS": "#2ecc71",
    "EQH": "#e67e22", "EQL": "#16a085", "SD": "#c0392b",
    "SWING_HIGH": "#e74c3c", "SWING_LOW": "#27ae60",
}


def _zone_color(zone_type: str) -> str:
    return _ZONE_COLORS.get(zone_type, "#7f8c8d")


def _draw_zone(ax, zone: dict, x_min, x_max, prominent: bool = True):
    """
    Gambar SATU zona di axes. prominent=True untuk zona KANDIDAT
    (yang sedang dievaluasi, warna solid + label jelas). prominent=
    False untuk zona LAIN di sekitarnya (konteks, warna pudar, tanpa
    label besar) -- membantu Claude melihat "peta" area penting tanpa
    membingungkan fokus utama.
    """
    color = _zone_color(zone["zone_type"])
    alpha = 0.22 if prominent else 0.08
    ax.fill_between([x_min, x_max], zone["zone_bottom"], zone["zone_top"],
                     color=color, alpha=alpha, zorder=0)
    if prominent:
        ax.axhline(zone["zone_top"], color=color, linewidth=0.8,
                    linestyle="--", alpha=0.6)
        ax.axhline(zone["zone_bottom"], color=color, linewidth=0.8,
                    linestyle="--", alpha=0.6)


def _annotate_zone(ax, zone: dict, label_x, prominent: bool = True):
    color = _zone_color(zone["zone_type"])
    mid = (zone["zone_top"] + zone["zone_bottom"]) / 2
    text = f"{zone['zone_type']} {zone['direction']}"
    if prominent:
        text += " [KANDIDAT]"
    ax.annotate(
        text, xy=(label_x, mid), fontsize=7 if prominent else 6,
        color=color, ha="left", va="center",
        fontweight="bold" if prominent else "normal",
        alpha=1.0 if prominent else 0.65,
        bbox=dict(boxstyle="round,pad=0.15", fc="white", ec=color,
                   alpha=0.75) if prominent else None,
    )


def generate_zone_chart(df_structure: pd.DataFrame, df_timing: pd.DataFrame,
                         pair: str, candidate_zone: dict,
                         context_zones: list = None,
                         output_dir: str = None,
                         n_candles_structure: int = 80,
                         n_candles_timing: int = 60) -> dict:
    """
    Generate 2 chart: H1 (struktur + peta area penting) dan M5 (lensa
    reaksi, v2.0 Bab 2.2). TIDAK ada entry/SL/TP -- itu keputusan
    Tier 2.

    Args:
        df_structure: candle H1 (anti-repaint sudah ditangani caller)
        df_timing: candle M5
        pair: nama pair (title & filename)
        candidate_zone: dict zona KANDIDAT yang sedang dievaluasi
            {zone_type, direction, zone_top, zone_bottom, ...}
        context_zones: list zona LAIN (opsional) utk konteks pudar di
            chart H1 -- membantu Claude lihat area penting lain
            terdekat tanpa mengaburkan fokus zona kandidat
        output_dir: folder simpan PNG (default data/charts/)
        n_candles_structure: jumlah candle H1 ditampilkan
        n_candles_timing: jumlah candle M5 ditampilkan

    Return: dict {"h1_chart_path": str, "m5_chart_path": str}
    """
    if output_dir is None:
        output_dir = str(Path(__file__).resolve().parent.parent / "data" / "charts")
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    zone_type = candidate_zone["zone_type"]
    direction = candidate_zone["direction"]

    # ================= CHART H1 (struktur + peta area) =================
    df_plot_h1 = _prepare_ohlc(df_structure.tail(n_candles_structure))

    title_h1 = f"{pair} H1 -- {zone_type} {direction.upper()} (kandidat)"
    fig1, axes1 = mpf.plot(
        df_plot_h1, type="candle", style="charles", title=title_h1,
        ylabel="Price", figsize=(14, 8), returnfig=True,
        volume=False, tight_layout=True,
    )
    ax1 = axes1[0]
    x_min1, x_max1 = ax1.get_xlim()

    # Perluas ylim supaya zona kandidat (bisa jadi di luar range
    # n_candles_structure yang ditampilkan) tetap terlihat penuh
    all_levels = [candidate_zone["zone_top"], candidate_zone["zone_bottom"]]
    if context_zones:
        for cz in context_zones:
            all_levels.extend([cz["zone_top"], cz["zone_bottom"]])
    y_min_cur, y_max_cur = ax1.get_ylim()
    y_min_needed = min(all_levels + [y_min_cur])
    y_max_needed = max(all_levels + [y_max_cur])
    padding = (y_max_needed - y_min_needed) * 0.06
    ax1.set_ylim(y_min_needed - padding, y_max_needed + padding)

    # Zona konteks (pudar) DULU, supaya zona kandidat tergambar DI ATAS
    label_x1 = x_min1 + (x_max1 - x_min1) * 0.01
    if context_zones:
        for cz in context_zones:
            _draw_zone(ax1, cz, x_min1, x_max1, prominent=False)
            _annotate_zone(ax1, cz, label_x1, prominent=False)

    # Zona kandidat (prominent) di atas
    _draw_zone(ax1, candidate_zone, x_min1, x_max1, prominent=True)
    _annotate_zone(ax1, candidate_zone, label_x1, prominent=True)

    h1_path = f"{output_dir}/{pair}_H1_{zone_type}_{timestamp}.png"
    fig1.savefig(h1_path, dpi=120, bbox_inches="tight")
    plt.close(fig1)

    # ================= CHART M5 (lensa reaksi) =================
    df_plot_m5 = _prepare_ohlc(df_timing.tail(n_candles_timing))

    title_m5 = f"{pair} M5 -- reaksi harga di {zone_type} {direction.upper()}"
    fig2, axes2 = mpf.plot(
        df_plot_m5, type="candle", style="charles", title=title_m5,
        ylabel="Price", figsize=(14, 7), returnfig=True,
        volume=False, tight_layout=True,
    )
    ax2 = axes2[0]
    x_min2, x_max2 = ax2.get_xlim()

    y_min_cur2, y_max_cur2 = ax2.get_ylim()
    y_min_needed2 = min([candidate_zone["zone_bottom"], y_min_cur2])
    y_max_needed2 = max([candidate_zone["zone_top"], y_max_cur2])
    padding2 = (y_max_needed2 - y_min_needed2) * 0.08
    ax2.set_ylim(y_min_needed2 - padding2, y_max_needed2 + padding2)

    _draw_zone(ax2, candidate_zone, x_min2, x_max2, prominent=True)
    label_x2 = x_min2 + (x_max2 - x_min2) * 0.01
    _annotate_zone(ax2, candidate_zone, label_x2, prominent=True)

    m5_path = f"{output_dir}/{pair}_M5_{zone_type}_{timestamp}.png"
    fig2.savefig(m5_path, dpi=120, bbox_inches="tight")
    plt.close(fig2)

    return {"h1_chart_path": h1_path, "m5_chart_path": m5_path}


if __name__ == "__main__":
    import mt5_connector as mt5c
    from screener import run_screener_for_pair
    import zone_state as zs
    from orderblock import init_db as init_ob_db
    from indicators import get_confirmed_snapshot

    config = load_config()

    print("=" * 60)
    print("TEST: chart_generator.py (Fase 3 - zona-agnostik H1/M5)")
    print("=" * 60)

    if not mt5c.connect():
        sys.exit(1)

    init_ob_db()
    zs.init_db()

    pair = "EURUSD"
    symbol = mt5c.resolve_symbol(pair, config)
    result = run_screener_for_pair(pair, config)

    df_structure = mt5c.get_candles(symbol, "H1", count=200)
    df_timing = mt5c.get_candles(symbol, "M5", count=100)

    if result["candidates"]:
        candidate = result["candidates"][0]
        print(f"Pakai kandidat NYATA (lolos gate): {candidate['zone_type']} "
              f"{candidate['direction']}")
    else:
        # PERBAIKAN: sebelumnya jatuh ke dummy dengan offset HARDCODE
        # (0.0005/0.0025, ~20 pip fix, TIDAK berbasis ATR) -- ini
        # menyesatkan karena terlihat seperti OB asli padahal bukan.
        # Sekarang: coba ambil OB NYATA dari DB (yang terdeteksi tapi
        # belum lolos gate karena harga belum masuk), baru kalau
        # benar-benar tidak ada apa pun, pakai dummy BERBASIS ATR
        # dengan label jelas "DUMMY" di title chart.
        from orderblock import get_active_obs
        active_obs = get_active_obs(pair)
        if active_obs:
            ob = active_obs[0]
            candidate = {
                "zone_type": "OB", "direction": ob["direction"],
                "zone_top": ob["ob_top"], "zone_bottom": ob["ob_bottom"],
            }
            print(f"[INFO] Tidak ada kandidat lolos gate -- pakai OB NYATA "
                  f"dari DB (belum di-gate): {ob['direction']} "
                  f"{ob['ob_bottom']:.5f}-{ob['ob_top']:.5f} "
                  f"(width={ob['ob_width']:.5f})")
        else:
            print("[INFO] Tidak ada OB nyata sama sekali di DB -- pakai "
                  "DUMMY berbasis ATR (bukan angka hardcode) utk test chart.")
            current_price = float(df_structure["close"].iloc[-2])
            snap = get_confirmed_snapshot(df_structure, config)
            atr_val = snap["atr_current"]
            candidate = {
                "zone_type": "OB_DUMMY", "direction": "bullish",
                "zone_top": round(current_price - 0.3 * atr_val, 5),
                "zone_bottom": round(current_price - 0.9 * atr_val, 5),
            }

    chart_result = generate_zone_chart(df_structure, df_timing, pair, candidate)
    print(f"\n[OK] Chart H1 disimpan: {chart_result['h1_chart_path']}")
    print(f"[OK] Chart M5 disimpan: {chart_result['m5_chart_path']}")

    mt5c.shutdown()
    print("\n[SELESAI] chart_generator.py Fase 3 berhasil.")
