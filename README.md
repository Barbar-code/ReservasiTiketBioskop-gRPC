# Reservasi Tiket Bioskop — Sistem Terdistribusi Berbasis gRPC

Proyek mini mata kuliah **Komputasi Paralel dan Terdistribusi** (topik: RPC).
Beberapa client dapat memesan kursi yang sama secara bersamaan — sistem dijamin
tidak pernah menjual satu kursi dua kali (pemesanan atomik dengan lock per
jadwal; alasan pemilihan desain dijelaskan di laporan).

Teknologi: **Python 3.10+**, `grpcio`, `grpcio-tools`, Protocol Buffers, HTTP/2.

Status pengerjaan:
- Kontrak `proto/bioskop.proto` sudah **disepakati**; core server — booking
  atomik per jadwal, servicer 5 RPC, event hub streaming — oleh **Anggota 1**.
- CLI client + wrapper retry, `DedupStore` + `--simulate-slow`, uji dedup,
  benchmark kinerja, dan deployment Docker oleh **Anggota 2** (terintegrasi di
  branch `fitur/integrasi-a1-a2`).

## Instalasi

```powershell
python -m venv .venv
.venv\Scripts\activate        # POSIX: source .venv/bin/activate
python -m pip install -r requirements.txt
```

Dependensi hanya: `grpcio`, `grpcio-tools`, `pytest` (+ pustaka standar).

## Generate stub gRPC

Stub `*_pb2.py` / `*_pb2_grpc.py` **tidak di-commit**; di-generate menjadi
paket `proto/`:

```powershell
python -m grpc_tools.protoc -I . --python_out=. --grpc_python_out=. proto/bioskop.proto
```

Alternatif: `make proto` atau `scripts/gen_proto.sh` (POSIX).
`tests/conftest.py` juga otomatis me-generate stub bila belum ada.

## Menjalankan server

```powershell
python -m server.main --port 50051 --workers 10
```

| Argumen | Arti |
|---|---|
| `--port` | port yang didengarkan (default 50051) |
| `--web-host` / `--web-port` | alamat dan port dashboard web (default `0.0.0.0:8080`) |
| `--workers` | besar `ThreadPoolExecutor` (default 10) |
| `--simulate-slow` | jeda balasan acak **setelah** operasi selesai — untuk demo retry client + dedup server |
| `--jeda-min` / `--jeda-maks` | batas jeda simulasi (detik; default 0,3 / 1,5) |

Server menyediakan dua jadwal seed — **`jadwal-1`** ("Langit Senja", 13:00) dan
**`jadwal-2`** ("Petualangan Rimba", 16:00) — denah 6 baris × 10 kursi.
Data pemesanan berada di memori dan kembali ke kondisi awal saat server
dimulai ulang. Setiap jadwal memiliki lock tersendiri; semua kursi dalam satu
transaksi diperiksa dan dipesan dalam critical section yang sama. Peristiwa
streaming dipancarkan setelah lock dilepas agar subscriber tidak memperlambat
pemesanan.

### Dashboard web

Server juga menyajikan aplikasi web responsif tanpa login atau dependensi
frontend tambahan. Jalankan server seperti biasa, lalu buka
**http://localhost:8080**. Dashboard menampilkan jadwal dan denah kursi,
memperbarui status kursi secara realtime, serta mendukung pemesanan dan
pembatalan. Riwayat ditampilkan dari penyimpanan lokal browser (maksimal 10
pesanan); sumber data dan konsistensi transaksi tetap ditangani server gRPC.

Untuk akses dari perangkat lain, buka `http://<alamat-server>:8080` dan
pastikan port web diizinkan firewall. Di Docker Compose, dashboard tersedia
melalui **http://localhost:8080**.

### Demo retry + dedup (`--simulate-slow`)

```powershell
python -m server.main --port 50051 --workers 10 --simulate-slow
# di client, tetap jalankan pesan normal:
python -m client.main pesan --jadwal jadwal-1 --nama Dzikra --kursi A1,B2
```

Jika balasan lambat melewati timeout 2 s → client retry dengan `request_id`
yang sama → server menjawab dari `DedupStore` (pesanan tetap satu).

## Menjalankan client

```powershell
python -m client.main --host localhost:50051                     # menu interaktif
python -m client.main --host localhost:50051 jadwal
python -m client.main --host localhost:50051 denah --jadwal jadwal-1
python -m client.main --host localhost:50051 pesan --jadwal jadwal-1 --nama Dzikra --kursi A1,B2
python -m client.main --host localhost:50051 batal --pesanan PESAN-1
python -m client.main --host localhost:50051 stream --jadwal jadwal-1           # live, Ctrl+C berhenti
python -m client.main --host localhost:50051 stream --jadwal jadwal-1 --max-detik 10
```

- `--host` default: env `BIOSKOP_HOST`, bila tidak ada `localhost:50051`.
- `pesan`/`batal` mencetak **request_id**; `--request-id <id>` bisa dipakai
  untuk mengulang operasi dengan id SAMA (peragakan at-most-once).
- Ketahanan sudah tertanam (`client/resilient.py`): timeout 2 s, retry hanya
  untuk `DEADLINE_EXCEEDED`/`UNAVAILABLE`, backoff 0,5→1→2 s, maksimal 3
  percobaan; error tampil sebagai pesan terkontrol (bukan traceback).

## Menjalankan tes

```powershell
python -m pytest tests/ -v
```

- Anggota 1: `test_booking.py`, `test_race.py`, `test_events.py`,
  `test_service.py` (atomisitas, race condition, streaming).
- Anggota 2: `test_dedup.py` — request_id yang SAMA dikirim dua kali lewat
  server gRPC sungguhan → logika bisnis dieksekusi **tepat sekali** (§8 no. 3).

## Uji kinerja (1/5/20 client — Ketentuan Wajib no. 5)

```powershell
python bench/load_test.py --clients 1 5 20 --requests 200 --runs 3
```

Mencetak throughput (req/s) dan latensi rata-rata + p95 per
`(run, jumlah client)`, lalu menyimpan CSV ke `bench/results/kinerja_<waktu>.csv`.
Grafik dibuat manual (Excel/Sheets) dari CSV — tanpa dependensi tambahan.
`--jadwal` default `jadwal-1`; `--mode campur|denah|pesan` (default `campur`:
bergantian GetDenah + PesanKursi di kursi berbeda sesuai skenario §9; saat
alokasi kursi habis otomatis beralih ke GetDenah agar jumlah request setara).

## Docker (2 container — Ketentuan Wajib no. 4)

```powershell
docker compose -f docker/docker-compose.yml up --build
```

- container **server** (port host 50051) dan **client** ber Komunikasi lewat
  jaringan internal compose (`BIOSKOP_HOST=server:50051`); client membuka menu
  interaktif (TTY aktif).
- Satu perintah tanpa menu:
  `docker compose -f docker/docker-compose.yml run --rm client python -m client.main denah --jadwal jadwal-1`

## Struktur repo

```
proto/bioskop.proto       # kontrak RPC final (disepakati kedua anggota)
proto/*_pb2*.py           # hasil generate — tidak di-commit
server/
  main.py                 # entrypoint: --port --workers --simulate-slow (integrasi)
  booking.py              # denah + pemesanan atomik, lock per jadwal (Anggota 1)
  service.py              # servicer 5 RPC + DedupStore at-most-once (A1, dedup oleh A2)
  events.py               # SeatEventHub pub/sub streaming (Anggota 1)
  dedup.py                # DedupStore request_id → response, TTL, lock (Anggota 2)
client/
  resilient.py            # timeout + retry + backoff + request_id konsisten (A2)
  main.py                 # CLI subcommand + menu interaktif (Anggota 2)
tests/                    # test_booking/test_race/test_events/test_service (A1), test_dedup (A2)
bench/load_test.py        # kinerja 1/5/20 client → CSV (Anggota 2)
docker/                   # Dockerfile server & client + compose 2 container (A2)
web/                      # Dashboard responsif dan poster film lokal (SVG)
```

## Catatan desain penting (bahan laporan)

1. **`request_id` dibuat sekali per operasi logis** di `client/main.py`, bukan
   per percobaan; `client/resilient.py` mengirim ulang request yang SAMA.
2. **Konsistensi**: satu `threading.Lock` per jadwal; pemesanan multi-kursi
   all-or-nothing dalam satu critical section; event streaming dipancarkan
   setelah lock dilepas (Anggota 1).
3. **At-most-once**: `DedupStore` (in-memory, lock, TTL 10 menit, lazy cleanup)
   menyimpan `request_id → response`; request duplikat dijawab tanpa eksekusi
   ulang. Batasan: isi hilang saat restart; usulan perbaikan: file/SQLite
   (dijelaskan di laporan §3.4).
4. **`--simulate-slow` via interceptor**: servicer tidak perlu tahu mode
   simulasi; jeda setelah operasi selesai benar-benar memicu deadline client.
5. **Streaming**: batas `--max-detik` diimplementasi dengan deadline level
   panggilan (iterator gRPC tak menyediakan `next(timeout)`); `DEADLINE_EXCEEDED`
   padanya diperlakukan berhenti normal.
