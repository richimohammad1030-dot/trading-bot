"""
diag_bias.py v2 - DIAGNOSTIK bias berbasis bos_choch (H1)

REVISI: logika "2-swing manual" v1 CACAT (terbukti di AUDUSD: bilang
bullish padahal bos_choch bilang bearish -- 2-swing ketipu bounce
terakhir). v2 memakai bos_choch library yang melacak struktur secara
berurutan & internal (tahan bounce).

Tujuan: verifikasi logika bias-dari-bos_choch menghasilkan bias yang
konsisten dengan kondisi nyata, SEBELUM dipindah ke screener.py.

Jalankan: python src/diag_bias.py
Lalu KIRIM seluruh output ke chat.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config_loader import load_config, get_all_pairs
import mt5_connector as mt5c
from indicators import get_bos_choch, get_confirmed_snapshot


def _isnan(x):
    try:
        return x != x
    except Exception:
        return False


def determine_bias_from_bos_choch(df, lookback_events: int = 3) -> dict:
    """
    Tentukan bias dari N event BOS/CHoCH TERAKHIR (berurutan).

    Prinsip:
    - bos_choch melacak struktur internal (BOS=continuation terkonfirmasi,
      CHoCH=reversal). Tahan terhadap bounce sesaat, beda dari
      membandingkan 2 swing mentah.
    - Event TERAKHIR paling menentukan arah "sekarang".
    - Kalau event terakhir CHoCH -> transisi (serahkan Tier 2).
    """
    bos = get_bos_choch(df)  # sudah anti-repaint via _prep internal
    valid = bos[(bos["BOS"].notna() & (bos["BOS"] != 0)) |
                (bos["CHOCH"].notna() & (bos["CHOCH"] != 0))]

    if len(valid) == 0:
        return {"bias": "ranging", "reason": "no_bos_choch_events", "events": []}

    events = []
    for idx, row in valid.iterrows():
        if row["BOS"] not in (0, None) and not _isnan(row["BOS"]):
            events.append(("BOS", "bullish" if row["BOS"] == 1 else "bearish",
                           float(row["Level"]), int(idx)))
        elif row["CHOCH"] not in (0, None) and not _isnan(row["CHOCH"]):
            events.append(("CHOCH", "bullish" if row["CHOCH"] == 1 else "bearish",
                           float(row["Level"]), int(idx)))

    recent = events[-lookback_events:]
    last_type, last_dir, _, _ = events[-1]

    directions = [d for (_, d, _, _) in recent]
    n_bull = directions.count("bullish")
    n_bear = directions.count("bearish")

    if last_type == "CHOCH":
        bias = last_dir
        reason = f"last_event_CHOCH_{last_dir}_transition"
    else:
        if n_bull > 0 and n_bear == 0:
            bias = "bullish"
            reason = f"consistent_bos_bullish_{n_bull}events"
        elif n_bear > 0 and n_bull == 0:
            bias = "bearish"
            reason = f"consistent_bos_bearish_{n_bear}events"
        else:
            bias = last_dir
            reason = f"mixed_recent_last_bos_{last_dir}"

    return {"bias": bias, "reason": reason, "events": events,
            "last_type": last_type, "last_dir": last_dir}


if __name__ == "__main__":
    config = load_config()
    print("=" * 60)
    print("DIAGNOSTIK BIAS v2 (berbasis bos_choch, H1)")
    print("=" * 60)

    if not mt5c.connect():
        sys.exit(1)

    pairs = get_all_pairs(config)
    for pair in pairs:
        symbol = mt5c.resolve_symbol(pair, config)
        df = mt5c.get_candles(symbol, "H1", count=200)
        print(f"\n{'='*60}")
        print(f"PAIR: {pair}  ({len(df)} candle H1)")
        print("=" * 60)

        try:
            result = determine_bias_from_bos_choch(df, lookback_events=3)

            print(f"  Total event BOS/CHoCH: {len(result['events'])}")
            print("  5 event terakhir (urut lama->baru):")
            for (tipe, arah, level, idx) in result["events"][-5:]:
                print(f"    idx={idx:<4} {tipe:5s} {arah:8s} @ {level:.5f}")

            print(f"\n  => BIAS: {result['bias'].upper()}")
            print(f"     alasan: {result['reason']}")
            if result.get("last_type") == "CHOCH":
                print(f"     [!] Event terakhir CHoCH -> fase transisi "
                      f"(potensi_choch), serahkan ke Tier 2")

            snap = get_confirmed_snapshot(df, config)
            ema_rel = ">" if snap["ema20"] > snap["ema50"] else "<"
            print(f"\n  DATA PENDUKUNG (utk Tier 2, bukan penentu):")
            print(f"    EMA20 {ema_rel} EMA50  |  ADX={snap['adx']} "
                  f"(+DI={snap['plus_di']} -DI={snap['minus_di']})")

            ema_bias = "bullish" if snap["ema20"] > snap["ema50"] else "bearish"
            match = "SEJALAN" if ema_bias == result["bias"] else "BEDA"
            print(f"    -> struktur={result['bias']} vs EMA={ema_bias}: {match}")

        except Exception as e:
            print(f"  [ERROR] {e}")
            import traceback
            traceback.print_exc()

    mt5c.shutdown()
    print(f"\n{'='*60}")
    print("[SELESAI] Kirim SELURUH output ini ke chat untuk analisis.")
    print("=" * 60)
