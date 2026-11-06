# Polyester Testnet Bot

Bot sederhana untuk **Polyester Spot testnet**. Tugasnya: memasang order beli
dan jual di dalam spread supaya akun terus melakukan transaksi (volume), bukan
mengejar profit besar.

> **Baru pertama kali?** Ikuti [INSTALASI.md](INSTALASI.md) — ada langkah per
> langkah untuk Windows, macOS, Linux, dan Codespace.

## Apa yang bot ini lakukan

1. Membaca daftar pair dan aturan order dari katalog resmi (tick, step, minimum).
2. Membaca saldo, lalu memasang **order beli di best bid** dan **order jual di
   best ask** (post-only, jadi tidak pernah "makan" likuiditas).
3. Menunggu order kena. Kalau 2 menit belum kena, harga diganti mengikuti book.
4. Kalau 5 menit sama sekali tidak ada transaksi, mengambil likuiditas sekali
   supaya volume tetap jalan (`BOT_TAKER_AFTER`).

Yang **tidak** dilakukan: menjamin untung, arbitrase, atau menebak arah harga.
Di testnet, fee maker 10 bps sementara spread 1–12 bps, jadi tiap putaran
memang rugi tipis — wajar untuk farming volume.

## Yang dibutuhkan

* **Python 3.11+**
* **API key Polyester** (Spot trading aktif)
* Saldo di bucket **trading** (bukan funding)

## Jalankan dalam 5 langkah

```bash
# 1. ambil kode
git clone https://github.com/0xDimzy/polyester-bot.git
cd polyester-bot

# 2. siapkan environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. install dependency
pip install -r requirements.txt

# 4. isi kredensial
cp .env.example .env               # lalu edit .env (3 nilai)

# 5. cek dulu, baru jalankan
python doctor.py                   # tidak mengirim order
python bot.py
```

Kalau `python doctor.py` sudah menampilkan saldo Anda dan `admissible: True`,
bot siap jalan.

## Cara membaca log

```
10:34:56 INFO [akun1] Equity awal: 1980.91 USDT.
10:34:58 INFO [akun1] 21/21 pair akan dikuotasi (mode volume).
10:34:59 INFO [akun1] OK   BTC-USDT BUY  qty=0.000389 @ 76196.89 (accepted)
10:35:30 INFO [akun1] TERISI BTC-USDT BUY 0.000389/0.000389 @ 76196.89 (filled)
10:35:30 INFO [akun1] Ringkasan: 1.2 mnt | order dipasang=21 aktif=21 gagal=0 terisi=1 volume=29.6
```

| Yang Anda lihat | Artinya |
| --- | --- |
| `21/21 pair akan dikuotasi` | semua pair siap; kalau `0/25` ada masalah katalog |
| `OK   ... (accepted)` | order diterima bursa |
| `TERISI ... (filled)` | order kena, volume bertambah |
| `[DRY] ...` | mode uji, tidak ada order sungguhan |
| `Alasan dilewati: ...` | kenapa suatu pair tidak memasang order |
| `gagal=N` | order ditolak; lihat baris `WARNING` di atasnya |

Semua log juga ditulis ke `bot.log` di folder yang sama.

## Kalau ada error

| Pesan | Artinya / solusi |
| --- | --- |
| `ERROR_CODE_POLICY_MARKET_DENY` | API key tidak diizinkan trading pair itu. Aktifkan di **API key → Permissions**, atau biarkan bot melewatinya |
| `INSUFFICIENT_FUNDS` + saldo `funding > 0` | dana masih di funding; transfer **funding → trading** |
| `ERROR_CODE_MIN_NOTIONAL` | order di bawah 5 USDT; naikkan `BOT_ORDER_PCT` |
| `ERROR_CODE_POST_ONLY_CROSS` | book bergerak saat order dikirim; bot otomatis coba ulang |
| `UNAUTHENTICATED` | key id / private key salah, atau jam komputer melenceng > 5 detik |
| `Tidak ada pair berquote` | katalog gagal dibaca; jalankan `python doctor.py` |

Tidak yakin? Jalankan `python doctor.py --place` — ia mencetak kode error asli
dari bursa, bukan tebakan.

## Perintah yang sering dipakai

| Perintah | Fungsi |
| --- | --- |
| `python doctor.py` | cek katalog, saldo, fee, spread, dan preview order beli |
| `python doctor.py --place` | kirim 1 order beli kecil lalu batalkan (uji izin) |
| `python doctor.py --account 2` | diagnosa akun kedua |
| `BOT_DRY_RUN=1 python bot.py` | jalankan penuh tanpa kirim order |
| `python bot.py` | jalankan sungguhan |
| `Ctrl+C` | berhenti; order bot otomatis dibatalkan |

## Isi folder

| File | Isi |
| --- | --- |
| `bot.py` | bot-nya |
| `doctor.py` | alat diagnosa (tidak mengirim order kecuali `--place`) |
| `.env.example` | contoh konfigurasi, salin jadi `.env` |
| `INSTALASI.md` | panduan instalasi detail + jalan 24/7 |
| `AUDIT.md` | kenapa versi lama tidak pernah membeli (teknis) |
| `tests/` | tes offline, tidak butuh kredensial |

## Bacaan lanjut

* [INSTALASI.md](INSTALASI.md) — instalasi per OS, multi akun, jalan 24/7.
* [AUDIT.md](AUDIT.md) — temuan audit, batasan katalog, cara uji.
