# Panduan Instalasi

Panduan langkah demi langkah, dari nol sampai bot jalan. Tidak perlu paham
Python — cukup ikuti perintahnya.

Kalau hanya butuh ringkasan cepat, lihat [README.md](README.md).

---

## 0. Ringkasan cepat

```bash
git clone https://github.com/0xDimzy/polyester-bot.git
cd polyester-bot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # isi 3 nilai
python doctor.py
python bot.py
```

Sisanya di bawah ini adalah penjelasan tiap langkah + solusi kalau macet.

---

## 1. Pastikan Python 3.11 atau lebih baru

```bash
python --version
```

Kalau muncul `Python 3.11.x` / `3.12.x` / `3.13.x` → lanjut.
Kalau muncul `3.10` atau lebih kecil, atau `command not found`:

| OS | Cara memasang |
| --- | --- |
| Windows | unduh dari [python.org/downloads](https://www.python.org/downloads/), **centang "Add python.exe to PATH"** saat instalasi |
| macOS | `brew install python@3.12` (atau unduh dari python.org) |
| Ubuntu/Debian | `sudo apt update && sudo apt install python3 python3-venv python3-pip` |

Di macOS/Linux biasanya perintahnya `python3`, bukan `python`. Ganti saja di
semua perintah berikutnya.

---

## 2. Ambil kode

```bash
git clone https://github.com/0xDimzy/polyester-bot.git
cd polyester-bot
```

Tidak punya git? Buka halaman repo → tombol **Code** → **Download ZIP**, lalu
ekstrak dan masuk ke foldernya.

---

## 3. Buat virtual environment

Virtual environment = "kotak" terpisah untuk dependency bot, supaya tidak
mengacak-acak Python sistem.

```bash
python -m venv .venv
```

Aktifkan:

| Shell | Perintah |
| --- | --- |
| macOS / Linux / Codespace / Git Bash | `source .venv/bin/activate` |
| Windows PowerShell | `.venv\Scripts\Activate.ps1` |
| Windows CMD | `.venv\Scripts\activate.bat` |

Kalau berhasil, prompt terminal akan mulai dengan `(.venv)`.

> **Windows: `Activate.ps1 cannot be loaded`**
> Jalankan sekali: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`
> lalu ulangi perintah aktivasinya.

---

## 4. Install dependency

```bash
pip install -r requirements.txt
```

Isinya cuma dua: `polyester-sdk` dan `python-dotenv`.

> ⚠️ **Jangan** `pip install polyester`. Paket itu bernama sama tapi isinya
> library R ↔ Python, dan akan merusak `import polyester`. Selalu lewat
> `requirements.txt`.

Cek hasilnya:

```bash
python -c "import polyester, dotenv; print('dependency OK')"
```

Kalau muncul `dependency OK`, lanjut.

<details>
<summary>Kalau `pip` tidak dikenali</summary>

Pakai `python -m pip install -r requirements.txt` (Windows: `py -m pip ...`).
</details>

---

## 5. Isi kredensial (`.env`)

```bash
cp .env.example .env          # Windows CMD: copy .env.example .env
```

Buka `.env` dengan editor, isi tiga nilai:

```env
POLYESTER_API_KEY_ID_1=...
POLYESTER_API_PRIVATE_KEY_1=...
POLYESTER_ACCOUNT_ID_1=...
```

**Dari mana?**

| Nilai | Lokasi di aplikasi Polyester |
| --- | --- |
| API key id + private key | sidebar **API** → buat key baru. Private key **hanya ditampilkan sekali** |
| Account ID | **Profile** → **Account ID** |

**Jangan lupa izinnya.** Di halaman API key → **Permissions**:

* aktifkan **Spot trading**;
* pilih market yang boleh ditradingkan (kalau tidak semua, bot akan
  mengarantina pair yang ditolak);
* set **maximum order size** yang cukup (bot memakai ±1,5% equity per order).

Kalau key-nya milik subaccount, ada dua lapis izin: policy API key **dan**
policy subaccount — keduanya harus mengizinkan.

> 🔒 `.env` berisi kunci rahasia. File ini sudah masuk `.gitignore`, jadi tidak
> akan ikut ter-commit. Jangan pernah kirim isinya ke orang lain atau ke chat.

**Saldo harus di bucket trading.** Deposit baru masuk ke *funding* dan tidak
bisa dipakai order. Pindahkan lewat menu transfer internal di aplikasi
(funding → trading). Bot akan memberi peringatan kalau mendeteksi dana di
funding.

---

## 6. Cek koneksi & izin

```bash
python doctor.py
```

Yang perlu dipastikan dari outputnya:

* `pair : 25` — katalog terbaca;
* tabel saldo menampilkan `USDT`/aset Anda di kolom **trading**;
* daftar spread terisi (bukan `book satu sisi` semua);
* bagian akhir: `admissible: True`.

Lalu uji benar-benar mengirim satu order kecil:

```bash
python doctor.py --place
```

Harus muncul `OK status: accepted`, lalu `order dibatalkan`. Kalau di sini
sudah ditolak, bot juga akan ditolak — baca kode error yang dicetak
(`ERROR_CODE_...`) dan cocokkan dengan tabel di [README.md](README.md#kalau-ada-error).

---

## 7. Uji tanpa mengirim order

```bash
BOT_DRY_RUN=1 BOT_MAX_CYCLES=2 python bot.py
```

Windows PowerShell:

```powershell
$env:BOT_DRY_RUN="1"; $env:BOT_MAX_CYCLES="2"; python bot.py
```

Yang dicari: baris `21/21 pair akan dikuotasi`, lalu banyak `[DRY] ... BUY`.
Kalau `dipasang=0`, jangan lanjut — kirim isi `bot.log`.

---

## 8. Jalankan sungguhan

```bash
python bot.py
```

Biarkan berjalan. Log muncul di layar dan sekaligus ditulis ke `bot.log`.

Berhenti kapan saja dengan `Ctrl+C` — bot akan membatalkan semua order-nya
sendiri sebelum keluar (butuh beberapa detik; jangan tutup jendela paksa).

---

## 9. Multi akun

Tambahkan blok `_2`, `_3`, ... di `.env`:

```env
POLYESTER_API_KEY_ID_1=...
POLYESTER_API_PRIVATE_KEY_1=...
POLYESTER_ACCOUNT_ID_1=...

POLYESTER_API_KEY_ID_2=...
POLYESTER_API_PRIVATE_KEY_2=...
POLYESTER_ACCOUNT_ID_2=...
```

Satu proses menjalankan semuanya paralel, log ditandai `[akun1]`, `[akun2]`.
Saat start bot mencetak `account_id` tiap worker — pastikan memang berbeda.

Aturannya:

1. Penomoran mulai dari `_1`, tidak boleh lompat di awal.
2. Satu `account_id` hanya sekali (kalau dobel, akun kedua dilewati — dua
   worker di akun yang sama akan saling membatalkan order).
3. Entri setengah terisi dilewati, akun lain tetap jalan.
4. Setiap API key punya izin sendiri.

Diagnosa per akun: `python doctor.py --account 2`.

---

## 10. Menjalankan 24/7 (opsional)

**Linux/macOS, cara paling sederhana:**

```bash
nohup .venv/bin/python bot.py >> bot.out 2>&1 &
tail -f bot.log
```

**Pakai screen/tmux** (lebih enak dilihat):

```bash
tmux new -s polyester
source .venv/bin/activate && python bot.py
# lepas: Ctrl+B lalu D   |   balik lagi: tmux attach -t polyester
```

**systemd** (kalau pakai VPS, otomatis hidup lagi setelah reboot) — simpan
sebagai `/etc/systemd/system/polyester-bot.service`:

```ini
[Unit]
Description=Polyester testnet bot
After=network-online.target

[Service]
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/polyester-bot
ExecStart=/home/YOUR_USER/polyester-bot/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now polyester-bot
sudo journalctl -u polyester-bot -f
```

---

## 11. Checklist kalau gagal

| Gejala | Cek |
| --- | --- |
| `No module named polyester` | venv belum aktif, atau salah paket (harus `polyester-sdk`) |
| `No module named dotenv` | `pip install -r requirements.txt` belum dijalankan |
| `Tidak ada akun ditemukan` | `.env` tidak ada / salah nama file / variabel belum diisi |
| `UNAUTHENTICATED` | key id atau private key salah; cek juga jam komputer |
| `INSUFFICIENT_FUNDS` | saldo di funding, atau tidak ada di bucket trading |
| `ERROR_CODE_POLICY_MARKET_DENY` | izin market di API key belum mencakup pair itu |
| `Tidak ada pair berquote` | katalog gagal; jalankan `python doctor.py` |
| `dipasang=0` di dry run | kirim 20 baris terakhir `bot.log` |

Masih mentok? Sertakan hasil `python doctor.py --place` atau 20–30 baris
terakhir `bot.log` saat bertanya — dua itu hampir selalu cukup untuk menemukan
penyebabnya.
