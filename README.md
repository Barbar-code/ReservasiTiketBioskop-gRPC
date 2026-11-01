# Reservasi Tiket Bioskop — Sistem Terdistribusi Berbasis gRPC

Proyek Mini mata kuliah **Komputasi Paralel dan Terdistribusi** (topik: RPC).
Beberapa client dapat memesan kursi yang sama secara bersamaan — sistem dijamin
tidak pernah menjual satu kursi dua kali (race condition diatasi atomik dengan
lock per jadwal, statistik & alasan pemilihan disebut di laporan).

Teknologi: **Python 3.10+**, `grpcio`, `grpcio-tools`, Protocol Buffers, HTTP/2.

> Status saat ini: sisi **client, ketahanan, dedup, benchmark, dan deployment**
> (tugas Anggota 2) sudah terimplementasi dan teruji. Logika server inti
> (`server/service.py`, `server/booking.py`, `server/events.py`) masih dikerjakan
> Anggota 1; kontrak final `proto/bioskop.proto` menunggu persetujuan kedua
> anggota sebelum di-commit (§7 AGENTS.md). Selama itu, stub dikembangkan dari
> draft §3.2 secara lokal.

## Instalasi

```bash
python -m venv venv
venv\Scripts\activate        # Windows (bash: source venv/bin/activate)
pip install -r requirements.txt
```

## Generate stub gRPC

Stub `*_pb2.py` / `*_pb2_grpc.py` **tidak di-commit**; di-generate dari
`proto/bioskop.proto`:

```bash
python -m grpc_tools.protoc -I proto --python_out=. --grpc_python_out=. proto/bioskop.proto
```

(catatan: `tests/conftest.py` ikut me-generate stub otomatis bila belum ada.)

## Menjalankan server

```bash
python -m server.main --port 50051 --workers 10
```

Argumen:

| Argumen | Arti |
|---|---|
| `--port` | port yang didengarkan (default 50051) |
| `--workers` | besar `ThreadPoolExecutor` (default 10) |
| `--simulate-slow` | jeda balasan acak (0,3–1,5 s, diatur `--jeda-min/--jeda-maks`) SETELAH operasi selesai — untuk demo **retry client + dedup server** |
| `--jeda-min` / `--jeda-maks` | batas jeda simulasi (detik) |

> `--simulate-slow` diimplementasi lewat `ServerInterceptor` sehingga kode
> layanan tetap bersih. Cara demo: aktifkan flag, set timeout client 2 s,
> lalu `pesan` — pool balasan lambat bisa melewati deadline → client retry
> dengan `request_id` yang sama → server menjawab dari `DedupStore`
> (pesanan tetap satu).

## Menjalankan client

```bash
python -m client.main --host localhost:50051        # menu interaktif
python -m client.main --host localhost:50051 jadwal
python -m client.main --host localhost:50051 denah --jadwal J1
python -m client.main --host localhost:50051 pesan --jadwal J1 --nama Dzikra --kursi A1,B2
python -m client.main --host localhost:50051 batal --pesanan PESAN-1
python -m client.main --host localhost:50051 stream --jadwal J1           # pantau live (Ctrl+C stop)
python -m client.main --host localhost:50051 stream --jadwal J1 --max-detik 10
```

- `--host` default: env `BIOSKOP_HOST`, bila tidak ada `localhost:50051`.
- `pesan`/`batal` mencetak **request_id**; pakai `--request-id <id>` untuk
  mengulang operasi dengan id yang SAMA (peragakan at-most-once).
- Ketahanan sudah tertanam: timeout 2 s, retry hanya untuk
  `DEADLINE_EXCEEDED`/`UNAVAILABLE`, backoff 0,5→1→2 s, maksimal 3 percobaan.
  Error tampil sebagai pesan terkontrol (bukan traceback).

## Menjalankan tes

```bash
python -m pytest tests/ -v
```

Yang telah teruji (`tests/test_dedup.py`, tugas Anggota 2): aturan dasar
`DedupStore` (TTL, id kosong), serta **uji end-to-end**: request_id yang sama
dikirim dua kali lewat server gRPC sungguhan → logika bisnis dieksekusi
**tepat sekali**, respons identik; request_id berbeda → dieksekusi normal.
(Tes `test_booking.py`/`test_race.py` menyusul dari Anggota 1.)

## Uji kinerja (1/5/20 client)

```bash
python bench/load_test.py --clients 1 5 20 --requests 200 --runs 3
```

Mencetak throughput (req/s), latensi rata-rata + p95 per (run, jumlah client)
dan menyimpan CSV ke `bench/results/kinerja_<waktu>.csv`. Grafik dibuat
manual (Excel/Sheets) dari CSV — tanpa dependensi tambahan. Mode dapat
`--mode campur|denah|pesan` (default `campur`: bergantian GetDenah + PesanKursi
di kursi berbeda, sesuai skenario §9; pelepasan saat kursi habis menjalankan
GetDenah agar jumlah request tetap setara).

## Docker (2 container — Ketentuan Wajib no. 4)

```bash
docker compose -f docker/docker-compose.yml up --build
```

- container **server** (port host 50051) dan **client** berkomunikasi lewat
  jaringan internal compose (`BIOSKOP_HOST=server:50051`); client buka menu
  interaktif (TTY sudah aktif).
- Satu perintah tanpa menu:
  `docker compose -f docker/docker-compose.yml run --rm client python -m client.main denah --jadwal J1`

## Struktur repo

```
proto/bioskop.proto      # kontrak RPC (draft §3.2; final menunggu persetujuan kelompok)
server/
  main.py                # entrypoint: --port --workers --simulate-slow   (Anggota 2)
  dedup.py               # DedupStore request_id -> response, TTL, lock   (Anggota 2)
  service.py / booking.py / events.py                                    (Anggota 1)
client/
  resilient.py           # wrapper timeout + retry + backoff + request_id (Anggota 2)
  main.py                # CLI subcommand + menu interaktif               (Anggota 2)
tests/ test_dedup.py (A2) | test_booking.py, test_race.py (A1)
bench/ load_test.py + results/*.csv                                      (Anggota 2)
docker/ Dockerfile.server, Dockerfile.client, docker-compose.yml         (Anggota 2)
```

## Catatan desain penting (bahan laporan)

1. **`request_id` dibuat sekali per operasi logis** di `client/main.py`, bukan
   per percobaan; `client/resilient.py` mengirim ulang request yang SAMA.
2. **DedupStore** (in-memory, lock, TTL 10 mtn, lazy cleanup) menjamin
   at-most-once untuk operasi non-idempoten. Batasan: isi hilang saat server
   restart; usulan perbaikan: file/SQLite (dijelaskan di laporan §3.4).
3. **`--simulate-slow` via interceptor**: servicer tidak perlu tahu mode
   simulasi; jeda SETELAH operasi selesai benar-benar memicu deadline client.
4. **Streaming**: pada `stream`, batas `--max-detik` diimplementasi dengan
   deadline level-panggilan (iterator gRPC tak menyediakan `next(timeout)`);
   `DEADLINE_EXCEEDED` padanya diperlakukan berhenti normal.
