"""
config_loader.py

Modul untuk membaca konfigurasi dari:
- config/config.yaml  (parameter sistem, AMAN di-commit)
- .env                 (kredensial rahasia, JANGAN di-commit)

Setiap modul lain (Step 3 dst) akan import dari sini,
supaya SATU SUMBER KEBENARAN untuk semua parameter.
"""

import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

# Path absolut ke root project (folder yang berisi config/, src/, dll)
ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config" / "config.yaml"
ENV_PATH = ROOT_DIR / ".env"


def load_config() -> dict:
    """Load parameter dari config/config.yaml"""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.yaml tidak ditemukan di: {CONFIG_PATH}\n"
            "Pastikan file ada di folder config/"
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_secrets() -> dict:
    """Load kredensial dari .env"""
    if not ENV_PATH.exists():
        raise FileNotFoundError(
            f".env tidak ditemukan di: {ENV_PATH}\n"
            "Copy .env.example menjadi .env lalu isi nilainya."
        )
    load_dotenv(ENV_PATH)

    required_keys = [
        "ANTHROPIC_API_KEY",
        "MT5_LOGIN",
        "MT5_PASSWORD",
        "MT5_SERVER",
    ]
    secrets = {}
    missing = []
    for key in required_keys:
        value = os.getenv(key)
        if not value or value.strip() == "" or "xxxx" in value or "your_" in value:
            missing.append(key)
        secrets[key] = value

    if missing:
        print("PERINGATAN: kredensial berikut belum diisi dengan benar di .env:")
        for key in missing:
            print(f"  - {key}")
        print("(Wajar jika belum sampai Step 3/Step koneksi Exness)\n")

    return secrets


def get_all_pairs(config: dict) -> list:
    """Helper: dapatkan list semua pair dari config (gabungan group_a, group_b, independent)"""
    pairs_cfg = config["pairs"]
    all_pairs = []
    all_pairs += pairs_cfg.get("group_a", [])
    all_pairs += pairs_cfg.get("group_b", [])
    all_pairs += pairs_cfg.get("independent", [])
    return all_pairs


if __name__ == "__main__":
    # ===== TEST RUN =====
    print("=" * 50)
    print("TEST: Membaca config.yaml dan .env")
    print("=" * 50)

    config = load_config()
    print("\n[OK] config.yaml terbaca.\n")

    print("--- Pairs ---")
    print("Group A (USD strength):", config["pairs"]["group_a"])
    print("Group B (independen)  :", config["pairs"]["group_b"])
    print("Independent           :", config["pairs"]["independent"])
    print("Semua pairs           :", get_all_pairs(config))

    print("\n--- Risk Framework ---")
    risk = config["risk"]
    print(f"Per trade        : {risk['per_trade_pct']}%")
    print(f"Daily limit      : {risk['daily_limit_pct']}%")
    print(f"Weekly limit     : {risk['weekly_limit_pct']}%")
    print(f"Monthly limit    : {risk['monthly_limit_pct']}% "
          f"(rolling {risk['monthly_rolling_days']} hari)")
    print(f"RR ratio         : 1:{risk['rr_ratio']}")

    print("\n--- Timeframes ---")
    tf = config["timeframes"]
    print(f"Zone (OB/SBR)    : {tf['zone']}")
    print(f"Entry primary    : {tf['entry']}")
    print(f"Entry fallback   : {tf['entry_fallback']}")
    print(f"HTF context      : {tf['htf_context']}")

    print("\n--- Spread Filter ---")
    for pair, sp in config["spread"].items():
        print(f"{pair:8s}: baseline {sp['baseline']} pip, "
              f"skip jika > {sp['skip_above']} pip")

    print("\n" + "=" * 50)
    print("TEST: Membaca .env (kredensial)")
    print("=" * 50)
    secrets = load_secrets()
    for key, value in secrets.items():
        if value:
            masked = value[:4] + "..." + value[-2:] if len(value) > 8 else "***"
        else:
            masked = "(belum diisi)"
        print(f"{key:20s}: {masked}")

    print("\n[SELESAI] Jika tidak ada error di atas, Step 2 berhasil.")
