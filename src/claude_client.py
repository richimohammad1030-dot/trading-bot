"""
claude_client.py

Wrapper untuk memanggil Claude API (Tier 2) sesuai
system prompt 7 task type (lihat system_prompt.md).

Fungsi utama:
- call_claude(task_type, payload, images=None) -> dict
  Mengirim payload sebagai JSON ke Claude, parse response
  JSON, return dict Python.
- load_image_as_base64(path, label) -> dict
  Helper BARU (Fase 4): baca file PNG dari disk, encode base64,
  siap dipakai sebagai salah satu elemen `images` di call_claude().

PENTING:
- System prompt dibaca dari file docs/system_prompt.md.
- MODEL direvisi Fase 4: "claude-sonnet-4-6" SUDAH TIDAK VALID
  (digantikan Claude Sonnet 5). String model resmi terkini:
  "claude-sonnet-5". Kalau nanti Anthropic merilis versi baru
  lagi, cukup ubah konstanta MODEL di sini -- tidak ada tempat
  lain yang hardcode nama model.
"""

import base64
import json
import sys
from pathlib import Path

from anthropic import Anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import load_secrets, ROOT_DIR

SYSTEM_PROMPT_PATH = ROOT_DIR / "docs" / "system_prompt.md"
MODEL = "claude-sonnet-5"   # REVISI Fase 4 (sebelumnya "claude-sonnet-4-6", deprecated)
MAX_TOKENS = 4096   # REVISI (ditemukan saat testing live): 1024 TERPOTONG
# -- Claude Sonnet 5 defaultnya menyertakan blok "thinking" (beda dari
# Sonnet 4.6 lama yang implisit tanpa itu), memakan sebagian besar
# budget token SEBELUM sempat menulis JSON output. Response jadi
# "Unterminated string" (JSON tidak selesai). 4096 memberi ruang cukup
# untuk thinking + JSON lengkap. Thinking SENGAJA dibiarkan aktif
# (bukan di-disable) karena Task 5 adalah penilaian visual bernuansa
# (liquidity grab vs breakout, riwayat kunjungan, bias transisi) --
# reasoning tersembunyi berpotensi membantu KUALITAS keputusan.
# Kalau nanti latency/biaya jadi masalah, bisa matikan eksplisit via
# thinking={"type": "disabled"} di parameter messages.create() --
# BELUM dilakukan di sini, keputusan disengaja ditunda ke Anda.


def _load_system_prompt() -> str:
    if not SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"System prompt tidak ditemukan di: {SYSTEM_PROMPT_PATH}\n"
            "Pastikan system_prompt.md ada di folder docs/"
        )
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _get_client() -> Anthropic:
    secrets = load_secrets()
    api_key = secrets.get("ANTHROPIC_API_KEY")
    if not api_key or "xxxx" in api_key:
        raise ValueError(
            "ANTHROPIC_API_KEY belum diisi dengan benar di .env"
        )
    return Anthropic(api_key=api_key)


def load_image_as_base64(path: str, label: str) -> dict:
    """
    BARU (Fase 4): baca file PNG dari disk (path dari chart_generator.py
    / package_builder.py), encode base64, siap dipakai di call_claude().

    Args:
        path: path absolut/relatif ke file PNG
        label: label deskriptif (mis. "H1_chart", "M5_chart")

    Return: dict {"label": str, "data": base64_str, "media_type": str}

    Raises:
        FileNotFoundError jika file tidak ada -- SENGAJA tidak
        di-silent-catch, karena chart yang hilang berarti ada bug di
        alur sebelumnya (chart_generator.py) yang harus diketahui,
        bukan diam-diam kirim paket tanpa gambar ke Claude.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"File chart tidak ditemukan: {path} (label={label}). "
            f"Cek apakah chart_generator.py berhasil generate sebelum "
            f"paket ini dikirim."
        )
    data = base64.b64encode(p.read_bytes()).decode("utf-8")
    return {"label": label, "data": data, "media_type": "image/png"}


def call_claude(task_type: str, payload: dict, images: list = None,
                 timeout: float = 20.0) -> dict:
    """
    Panggil Claude dengan task_type tertentu.

    Args:
        task_type: salah satu dari 7 task type di system prompt
                   (mis. "final_visual_review")
        payload:   dict berisi field-field sesuai skema task
                   tersebut (TANPA "task_type" dan "images",
                   keduanya akan ditambahkan otomatis)
        images:    list of dict {"label": str, "data": base64_str,
                   "media_type": "image/png"} - HANYA untuk
                   task_type "final_visual_review". Pakai
                   load_image_as_base64() untuk membuat tiap elemen.
        timeout:   REVISI Fase 4 (v2.3 Bab 1) -- timeout absolut
                   (detik) untuk mencegah infinite hang jika koneksi
                   putus di tengah inference. Timeout diperlakukan
                   sebagai kegagalan biasa (exception diteruskan ke
                   pemanggil, yang harus menerapkan "diam itu aman" --
                   TIDAK entry, lanjut ke kandidat/pair berikutnya).

    Return: dict hasil parse JSON dari response Claude.

    Raises:
        ValueError jika response BUKAN JSON valid (ini sengaja
        TIDAK di-silent-catch - kalau Claude mengembalikan
        teks non-JSON, itu BUG system prompt yang harus
        diketahui, bukan di-skip diam-diam).
        anthropic.APITimeoutError jika melebihi `timeout` detik
        (v2.3 Bab 1) -- pemanggil WAJIB menangkap ini secara
        eksplisit dan memperlakukannya sebagai "gagal, jangan entry".
    """
    client = _get_client()
    system_prompt = _load_system_prompt()

    full_payload = {"task_type": task_type, **payload}

    # Build content blocks
    content = []
    if images:
        for img in images:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.get("media_type", "image/png"),
                    "data": img["data"],
                }
            })
        full_payload["images"] = [
            {"label": img["label"], "data": "<see image blocks above>"}
            for img in images
        ]

    content.append({
        "type": "text",
        "text": json.dumps(full_payload, ensure_ascii=False)
    })

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
        timeout=timeout,
    )

    # PERBAIKAN (ditemukan saat testing live, Fase 4): JANGAN asumsikan
    # content[0] selalu blok teks. Claude Sonnet 5 bisa mengembalikan
    # ThinkingBlock sebagai elemen PERTAMA di response.content -- kode
    # lama (content[0].text) crash dengan AttributeError saat ini
    # terjadi. Sekarang cari blok bertipe "text" di mana pun posisinya.
    raw_text = None
    for block in response.content:
        if block.type == "text":
            raw_text = block.text.strip()
            break

    if raw_text is None:
        block_types = [b.type for b in response.content]
        raise ValueError(
            f"Response Claude untuk task '{task_type}' TIDAK mengandung "
            f"blok teks sama sekali. Tipe blok yang diterima: {block_types}"
        )

    # Bersihkan kemungkinan markdown fence walau system prompt
    # melarangnya (defensive parsing)
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.startswith("json"):
            raw_text = raw_text[4:].strip()

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError as e:
        # Petunjuk tambahan: string "Unterminated" atau posisi error di
        # ujung teks biasanya berarti respons TERPOTONG (max_tokens
        # kurang), bukan system prompt salah format.
        looks_truncated = "Unterminated" in str(e) or e.pos >= len(raw_text) - 5
        hint = (" (KEMUNGKINAN TERPOTONG -- coba naikkan MAX_TOKENS)"
                if looks_truncated else "")
        raise ValueError(
            f"Response Claude BUKAN JSON valid untuk task "
            f"'{task_type}'{hint}:\n--- RAW RESPONSE ---\n{raw_text}\n"
            f"--- ERROR ---\n{e}"
        )

    return result


if __name__ == "__main__":
    # ===== TEST RUN: Task Type 1 (adx_grey_zone) =====
    print("=" * 50)
    print("TEST: call_claude() - task_type=adx_grey_zone")
    print("=" * 50)

    test_payload = {
        "pair": "EURUSD",
        "adx": 22.4,
        "gap_now": 0.00045,
        "gap_prev": 0.00031,
        "ema20": 1.08652,
        "ema50": 1.08607,
    }

    print("Payload:", json.dumps(test_payload, indent=2))
    print("\nMemanggil Claude API...\n")

    result = call_claude("adx_grey_zone", test_payload)

    print("Response (parsed dict):")
    for k, v in result.items():
        print(f"  {k}: {v}")

    # Validasi skema dasar
    assert result.get("result") in ("trending", "ranging")
    assert result.get("direction") in ("bullish", "bearish", None)
    assert "reasoning" in result

    print("\n[OK] Skema response valid.")
    print("[SELESAI] claude_client.py (Fase 4 -- model + timeout direvisi) berhasil.")
