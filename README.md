# Audit bot Polyester testnet

Hasil audit skrip lama + versi yang sudah diperbaiki. Fokus: **kenapa order beli
tidak pernah jalan**.

## Ringkasan: bukan API-nya yang salah

Saya cocokkan setiap pemanggilan SDK di skrip lama dengan source
`polyester-sdk 0.6.2` (yang dipakai `AsyncPolyester`):

| Pemanggilan | Status |
| --- | --- |
| `AsyncPolyester(environment=..., api_url=..., ws_url=..., api_key_id=..., api_private_key=..., default_account_id=...)` | valid |
| `orders.create(symbol, side, order_type, tif, qty, price, post_only, client_order_id)` | valid |
| `orders.create(..., order_type="market", max_slippage_bps=...)` | valid |
| `orders.cancel(key=ClientOrderId(...), symbol=...)` | valid |
| `orders.get(key=..., include_execution_history=False)` | valid |
| `orders.list_open(limit=, page_token=)` → `.orders`, `.next_page_token` | valid |
| `orderbook.get(symbol=, depth=)` | valid |
| `balances.list()` → `AssetBalance.{asset_id,trading,funding,reserved,available}` (u128, skala 1e18) | valid |
| `catalogs.ledger_id_for_asset / base_quantity_scale_for_symbol / symbol_for_symbol_id` | valid |
| `fees.get_spot_fee_rates()` → `maker_fee_rate_percent` (persen → `×100` = bps) | valid |
| `Price.from_ticks(ticks, symbol=...)` | valid |

Jadi tidak ada signature yang berubah. Masalahnya ada di **aritmatika harga
dan ukuran order**.

## Temuan utama

### 1. Syarat spread 40 bps vs spread nyata 1–12 bps → tidak ada order sama sekali

`plan_quotes()` lama memakai `half = (maker_bps + MIN_EDGE_BPS) / 10000`
= `(2 + 10) / 10000` = 20 bps, lalu:

```python
want_bid = ref * (1 - half)      # 20 bps di bawah mid
if want_bid > best_bid: ...      # baru dipasang kalau menembus best bid
```

Artinya bot hanya mau membeli kalau **spread > 2 × 20 bps = 40 bps**.

Snapshot book testnet (6 Nov 2026, BTC ≈ 76.164):

```
ARB-USDT   spread 11.83 bps
AVAX-USDT  spread  6.00 bps
BTC-USDT   spread  1.25 bps
ETH-USDT   spread  8.08 bps
SOL-USDT   spread  4.44 bps
... semua 25 market: 1.20 - 11.83 bps
```

Saya jalankan ulang rumus lama terhadap 25 market itu: **0 dari 25 pair
menghasilkan order**. `buy` dan `sell` dua-duanya `None`, jadi tidak pernah ada
order beli (atau jual) yang dikirim ke API. Log-nya pun hanya muncul di siklus
1 dan tiap 10 siklus (level `DEBUG`), sehingga dari luar terlihat seperti bot
"diam / gak jalan" tanpa error.

### 2. Ukuran order bisa di bawah minimum → order tidak pernah dibuat

Semua pair punya `minNotionalQuote = 5` USDT. Skrip lama:

```python
notional = max(equity * 1.5 / 100, min_notional * 2)   # hanya untuk target
qty = floor_step(min(notional, quote_avail * 0.95) / px, step)
```

`max(..., min_notional*2)` cuma dipakai untuk menghitung *target inventory*,
bukan ukuran order. Kalau `equity × 1.5% < 5` (equity < ±333 USDT), `qty` jadi
terlalu kecil, `pair.ok_size()` gagal, dan order **tidak pernah dikirim** —
tanpa error, hanya baris log "margin ada tapi tidak ada order".

Sekarang `Pair.size()` menaikkan qty ke `min_qty` / `min_notional` bila perlu,
dan menolak order yang tetap tidak memenuhi syarat.

### 3. Saldo `funding` dianggap tidak ada (dan equity salah hitung)

`balances.list()` mengembalikan `funding` dan `trading` terpisah. Order spot
hanya memakai bucket **trading** (`available`). Skrip lama:

```python
"total": Decimal(bal.trading) / LEDGER_SCALE     # funding tidak dihitung
```

Dua akibat:

* equity terlalu kecil (kalau dana masih di funding), dan
* order beli ditolak `INSUFFICIENT_FUNDS` walaupun UI menunjukkan saldo.

Deposit baru masuk ke **funding**; harus ditransfer funding → trading dulu.
Bot versi baru menjumlahkan keduanya untuk equity dan memberi peringatan
eksplisit kalau ada dana nyangkut di funding.

### 4. Order post-only bisa kena `POST_ONLY_CROSS`

Harga dihitung dari book yang dibaca beberapa ratus ms sebelumnya. Kalau book
bergerak, order post-only ditolak `ERROR_CODE_POST_ONLY_CROSS`. Versi baru
mencoba ulang satu tick menjauh dari market.

### 5. Log alasan tidak terlihat

Alasan "dilewati" memakai `SKIP` level (DEBUG) hampir sepanjang waktu. Versi
baru mencatat alasan pertama per pair di level INFO dan menutup tiap siklus
dengan ringkasan `Alasan dilewati: ...`.

## Yang diubah di `bot.py`

* **Kuotasi di dalam spread**: join `best_bid` / `best_ask` (opsional
  `BOT_IMPROVE_TICKS=1` untuk maju satu tick). Tidak lagi menuntut edge 20 bps.
* **Syarat spread eksplisit**: `BOT_MIN_SPREAD_BPS` (default `0` = selalu
  kuotasi selama ada book). `BOT_PROFITABLE_ONLY=1` mengembalikan syarat
  `2×fee + BOT_MIN_EDGE_BPS` kalau memang mau cari profit, bukan volume.
* **Sizing aman**: menghormati `step_size`, `min_qty_base`, `min_notional_quote`,
  `max_order_notional_quote`, dan saldo yang benar-benar `available`.
* **Equity** = `trading + funding`, plus peringatan dana di funding.
* **Retry `POST_ONLY_CROSS`** satu tick.
* **Fallback volume** `BOT_TAKER_AFTER` (default 300 s): kalau maker tidak kena,
  kirim limit IOC di harga touch supaya transaksi tetap terjadi. Set `0` kalau
  tidak mau mengambil likuiditas.
* **Isolasi error per pair**: satu pair error tidak mematikan akun.
* **Ringkasan alasan** tiap siklus, jadi kelihatan kenapa order tidak dipasang.
* `sweep_expired` + `reconcile` tetap seperti aslinya (sudah benar).

## Cara pakai

> **Penting:** paket PyPI-nya bernama **`polyester-sdk`**, bukan `polyester`.
> `pip install polyester` memasang paket lain (jembatan R ↔ Python) yang juga
> mengklaim nama modul `polyester`, dan itu akan merusak `import polyester`.
> Selalu pasang lewat `requirements.txt`.

```bash
pip install -r requirements.txt
cp .env.example .env      # isi API key, private key, account id
```

Diagnosa dulu (tidak mengirim order, kecuali `--place`):

```bash
python doctor.py                        # katalog, saldo, fee, spread, preview order beli
python doctor.py --place                # benar-benar kirim 1 order beli kecil lalu batalkan
python doctor.py --symbol ETH-USDT --place
python doctor.py --account 2 --place    # diagnosa akun kedua (suffix _2 di .env)
```

`doctor.py` mencetak kode error mentah (`ERROR_CODE_...`), daftar `violations`,
dan `reject_reason`, jadi kalau order beli masih ditolak, penyebabnya langsung
kelihatan — bukan tebakan.

Jalankan bot:

```bash
python bot.py
# atau
BOT_DRY_RUN=1 python bot.py
```

## Multi akun

Bisa, dan jalan **paralel** dalam satu proses. Cukup tambah suffix angka di
`.env`:

```env
POLYESTER_API_KEY_ID_1=...
POLYESTER_API_PRIVATE_KEY_1=...
POLYESTER_ACCOUNT_ID_1=...

POLYESTER_API_KEY_ID_2=...
POLYESTER_API_PRIVATE_KEY_2=...
POLYESTER_ACCOUNT_ID_2=...
```

Yang terjadi:

* `load_accounts()` membaca `_1`, `_2`, ... sampai ketemu nomor yang kosong.
* Tiap akun dapat `AsyncPolyester` sendiri, `Stats` sendiri, equity/target
  inventory sendiri, dan berjalan sebagai task `asyncio` terpisah.
* Label log per akun: `17:20:01 INFO    [akun2] OK   ETH-USDT BUY ...`
  (pakai `ContextVar`, jadi tidak saling menimpa walau interleaved).
* `BOT_*` tetap global untuk semua akun. Yang per-akun otomatis: saldo, equity,
  order yang dibatalkan saat start, dan `ORDER_PCT` (persentase dari equity
  masing-masing).

Aturan mainnya:

1. Penomoran harus mulai dari `_1` (tidak boleh langsung `_2`).
2. Satu `account_id` hanya boleh muncul sekali. Kalau dobel, akun kedua
   dilewati dengan peringatan — dua worker di akun yang sama akan saling
   membatalkan order karena sama-sama memakai prefix `bot-`.
3. Entri yang cuma terisi sebagian dilewati (dengan peringatan), akun lain
   tetap jalan. Jadi salah ketik di akun 2 tidak mematikan akun 1.
4. Setiap API key punya policy sendiri — pastikan semuanya punya izin
   **Spot trading** dan daftar market yang sama kalau ingin perilaku identik.
5. Beban API naik linear: N akun × jumlah pair call `orderbook.get` per siklus.
   Rate limit dihitung per API key, jadi biasanya aman; kalau mesin terasa
   berat, naikkan `BOT_CYCLE_DELAY`.

## Kalau order beli masih gagal

Cek output `doctor.py`, lalu cocokkan:

| Yang muncul | Arti / tindakan |
| --- | --- |
| `INSUFFICIENT_FUNDS` + tabel saldo `funding > 0, available = 0` | dana masih di funding; transfer funding → trading |
| `ERROR_CODE_POLICY_MARKET_DENY` / `PERMISSION_DENIED` | API key/subaccount policy tidak mengizinkan pair itu (atau Spot trading belum aktif). Bot mengarantina pair tersebut sampai restart; aktifkan market di **API key → Permissions** / subaccount policy, atau batasi lewat `BOT_PAIRS` |
| `ERROR_CODE_MIN_NOTIONAL` | naikkan `BOT_ORDER_PCT` atau equity |
| `ERROR_CODE_QTY_STEP_SIZE` | `step_size` pair (bot sudah membulatkan; muncul kalau katalog basi) |
| `ERROR_CODE_POST_ONLY_CROSS` | book bergerak; bot sudah retry, atau set `BOT_IMPROVE_TICKS=0` |
| `ERROR_CODE_PRICE_TICK_SIZE` | `tick_size` pair (idem) |
| `UNAUTHENTICATED` | key id / private key salah, atau timestamp mesin melenceng > 5 detik |

## Batasan katalog testnet (snapshot 6 Nov 2026)

* 25 pair: 20 USDT, 3 USDC, 2 BTC.
* `minNotionalQuote = 5` untuk semua pair.
* `minQtyBase` bervariasi: `ARB=1`, `BONK=100000`, `BTC=0.00001`.
* `SHIB`/`BONK` punya `baseQuantityScale = 0` (qty integer).
* `maxOpenOrdersPerPair = 200`, `maxOpenOrdersTotal = 1000`.
* `BTC-USDC` dan `WLD-USDT` tidak punya book → otomatis dilewati.

## Uji offline

```bash
python tests/test_offline.py
```

Tes menjalankan `run_account()` dengan client palsu (tanpa jaringan, tanpa SDK)
dan memastikan order beli **dan** jual benar-benar dipasang, ukurannya di atas
minimum, harganya di dalam spread, plus regresi bahwa rumus lama memang
menghasilkan 0 kuotasi pada data book testnet.

Validasi encoding order memakai kode SDK asli (butuh SDK terpasang, tidak
mengirim order, tidak butuh kredensial):

```bash
python tests/validate_sdk_encoding.py
```

Skrip ini mengambil katalog publik testnet, membentuk order beli post-only
untuk setiap pair seperti yang dilakukan bot, lalu meng-encode-nya lewat
`polyester.codecs.orders`. Hasil saat audit: **21 pair ter-encode, 0 gagal**
(`limit_gtc`, `post_only=True`, `price_ticks` dan `base_qty_scaled` sesuai).
Dari situ juga terlihat bahwa SDK hanya menolak qty/harga yang jumlah desimalnya
melebihi scale pair (`ERROR_CODE_QTY_STEP_SIZE` / `ERROR_CODE_PRICE_TICK_SIZE`);
keselarasan tick/step yang sebenarnya tetap divalidasi server.
