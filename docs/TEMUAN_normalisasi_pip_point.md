# TEMUAN AUDIT — Normalisasi Pip/Point Tidak Terpusat

**Sumber audit:** Perbandingan Addendum v2.3 (Ketahanan Eksekusi Run-Time, Bab 3) vs kode aktual di project knowledge.
**Tanggal temuan:** 2026-07-03
**Tanggal selesai:** 2026-07-04
**Status:** ✅ SELESAI — semua 5 titik bug diperbaiki, diverifikasi via regresi + live test di akun Exness Cent.

---

## 1. Aturan yang Dilanggar (Ringkasan)

Addendum v2.3 Bab 3.6 mengunci prinsip berikut sebagai **wajib**:

> "SEMUA angka jarak (SL, TP, slippage, hard cap, spread) di config didefinisikan dalam PIP atau HARGA ABSOLUT — tidak pernah 'point' mentah.
> Konversi pip↔price/point HANYA lewat fungsi terpusat berbasis symbol_info. Tidak ada modul yang menghitung sendiri.
> JANGAN hardcode asumsi pip — TURUNKAN dari properti simbol saat runtime (Bab 3.3)."

Ditemukan awal **4 implementasi** konversi pip berbeda tersebar di codebase; saat eksekusi perbaikan, terungkap **implementasi ke-5** di `risk_engine.py` yang tidak tercatat di audit awal.

---

## 2. Solusi yang Diterapkan

**Satu fungsi pip terpusat baru di `mt5_connector.py`** (single source of truth untuk seluruh codebase):

```python
def pip_size(symbol: str) -> float
def price_to_pips(price_diff: float, symbol: str) -> float
def pips_to_price(pips: float, symbol: str) -> float
def get_spread_pip(symbol: str) -> float
```

**Formula** (referensi kanonik forum MQL5, mql5.com/en/forum/232387, dikonfirmasi dokumentasi resmi mql5.com/en/docs/python_metatrader5):
```
pip_multiplier = 10.0 jika digits in (3, 5), selain itu 1.0
pip_size       = point * pip_multiplier
```

Semua fungsi **raise `RuntimeError`** eksplisit jika `symbol_info(symbol) is None` — tidak silent-fallback ke angka default (konsisten filosofi "gagal harus terlihat" yang sudah dipakai `claude_client.py`).

**Sanity-check khusus XAUUSD** (bukan hardcode kedua, murni validasi runtime): riset tambahan (Vantage, DailyForex, forum MQL5) mengonfirmasi gold **tidak** mengikuti aturan forex "digit kedua-dari-belakang" — konvensi pasar gold adalah pip = $0.01 secara tetap, independen presisi kuotasi broker. Formula generik kebetulan cocok untuk broker Anda (`XAUUSDm` digits=3 → 0.01), tapi `pip_size()` mem-print `WARNING` eksplisit jika hasil menyimpang jauh dari $0.01 — supaya kalau broker mengubah presisi gold di masa depan, sistem terlihat berteriak, bukan diam-diam salah skala.

**Lot sizing cent vs standard account** — diverifikasi via forum MQL5: `trade_tick_value` sudah dihitung MT5 dalam mata uang deposit akun secara otomatis. `risk_engine.py::calculate_lot()` **tidak diubah** karena sudah benar sejak awal (pakai `trade_tick_size`/`trade_tick_value` langsung) — justru dijadikan acuan desain fungsi pip terpusat.

---

## 3. Kelima Titik Bug — Status Perbaikan

| # | File | Fungsi | Masalah Lama | Fix | Status |
|---|---|---|---|---|---|
| 1 | `spread_filter.py` | `get_current_spread_pip()` | Konversi manual `digits in (5,3)` → bagi 10 | Didelegasikan ke `mt5c.get_spread_pip()` | ✅ |
| 2 | `mt5_connector.py` | `get_spread()` | Logic konversi manual berbeda (`point <= 0.00001`) | **Dihapus total** (0 pemanggil live, terverifikasi grep) | ✅ |
| 3 | `orderblock.py` | `_pip_divisor()` | **Bug nyata**: docstring bilang digits=3→divisor=100, kode return 10000 (salah 100x untuk USDJPY & XAUUSD) | **Dihapus total**; satu pemanggil (`__main__` diagnostic) pakai `mt5c.pip_size()`/`price_to_pips()` | ✅ |
| 4 | `exit_logic.py` | `calculate_sl_tp()` (inline) | **Hardcode**: `pip_size = 0.0001 if digits in (5,4) else 0.01` — tidak diturunkan dari `symbol_info` sama sekali | Diganti `mt5c.pips_to_price(spread_pip, symbol)` | ✅ |
| 5 | `risk_engine.py` | `is_level_blacklisted()` | **Bug ke-5, ditemukan saat eksekusi** (tak tercatat di audit awal): hardcode `tolerance=0.0001` di signature — salah untuk XAUUSD & USDJPY | `tolerance=None` default → `mt5c.pips_to_price(1, pair)`, dengan fallback eksplisit + `WARNING` print jika `symbol_info` gagal | ✅ |

---

## 4. Bukti Verifikasi (Regresi vs Data Live Exness Cent)

`test_pip_regression.py` dijalankan terhadap 5 pair aktif (EURUSDm, GBPUSDm, AUDUSDm, USDJPYm, XAUUSDm):

- **EURUSD, GBPUSD, AUDUSD** (digits=5): seluruh implementasi lama = fungsi baru, tanpa mismatch.
- **USDJPY, XAUUSD** (digits=3): mismatch hanya di `orderblock._pip_divisor` — persis bug #3 di atas, dikonfirmasi bukan penyimpangan baru.

`risk_engine.py::is_level_blacklisted()` diuji langsung: level XAUUSD berselisih **separuh dari 1 pip riil** ($0.005 dari $4175.00) —
- Tolerance **LAMA** (hardcode 0.0001): hasil `False` (bug — level seharusnya dianggap sama, gagal terdeteksi).
- Tolerance **BARU** (`pips_to_price`, = 0.01): hasil `True` (benar).

Dampak riil bug lama jika belum diperbaiki: level XAUUSD/USDJPY yang baru saja di-blacklist (mis. karena SL/BE) bisa lolos re-entry protection karena toleransi pencocokan terlalu kecil 100x lipat — sistem akan mengizinkan entry ulang di level yang sebetulnya baru saja gagal.

---

## 5. File yang Diubah

- `mt5_connector.py` — fungsi pip terpusat ditambahkan, `get_spread()` lama dihapus
- `spread_filter.py` — delegasi ke fungsi terpusat
- `orderblock.py` — `_pip_divisor()` dihapus
- `exit_logic.py` — hardcode dihapus
- `risk_engine.py` — **(file kritis)** hardcode tolerance dihapus, dikonfirmasi eksplisit sebelum eksekusi
- `test_pip_regression.py` — file baru (regresi lama vs baru)
- `verify_symbol_pip_properties.py` — file baru (verifikasi `symbol_info` live)

---

## 6. Item Terbuka (TIDAK Termasuk Cakupan Audit Ini — Perlu Audit Terpisah)

- Escalating cooldown 3-tingkat di `zone_state.py` (Addendum v2.2 Bab 1.4: `not_yet_formed` → throttle 2 candle M5; `setup_invalid` → 1→3→sampai-mitigasi candle H1) — fungsi `apply_skip_cooldown()` sudah dipanggil dengan `skip_reason_type`, tapi logika detail throttle belum diaudit penuh untuk konfirmasi kesesuaian persis dengan tabel addendum.
- Pre-Execution Spread Check (Addendum v2.3 Bab 2.2) — perlu konfirmasi apakah execution gate memanggil ulang `spread_filter.py` tepat sebelum OrderSend (Fase 6), bukan hanya di pre-filter awal.
- Keputusan nasib `exit_logic.py::calculate_sl_tp()` — mati di jalur live (Claude yang beri SL/TP, D-05), pip hardcode-nya sudah diperbaiki tapi belum diputuskan apakah akan dipakai sebagai validator SL Claude di Fase 6 atau dihapus permanen.
