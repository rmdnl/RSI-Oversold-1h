# Binance Spot RSI Bot

Bot trading spot Binance (Python) berbasis RSI, multi pair, semua parameter lewat `.env`.

## Daftar isi

1. [Logika strategi](#logika-strategi)
2. [Struktur file](#struktur-file)
3. [Menyiapkan API key dan Telegram](#menyiapkan-api-key-dan-telegram)
4. [Instalasi di VPS Ubuntu](#instalasi-di-vps-ubuntu)
5. [Parameter `.env`](#parameter-env)
6. [Operasional harian](#operasional-harian)
7. [Troubleshooting](#troubleshooting)
8. [Fee dan ukuran order](#fee-dan-ukuran-order)
9. [Git](#git)
10. [Kepatuhan aturan API Binance](#kepatuhan-aturan-api-binance)
11. [Batasan dan risiko](#batasan-dan-risiko)

## Logika strategi

- RSI Wilder (sama dengan `ta.rsi` TradingView), periode `RSI_PERIOD`, pada `TIMEFRAME` (default 1h).
- Hanya memakai candle yang **sudah close**. Bot mengevaluasi sekali per candle per pair.
- **Sinyal BUY**: RSI menembus naik `RSI_TRIGGER` (18) dan dalam episode di bawah trigger itu RSI pernah `<= RSI_OVERSOLD` (16).
  Contoh: RSI 30 -> 15 -> 17 -> 18,5 menghasilkan sinyal. RSI 30 -> 17 -> 18,5 tidak (tidak pernah menyentuh 16).
- Sinyal yang sudah lebih dari `SIGNAL_MAX_AGE_SECONDS` sejak candle close dilewati (mencegah beli telat setelah bot lama mati).
- **Entry**: MARKET BUY senilai `QUOTE_PER_TRADE`.
- **Exit**: order OCO di Binance, TP `LIMIT_MAKER` (+`TP_PCT`%) dan SL `STOP_LOSS_LIMIT` (-`SL_PCT`%). TP/SL tetap aktif walau bot atau VPS mati.
- Satu posisi per pair, maksimum `MAX_OPEN_POSITIONS` posisi sekaligus.

## Struktur file

| File | Fungsi |
|---|---|
| `bot.py` | Seluruh kode bot |
| `.env.example` | Template konfigurasi (salin menjadi `.env`) |
| `requirements.txt` | Dependensi (`requests`, `python-dotenv`) |
| `binance-rsi-bot.service` | Unit systemd |
| `.gitignore` | Mengecualikan `.env`, `data/`, `state*.json`, dan cache Python dari git |
| `data/state.json` | Posisi terbuka dan candle terakhir yang diproses. **Jangan dihapus saat ada posisi terbuka** |
| `data/trades.csv` | Riwayat trade tertutup (TP, SL, atau EMERGENCY_SELL) |
| `data/bot.log` | Log (rotasi otomatis, 5 MB x 3 file) |
| `data/bot.lock` | Penjaga agar hanya satu instance yang jalan |

`.env` dibaca dari folder yang sama dengan `bot.py`, bukan dari folder tempat Anda menjalankan perintah.

## Menyiapkan API key dan Telegram

**API key Binance (mainnet)**

1. Masuk ke Binance, buka menu **API Management**, lalu buat API key baru (beri label, mis. `rsi-bot`).
2. Di pengaturan pembatasan key: aktifkan **Spot trading**, biarkan **Withdrawals nonaktif**, dan batasi akses ke **IP VPS** Anda.
3. Salin API key dan secret ke `.env` (`BINANCE_API_KEY`, `BINANCE_API_SECRET`). Secret hanya ditampilkan sekali.

**API key testnet**

1. Buka https://testnet.binance.vision, login, lalu buat HMAC key lewat tombol generate di halaman itu.
2. Key testnet terpisah dari key mainnet. Pakai bersama `BINANCE_TESTNET=true`.

**Telegram (opsional)**

1. Chat dengan `@BotFather`, kirim `/newbot`, ikuti petunjuknya, lalu salin token ke `TELEGRAM_BOT_TOKEN`.
2. Kirim satu pesan apa saja ke bot baru Anda.
3. Buka `https://api.telegram.org/bot<TOKEN>/getUpdates` di browser, cari `"chat":{"id":...}`, lalu salin angkanya ke `TELEGRAM_CHAT_ID`.

Bot mengirim notifikasi saat mulai, beli terisi, OCO terpasang, TP/SL kena, error penting, dan saat Binance membatasi akses.

## Instalasi di VPS Ubuntu

```bash
sudo apt update && sudo apt install -y python3 python3-venv chrony
sudo useradd -m -s /bin/bash botuser
sudo mkdir -p /opt/binance-rsi-bot && sudo chown botuser: /opt/binance-rsi-bot
sudo -u botuser -i
cd /opt/binance-rsi-bot
# salin bot.py, requirements.txt, .env.example ke folder ini
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env && nano .env
```

Urutan uji yang disarankan:

1. `DRY_RUN=true` lalu `./venv/bin/python bot.py --check` (validasi pair, filter, koneksi, tampilkan RSI saat ini; tanpa trading).
2. `DRY_RUN=true` dan jalankan `./venv/bin/python bot.py` beberapa jam untuk melihat log sinyal.
3. `BINANCE_TESTNET=true`, `DRY_RUN=false` dengan API key testnet untuk menguji order dan OCO sungguhan.
4. Mainnet dengan `QUOTE_PER_TRADE` kecil dulu.

Jalankan sebagai service:

```bash
sudo cp binance-rsi-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now binance-rsi-bot
journalctl -u binance-rsi-bot -f        # log live
tail -f /opt/binance-rsi-bot/data/bot.log
```

Jika folder atau user berbeda dari contoh di atas, sesuaikan `User`, `WorkingDirectory`, dan `ExecStart` di file service.

## Parameter `.env`

| Variabel | Default | Keterangan |
|---|---|---|
| `BINANCE_API_KEY`, `BINANCE_API_SECRET` | kosong | Wajib bila `DRY_RUN=false` |
| `BINANCE_TESTNET` | `false` | `true` memakai testnet |
| `DRY_RUN` | `true` | `true` hanya mencatat sinyal, tidak mengirim order (tidak butuh API key) |
| `SYMBOLS` | wajib | Pair dipisah koma, mis. `BNBUSDT,ETHUSDT`. Semua pair harus punya quote asset yang sama |
| `TIMEFRAME` | `1h` | `15m`, `30m`, `1h`, `2h`, `4h`, atau `1d` |
| `RSI_PERIOD` | `14` | Minimal 2 |
| `RSI_OVERSOLD` | `16` | Harus `0 < oversold < trigger < 100` |
| `RSI_TRIGGER` | `18` | Level yang harus ditembus naik untuk memicu BUY |
| `TP_PCT` | `2` | Persen dari harga entry, rentang 1-3 |
| `SL_PCT` | `2.5` | Persen dari harga entry, rentang 2-3 |
| `SL_TYPE` | `STOP_LOSS_LIMIT` | Atau `STOP_LOSS` (market saat stop kena: pasti terisi, ada slippage) |
| `SL_LIMIT_BUFFER_PCT` | `0.3` | Selisih harga limit di bawah harga stop (hanya untuk `STOP_LOSS_LIMIT`), rentang 0-2 |
| `QUOTE_PER_TRADE` | wajib | Nilai per entry dalam quote asset (mis. USDT). Minimal ~1,1x min notional pair |
| `MAX_OPEN_POSITIONS` | `3` | Batas posisi terbuka sekaligus |
| `POLL_INTERVAL_SECONDS` | `30` | Interval siklus bot (10-300) |
| `CANDLE_DELAY_SECONDS` | `3` | Jeda setelah candle close sebelum dievaluasi (0-60) |
| `SIGNAL_MAX_AGE_SECONDS` | `600` | Sinyal yang lebih tua dari ini dilewati (minimal 60) |
| `KLINE_LIMIT` | `500` | Jumlah candle untuk menghitung RSI (150-1000) |
| `RECV_WINDOW_MS` | `5000` | Toleransi timestamp request (1000-60000) |
|| `EMERGENCY_SELL_ON_OCO_FAIL` | `true` | Jual market bila OCO gagal terpasang 2x ||
|| `DATA_DIR` | `./data` | Lokasi state, log, dan riwayat trade ||
|| `STATE_FILE` | (otomatis) | Nama file state dalam `DATA_DIR`. Kosong = default per venue: `state.json` (mainnet) atau `state.testnet.json` (testnet). Jika diisi, harus path relatif (mis. `subdir/state.json`), tanpa `..` atau absolut. Memungkinkan menjalankan mainnet & testnet bersamaan dengan file state terpisah. ||
|| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | kosong | Isi keduanya atau kosongkan keduanya ||

Bot menolak start dengan pesan `[CONFIG ERROR]` bila ada nilai yang tidak valid atau di luar rentang.

## Operasional harian

**Menghentikan bot**: `sudo systemctl stop binance-rsi-bot`. Order OCO yang sudah terpasang tetap aktif di Binance, jadi posisi tetap terlindungi saat bot mati.

**Restart**: posisi dibaca dari `data/state.json` dan dilanjutkan.
- Order beli yang statusnya belum pasti diverifikasi lewat client ID.
- Posisi yang belum terpasang OCO-nya akan dipasangkan.
- Posisi yang sudah punya OCO dipantau lagi sampai TP/SL kena.
- Sinyal dari candle yang sudah lewat `SIGNAL_MAX_AGE_SECONDS` tidak dieksekusi.

**Update kode**:
1. `sudo systemctl stop binance-rsi-bot`
2. Ganti `bot.py`
3. `./venv/bin/python bot.py --check`
4. `sudo systemctl start binance-rsi-bot`

**`--check`** aman dijalankan kapan saja, termasuk saat service berjalan (tidak trading dan tidak menulis state).

**Mengubah `.env`**: restart service agar berlaku. Menghapus sebuah pair dari `SYMBOLS` tidak menghentikan pemantauan posisi terbuka pair itu.

**Menutup posisi manual**: batalkan order OCO-nya di Binance (Open Orders), lalu jual koinnya manual bila perlu. Bot melihat OCO selesai tanpa order terisi, mengirim notifikasi, dan menghapus posisi itu dari pelacakan.

**Posisi berstatus `stuck`** (di `state.json`, juga ada pesan CRITICAL di log): bot berhenti mengurusnya, tetapi tetap menghitungnya terhadap `MAX_OPEN_POSITIONS`. Penyebabnya: qty terlalu kecil untuk order TP/SL, atau market sell darurat gagal. Cek saldo dan Open Orders di Binance, tutup posisi secara manual, lalu hentikan bot, hapus entri pair itu dari `positions` di `state.json`, dan jalankan bot lagi.

**Jangan**: menjalankan dua bot dengan API key yang sama, menghapus `state.json` saat ada posisi terbuka, atau me-restart berulang-ulang saat Binance sedang membatasi akses.

## Troubleshooting

**Kode exit**

| Kode | Arti |
|---|---|
| `2` | Konfigurasi salah (pesan `[CONFIG ERROR]`). systemd tidak me-restart otomatis. Perbaiki `.env` lalu start ulang |
| `3` | Instance lain sudah berjalan (`bot.lock` terkunci) |
| `1` | Gagal saat startup (jaringan atau Binance). systemd mencoba lagi setelah 10 detik |

**Masalah umum**

| Gejala | Penyebab dan solusi |
|---|---|
| `Jam VPS melenceng >1 detik` | Aktifkan NTP: `sudo systemctl enable --now chrony`, cek dengan `timedatectl status`. Error `-1021` ditangani otomatis dengan sinkronisasi ulang |
| `-2015` / `Invalid API-key, IP, or permissions` | Key salah, IP VPS belum di-whitelist, atau izin Spot trading belum aktif. Pastikan key mainnet tidak dipakai di testnet atau sebaliknya |
| `Simbol tidak ditemukan` / `status ... bukan TRADING` | Pair salah ketik atau sedang tidak diperdagangkan. Pair juga harus mendukung OCO |
| `QUOTE_PER_TRADE ... terlalu kecil` | Naikkan nilainya sampai di atas ~1,1x min notional pair (sering 5 USDT, jadi minimal sekitar 5,5; disarankan 10 atau lebih) |
| `saldo ... < QUOTE_PER_TRADE, entry dilewati` | Saldo quote asset bebas tidak cukup. Entry baru dicoba pada sinyal berikutnya |
| `sinyal sudah basi` | Bot terlambat memproses sinyal (mati atau terhambat). Sengaja dilewati |
| `Menahan request ... karena 429` | Rate limit tercapai. Bot menunggu sesuai `Retry-After`. Cek apakah ada program lain yang memakai IP yang sama |
| `IP di-ban Binance` (418) atau 403 | Bot berhenti request sampai masa ban selesai (ban berkisar dari 2 menit sampai 3 hari untuk pelanggar berulang). Jangan restart berulang |
| `OCO ditolak` | Harga bergerak terlalu cepat atau filter pair tidak terpenuhi. Setelah 2x gagal bot menjual market (jika `EMERGENCY_SELL_ON_OCO_FAIL=true`) |
| `data/state.json rusak` | Bot sengaja menolak start. Periksa isinya, perbaiki JSON-nya, atau rename file bila yakin tidak ada posisi terbuka |
| Tidak ada sinyal berhari-hari | Normal untuk RSI 16/18 pada 1H. Lihat baris log `RSI prev=... cur=...` per candle untuk memastikan bot hidup |

## Fee dan ukuran order

- Fee spot umumnya 0,1% per transaksi, tarif akun Anda bisa berbeda. Bila fee dibayar dengan BNB, ada diskon (bila fitur itu aktif di akun).
- Satu siklus beli dan jual berarti dua kali fee. Dengan TP 1%, keuntungan bersih bisa tinggal sekitar 0,8%; dengan SL 2%, kerugian bersih sekitar 2,2%. Pertimbangkan ini saat memilih `TP_PCT` dan `SL_PCT`.
- Kolom `pnl_quote` dan `pnl_pct` di `data/trades.csv` dihitung **sebelum fee**.
- Bila fee dipotong dari koin yang dibeli, bot mengurangkannya dari qty sebelum memasang OCO, jadi qty OCO sedikit lebih kecil dari qty beli.
- Bot memvalidasi saat startup bahwa `QUOTE_PER_TRADE` cukup besar agar order TP/SL memenuhi `NOTIONAL` pair.

## Git

- `.gitignore` mengecualikan `.env`, `.env.*`, `data/`, `state*.json`, `venv/`, `.venv/`, log, dan cache Python. `.env.example` tetap ikut di-commit.
- Nama file **wajib** diawali titik (`.gitignore`, `.env.example`). File `gitignore` atau `env.example` tidak pernah dibaca git dan tidak pernah ditemukan oleh perintah di README ini. Setelah mengubah nama file, buktikan dengan `git check-ignore -v .env` (harus mengembalikan satu baris aturan, bukan kosong).
- Jangan pernah commit `.env`. Bila terlanjur ter-commit: `git rm --cached .env`, lalu **buat API key baru** di Binance karena key lama sudah tersimpan di riwayat git.
- **State per environment**: Bot menulis venue (`mainnet`/`testnet`) ke `state.json`. Saat startup, bot memvalidasi kecocokan venue. Jika state file milik venue berbeda **dan memiliki posisi terbuka**, bot menolak start dengan `[CONFIG ERROR]`. Jika tidak ada posisi, bot adopsi venue saat ini. Gunakan `STATE_FILE` untuk nama file state kustom.

## Kepatuhan aturan API Binance

- Semua request signed HMAC-SHA256 dengan `timestamp` dan `recvWindow`; jam disinkronkan dengan `/api/v3/time` dan otomatis diulang bila `-1021`.
- Weight dibaca dari header `X-MBX-USED-WEIGHT-1M`; bot menahan diri bila pemakaian lewat 80% batas.
- HTTP 429: berhenti sesuai `Retry-After`. HTTP 418/403: bot berhenti request sampai ban selesai (tidak menembak ulang).
- Kline hanya diambil sekali per candle per pair; posisi dipantau dengan polling ringan (`GET orderList`, weight kecil) tiap `POLL_INTERVAL_SECONDS`.
- Order diberi `newClientOrderId` unik. Jika respons order hilang (timeout/5xx), bot memverifikasi lewat client ID dan tidak mengirim ulang buta, sehingga tidak ada order ganda.
- Kuantitas dan harga dibulatkan memakai `PRICE_FILTER`, `LOT_SIZE`, dan `NOTIONAL` dari `exchangeInfo`; fee dalam base asset dikurangkan sebelum memasang OCO.
- Satu instance saja (file lock) agar tidak terjadi order ganda.

## Batasan dan risiko

- `STOP_LOSS_LIMIT` bisa tidak terisi bila harga gap melewati harga limit. Gunakan `SL_TYPE=STOP_LOSS` bila lebih mengutamakan kepastian keluar.
- Bila OCO gagal terpasang 2x, bot menjual market (`EMERGENCY_SELL_ON_OCO_FAIL`). Posisi yang ditandai `stuck` perlu ditangani manual (lihat Operasional harian).
- Bot tidak mengambil alih koin yang sudah ada di akun sebelum bot jalan; hanya posisi yang dibuatnya sendiri yang dilacak.
- Ini bukan jaminan profit dan strategi belum di-backtest. Mulai dengan nominal kecil.
