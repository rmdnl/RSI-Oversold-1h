# Binance Spot RSI Bot

Bot trading spot Binance (Python) berbasis RSI, multi pair, semua parameter lewat `.env`.

## Logika

- RSI Wilder (sama dengan `ta.rsi` TradingView), periode `RSI_PERIOD`, pada `TIMEFRAME` (default 1h).
- Hanya memakai candle yang **sudah close**. Bot mengevaluasi sekali per candle per pair.
- **Sinyal BUY**: RSI menembus naik `RSI_TRIGGER` (18) dan dalam episode di bawah trigger itu RSI pernah `<= RSI_OVERSOLD` (16).
- **Entry**: MARKET BUY senilai `QUOTE_PER_TRADE`.
- **Exit**: order OCO di Binance, TP `LIMIT_MAKER` (+`TP_PCT`%) dan SL `STOP_LOSS_LIMIT` (-`SL_PCT`%). TP/SL tetap aktif walau bot atau VPS mati.
- `TP_PCT` dibatasi 1-3 dan `SL_PCT` 2-3 (bot menolak start bila di luar rentang).
- Satu posisi per pair, maksimum `MAX_OPEN_POSITIONS` posisi sekaligus.

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

1. `DRY_RUN=true` lalu `./venv/bin/python bot.py --check` (validasi pair, filter, koneksi, tampilkan RSI saat ini).
2. `DRY_RUN=true` dan jalankan `./venv/bin/python bot.py` beberapa jam untuk melihat log sinyal.
3. `BINANCE_TESTNET=true`, `DRY_RUN=false` dengan API key testnet (https://testnet.binance.vision) untuk menguji order dan OCO sungguhan.
4. Mainnet dengan `QUOTE_PER_TRADE` kecil dulu.

Jalankan sebagai service:

```bash
sudo cp binance-rsi-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now binance-rsi-bot
journalctl -u binance-rsi-bot -f        # log live
tail -f /opt/binance-rsi-bot/data/bot.log
```

File di `data/`: `state.json` (posisi, jangan dihapus saat ada posisi terbuka), `trades.csv` (riwayat trade tertutup), `bot.log`, `bot.lock`.

## Keamanan API key

- Buat API key khusus bot: aktifkan hanya Spot trading, **matikan Withdrawals**, dan batasi ke IP VPS.
- `.env` harus `chmod 600`.

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
- Bila OCO gagal terpasang 2x, bot menjual market (`EMERGENCY_SELL_ON_OCO_FAIL`). Posisi yang ditandai `stuck` di `state.json` perlu ditangani manual, lalu hapus entrinya.
- Bot tidak mengimpor koin yang sudah ada di akun sebelum bot jalan; hanya posisi yang dibuatnya sendiri yang dilacak.
- Ini bukan jaminan profit dan strategi belum di-backtest. Mulai dengan nominal kecil.
