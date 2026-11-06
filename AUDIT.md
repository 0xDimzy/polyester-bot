# Audit teknis: kenapa versi lama tidak pernah membeli

Dokumen ini menjelaskan temuan audit pada skrip lama dan perubahan yang
dilakukan di `bot.py`. Untuk cara menjalankan bot, lihat
[README.md](README.md) atau [INSTALASI.md](INSTALASI.md).

## Ringkasan: bukan API-nya yang salah

Setiap pemanggilan SDK di skrip lama sudah dicocokkan dengan source
`polyester-sdk 0.6.2`:

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

Tidak ada signature yang berubah. Masalahnya ada di **aritmatika harga dan
ukuran order**.

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

Rumus lama dijalankan ulang terhadap 25 market itu: **0 dari 25 pair
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

Alasan "dilewati" memakai level `DEBUG` hampir sepanjang waktu. Versi baru
mencatat alasan pertama per pair di level `INFO` dan menutup tiap siklus dengan
ringkasan `Alasan dilewati: ...`.

## Yang diubah di `bot.py`

* **Kuotasi di dalam spread**: join `best_bid` / `best_ask` (opsional
  `BOT_IMPROVE_TICKS=1` untuk maju satu tick). Tidak lagi menuntut edge 20 bps.
* **Syarat spread eksplisit**: `BOT_MIN_SPREAD_BPS` (default `0` = selalu
  kuotasi selama ada book). `BOT_PROFITABLE_ONLY=1` mengembalikan syarat
  `2×fee + BOT_MIN_EDGE_BPS` kalau memang mau cari profit, bukan volume.
* **Sizing aman**: menghormati `step_size`, `min_qty_base`,
  `min_notional_quote`, `max_order_notional_quote`, dan saldo yang benar-benar
  `available`.
* **Equity** = `trading + funding`, plus peringatan dana di funding.
* **Retry `POST_ONLY_CROSS`** satu tick.
* **Fallback volume** `BOT_TAKER_AFTER` (default 300 s): kalau maker tidak kena,
  kirim limit IOC di harga touch supaya transaksi tetap terjadi. Set `0` kalau
  tidak mau mengambil likuiditas.
* **Isolasi error per pair**: satu pair error tidak mematikan akun.
* **Karantina policy**: pair yang ditolak `ERROR_CODE_POLICY_MARKET_DENY`
  dilewati sampai restart, tidak ditembak ulang tiap siklus.
* **Ringkasan alasan** tiap siklus, jadi kelihatan kenapa order tidak dipasang.
* `sweep_expired` + `reconcile` tetap seperti aslinya (sudah benar).

## Catatan versi lanjutan

Dua bug yang baru ketahuan saat dipakai di akun sungguhan, bukan saat audit:

1. **Status katalog** — SDK (jalur Connect) mengembalikan `PAIR_STATUS_ENABLED`,
   bukan `ENABLED`. Filter allowlist yang ketat membuang 25/25 pair dan bot
   berhenti dengan "Tidak ada pair berquote". Sekarang status dinormalkan dan
   yang ditolak hanya `DISABLED/HALT/SUSPEND/DELIST/PAUSED/CLOSED/UNSPECIFIED`.
2. **Policy market** — API key bisa dibatasi per market
   (`ERROR_CODE_POLICY_MARKET_DENY`); pair seperti itu sekarang dikarantina.

## Batasan katalog testnet (snapshot 6 Nov 2026)

* 25 pair: 20 USDT, 3 USDC, 2 BTC.
* `minNotionalQuote = 5` untuk semua pair.
* `minQtyBase` bervariasi: `ARB=1`, `BONK=100000`, `BTC=0.00001`.
* `SHIB`/`BONK` punya `baseQuantityScale = 0` (qty integer).
* `maxOpenOrdersPerPair = 200`, `maxOpenOrdersTotal = 1000`.
* `BTC-USDC` dan `WLD-USDT` berstatus `PAIR_STATUS_DISABLED` (dan book-nya kosong).
* Fee testnet: maker 0,1% (10 bps), taker 0,15% (15 bps). Dengan spread 1–12 bps,
  setiap round-trip maker kira-kira rugi 10–15 bps. Ini konsekuensi jadwal fee,
  bukan bug — bot ini optimize volume, bukan profit.

## Uji offline

```bash
python tests/test_offline.py
```

Menjalankan `run_account()` dengan client palsu (tanpa jaringan, tanpa SDK) dan
memastikan order beli **dan** jual benar-benar dipasang, ukurannya di atas
minimum, harganya di dalam spread, plus regresi rumus lama (0 kuotasi) dan
karantina policy.

Validasi encoding memakai kode SDK asli (butuh SDK terpasang, tidak mengirim
order, tidak butuh kredensial):

```bash
python tests/validate_sdk_encoding.py
```

Skrip ini mengambil katalog publik testnet, membentuk order beli post-only untuk
setiap pair seperti yang dilakukan bot, lalu meng-encode-nya lewat
`polyester.codecs.orders`. Hasil saat audit: **21 pair ter-encode, 0 gagal**
(`limit_gtc`, `post_only=True`, `price_ticks` dan `base_qty_scaled` sesuai).
Dari situ juga terlihat bahwa SDK hanya menolak qty/harga yang jumlah desimalnya
melebihi scale pair; keselarasan tick/step yang sebenarnya tetap divalidasi
server.
