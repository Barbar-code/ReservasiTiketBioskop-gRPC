# AGENTS.md — Sistem Reservasi Tiket Bioskop Berbasis gRPC

Panduan kerja untuk seluruh kontributor (manusia maupun AI agent) pada proyek ini.
Baca seluruh dokumen ini sebelum menulis kode.

## 1. Ringkasan Proyek

- **Mata kuliah**: Komputasi Paralel dan Terdistribusi (topik: Remote Procedure Calls)
- **Tugas**: Proyek Mini — Sistem Terdistribusi Berbasis RPC
- **Studi kasus**: Reservasi tiket bioskop. Beberapa client memesan kursi yang sama secara bersamaan; sistem harus menjamin satu kursi tidak pernah terjual dua kali.
- **Teknologi**: Python 3.10+, `grpcio`, `grpcio-tools`, Protocol Buffers (IDL), HTTP/2.
- **Kelompok**: 2 orang (Anggota 1 dan Anggota 2). Durasi pengerjaan: 1 minggu.
- **Fitur RPC wajib studi kasus**: unary `PesanKursi`, server streaming status kursi, penanganan race condition.

## 2. Ketentuan Umum (Wajib Dipenuhi)

| No | Ketentuan | Status |
|----|-----------|--------|
| 1 | File `.proto` dengan **minimal 4 RPC**, termasuk **minimal 1 RPC streaming** | ☐ |
| 2 | Server melayani banyak client bersamaan **tanpa data rusak** (lock/mekanisme lain, pilihan dijelaskan) | ☐ |
| 3 | Client memakai **timeout dan retry**; operasi non-idempoten dilindungi **ID permintaan** | ☐ |
| 4 | Demo berjalan di **minimal 2 komputer atau 2 container** berbeda | ☐ |
| 5 | Uji kinerja: **latensi rata-rata dan throughput** untuk **1, 5, dan 20 client** bersamaan | ☐ |
| 6 | Repo Git dengan **riwayat commit dari setiap anggota** | ☐ |
| 7 | `README.md` berisi cara instalasi, generate stub, dan menjalankan server/client | ☐ |

Bobot penilaian (jadikan prioritas): Fungsionalitas 25%, Concurrency & konsistensi 20%, Desain `.proto` 15%, Toleransi kegagalan 15%, Analisis kinerja 15%, Laporan & presentasi 10%.

## 3. Desain Sistem

### 3.1 Arsitektur

```
 Client CLI (N proses) ──gRPC/HTTP2──► Server gRPC (ThreadPoolExecutor)
                                          ├─ BookingService (logika + lock per jadwal)
                                          ├─ DedupStore   (request_id → response)
                                          └─ SeatEventHub (pub/sub untuk streaming)
```

- Data disimpan **in-memory** (dict) dengan data seed film/jadwal. Persistensi bukan syarat; jika ditambahkan, jangan mengorbankan jadwal inti.
- Satu jadwal tayang (`showtime`) memiliki denah kursi, misalnya baris A–F × kolom 1–10.

### 3.2 Draft kontrak `proto/bioskop.proto`

Draft ini adalah titik awal; perubahan dibahas bersama oleh kedua anggota (lihat §7).

```proto
syntax = "proto3";
package bioskop;

service LayananBioskop {
  rpc ListJadwal   (ListJadwalRequest)   returns (ListJadwalResponse);        // unary
  rpc GetDenah     (DenahRequest)        returns (DenahResponse);             // unary
  rpc PesanKursi   (PesanKursiRequest)   returns (PesanKursiResponse);        // unary, NON-idempoten
  rpc BatalkanPesanan (BatalRequest)     returns (BatalResponse);             // unary, NON-idempoten
  rpc StreamStatusKursi (DenahRequest)   returns (stream PerubahanKursi);     // server streaming
}

message ListJadwalRequest  {}
message Jadwal   { string jadwal_id = 1; string film = 2; string jam = 3; int32 baris = 4; int32 kolom = 5; }
message ListJadwalResponse { repeated Jadwal jadwal = 1; }

message DenahRequest  { string jadwal_id = 1; }
message DenahResponse { string jadwal_id = 1; repeated Kursi kursi = 2; }
message Kursi { string kode = 1; StatusKursi status = 2; }   // kode: "A1", "B7", dst.
enum StatusKursi { TERSEDIA = 0; DIPESAN = 1; }

message PesanKursiRequest {
  string request_id = 1;            // UUID, SAMA untuk setiap retry
  string jadwal_id  = 2;
  string pemesan    = 3;
  repeated string kode_kursi = 4;   // semua-atau-tidak-sama-sekali
}
message PesanKursiResponse {
  bool sukses = 1; string pesan = 2; string pesanan_id = 3;
  repeated string kursi_gagal = 4;
}

message BatalRequest  { string request_id = 1; string pesanan_id = 2; }
message BatalResponse { bool sukses = 1; string pesan = 2; }

message PerubahanKursi { string jadwal_id = 1; string kode = 2; StatusKursi status = 3; string waktu = 4; }
```

Jumlah RPC = 5 (≥ 4), streaming = 1 (`StreamStatusKursi`). Gunakan `context.abort()` dengan kode status gRPC yang tepat (`NOT_FOUND`, `INVALID_ARGUMENT`, `FAILED_PRECONDITION`/`ALREADY_EXISTS`).

### 3.3 Concurrency & Konsistensi

- **Satu `threading.Lock` per jadwal** (bukan satu lock global) agar jadwal berbeda tidak saling menunggu.
- Pemesanan multi-kursi bersifat **atomik (all-or-nothing)**: cek semua kursi dahulu, baru tandai semuanya, dalam satu critical section. Jika ada yang sudah terpesan, tidak ada kursi yang berubah.
- Pemicu event streaming dilakukan **setelah** lock dilepas, supaya subscriber yang lambat tidak menahan lock.
- Streaming: tiap subscriber memegang `queue.Queue`; hub mem-broadcast `PerubahanKursi`. Wajib bersihkan subscriber saat client putus (`context.add_callback`) agar tidak bocor memori.
- Dokumentasikan alasan memilih lock (kesederhanaan, critical section pendek) di laporan.

### 3.4 Toleransi Kegagalan

- **Client**: setiap pemanggilan memakai `timeout` (default 2 detik). Retry hanya untuk `DEADLINE_EXCEEDED` dan `UNAVAILABLE`, dengan *exponential backoff* (0,5 s → 1 s → 2 s), maksimal 3 percobaan.
- **`request_id` (UUID) dibuat sekali per operasi logis dan dipakai ulang di setiap retry.** Jangan pernah membuat UUID baru per percobaan.
- **Server**: `DedupStore` menyimpan `request_id → response` (dilindungi lock, ada TTL pembersihan). Request duplikat mengembalikan response tersimpan tanpa eksekusi ulang → semantik **at-most-once**.
- Operasi idempoten (`ListJadwal`, `GetDenah`) boleh di-retry bebas tanpa `request_id`.
- Ada flag server `--simulate-slow` (balasan lambat acak setelah operasi selesai) untuk mendemokan retry + deduplikasi.
- Batasan yang harus dijelaskan di laporan: `DedupStore` in-memory hilang saat server restart (at-most-once gugur); usulan perbaikan = simpan ke file/SQLite.

## 4. Struktur Repositori

```
.
├── AGENTS.md
├── README.md
├── requirements.txt
├── Makefile / scripts/gen_proto.sh     # generate stub
├── proto/bioskop.proto
├── server/
│   ├── main.py            # entrypoint, argumen: --port --workers --simulate-slow
│   ├── service.py         # servicer gRPC (tipis, delegasi ke logika)
│   ├── booking.py         # logika kursi + lock per jadwal
│   ├── dedup.py           # DedupStore
│   └── events.py          # SeatEventHub
├── client/
│   ├── main.py            # CLI interaktif / subcommand
│   └── resilient.py       # wrapper timeout + retry + backoff
├── tests/
│   ├── test_booking.py    # unit test logika
│   ├── test_race.py       # uji race condition (banyak thread, kursi sama)
│   └── test_dedup.py      # retry tidak menggandakan pemesanan
├── bench/
│   ├── load_test.py       # 1, 5, 20 client → latensi & throughput
│   └── results/           # CSV + grafik
├── docker/
│   ├── Dockerfile.server
│   ├── Dockerfile.client
│   └── docker-compose.yml # 2 container: server & client
└── docs/                  # diagram arsitektur, bahan laporan (nanti)
```

File `*_pb2.py` / `*_pb2_grpc.py` **tidak** di-commit (di-generate lewat skrip); tambahkan ke `.gitignore`.

## 5. Perintah Umum

```bash
python3 -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt                      # grpcio, grpcio-tools
python -m grpc_tools.protoc -I proto --python_out=. --grpc_python_out=. proto/bioskop.proto
python -m server.main --port 50051 --workers 10
python -m client.main --host localhost:50051
pytest tests/ -v
python bench/load_test.py --clients 1 5 20 --host localhost:50051
docker compose -f docker/docker-compose.yml up --build
```

Sesuaikan jika struktur impor berubah; perbarui `README.md` pada saat yang sama.

## 6. Pembagian Tugas (Seimbang ±50 : 50)

Prinsip: **kedua anggota menyentuh sisi server DAN client**, supaya riwayat commit seimbang dan keduanya menguasai seluruh sistem saat presentasi. Perkiraan beban dalam jam kerja (total ≈ 28 jam, ≈ 14 jam per orang).

### Anggota 1 — "Core Server & Konsistensi"

| # | Tugas | Estimasi |
|---|-------|----------|
| 1 | Menyusun draft awal `bioskop.proto` + skrip generate stub | 1,5 j |
| 2 | `booking.py`: denah kursi, `PesanKursi` atomik, `BatalkanPesanan`, lock per jadwal | 4 j |
| 3 | `service.py`: servicer gRPC untuk 5 RPC, status error yang tepat | 2 j |
| 4 | `events.py` + `StreamStatusKursi` (server streaming, cleanup subscriber) | 2,5 j |
| 5 | `tests/test_booking.py` dan `tests/test_race.py` (bukti tidak ada double-booking) | 3 j |
| 6 | Pengayaan opsional: persistensi `DedupStore` ke SQLite/file | 1 j |
| | **Subtotal** | **≈ 14 j** |

### Anggota 2 — "Client, Ketahanan, Kinerja & Deployment"

| # | Tugas | Estimasi |
|---|-------|----------|
| 1 | Review `.proto` dan menambah/merevisi pesan sesuai kebutuhan client | 1 j |
| 2 | `client/main.py` (CLI: lihat jadwal, denah, pesan, batal, pantau stream) | 3 j |
| 3 | `client/resilient.py`: timeout, retry, backoff, `request_id` konsisten | 2,5 j |
| 4 | `dedup.py` + flag `--simulate-slow` + `tests/test_dedup.py` | 2,5 j |
| 5 | `bench/load_test.py` (1/5/20 client), tabel & grafik di `bench/results/` | 3 j |
| 6 | Docker (`Dockerfile.*`, `docker-compose.yml`) + uji di 2 container/komputer + `README.md` | 2 j |
| | **Subtotal** | **≈ 14 j** |

### Tugas bersama (dikerjakan berdua)

- Menyepakati kontrak `.proto` final **sebelum** implementasi besar (±1 jam, bersama).
- Saling *code review* lewat Pull Request (setiap PR di-review anggota lain).
- Skenario demo + rekaman video demo (maks. 5 menit).
- Diagram arsitektur dan seluruh isi laporan (dibagi saat laporan dikerjakan; lihat §9).
- Persiapan presentasi: **keduanya harus menguasai seluruh kode**.

### Urutan Kerja yang Disarankan (1 minggu)

| Hari | Fokus |
|------|-------|
| 1 | Setup repo, `.gitignore`, `requirements.txt`, sepakati `.proto` final (bersama) |
| 2–3 | A1: booking + servicer. A2: client CLI + wrapper retry. Integrasi unary |
| 4 | A1: streaming + uji race. A2: dedup + simulate-slow + uji dedup |
| 5 | A2: load test + Docker. A1: perbaikan bug dari hasil uji beban |
| 6 | Uji di 2 mesin/container, rekam demo, rapikan README |
| 7 | Cadangan waktu, finalisasi, latihan presentasi |

## 7. Aturan Kolaborasi Git

- Branch utama `main` selalu dalam kondisi berjalan. Tidak ada push langsung ke `main` untuk perubahan besar; gunakan branch `fitur/<nama>` + Pull Request.
- **Setiap anggota wajib memiliki commit nyata** di repo (syarat ketentuan no. 6). Jangan menyatukan pekerjaan orang lain dalam satu commit atas nama satu orang.
- Format pesan commit: `tipe: ringkasan` (`feat`, `fix`, `test`, `docs`, `chore`, `perf`). Contoh: `feat(server): pesan kursi atomik dengan lock per jadwal`.
- Perubahan `bioskop.proto` memerlukan persetujuan kedua anggota; kompatibilitas mundur dijaga (jangan ubah/hapus nomor field yang sudah ada).
- Commit kecil dan sering; jangan commit file hasil generate, `venv/`, atau `__pycache__/`.

## 8. Standar Kode dan Definition of Done

- Python 3.10+, mengikuti PEP 8, memakai type hint pada fungsi publik, komentar singkat berbahasa Indonesia untuk bagian non-trivial.
- Tidak ada state global yang diubah tanpa lock.
- Tidak ada `time.sleep` di jalur normal server selain di mode simulasi.
- Satu fitur dianggap **selesai** jika: berjalan end-to-end, ada tes, tidak merusak tes lain, README diperbarui bila perintah berubah, dan sudah di-review anggota lain.

Pengujian minimum yang harus lulus sebelum demo:

1. 50 thread memesan kursi yang sama → tepat 1 sukses, 49 gagal, tidak ada kursi ganda.
2. Pesan multi-kursi dengan salah satu kursi sudah terpesan → tidak ada kursi yang berubah (atomik).
3. Retry dengan `request_id` sama setelah balasan hilang → pesanan hanya tercipta sekali.
4. Dua client memantau `StreamStatusKursi`, satu memesan → keduanya menerima event; client putus tidak membuat server error.
5. Server dimatikan saat client berjalan → client menampilkan error terkontrol (bukan crash traceback).

## 9. Uji Kinerja (Ketentuan No. 5)

- Skenario: N client (1, 5, 20) masing-masing memanggil `GetDenah` dan `PesanKursi` pada kursi berbeda, jumlah permintaan tetap per percobaan (misal 200), ulangi ≥ 3 kali dan ambil rata-rata.
- Catat: waktu total, **throughput (req/s)**, **latensi rata-rata (ms)** (opsional p95).
- Simpan ke `bench/results/*.csv`, buat grafik, dan jelaskan **penyebab** hasil (mis. kontensi lock, jumlah `max_workers`, GIL), karena analisis penyebab dinilai.
- Jalankan juga di dua mesin/container agar latensi jaringan ikut terukur.

## 10. Laporan (DITUNDA — Jangan Dikerjakan Sekarang)

Laporan proyek akan dikerjakan setelah implementasi selesai. Simpan bahan mentah di `docs/` selama pengerjaan (diagram, tangkapan layar, hasil benchmark). Isi laporan yang nanti harus ada:

1. Deskripsi studi kasus dan diagram arsitektur (komponen dan alur pemanggilan RPC)
2. Isi file `.proto` beserta penjelasan setiap RPC
3. Strategi concurrency dan toleransi kegagalan
4. Hasil pengujian kinerja dan analisisnya
5. Pembagian tugas anggota (gunakan §6 sebagai dasar)
6. Tautan repositori Git dan video demo (maks. 5 menit)

Format laporan: Times New Roman; judul/subjudul ukuran 14 atau 16; isi seluruhnya ukuran 12; spasi 1,5; identitas (nama, NPM/NIM, kelas, mata kuliah) berupa placeholder dahulu. Pengumpulan: `Kelompok<no>_ProyekRPC.zip` berisi laporan PDF dan kode sumber. Penggunaan referensi atau alat bantu (termasuk AI) wajib dicantumkan di laporan.

## 11. Instruksi untuk AI Agent

- Patuhi struktur di §4 dan kontrak di §3.2; jika perlu menyimpang, tulis alasannya dan minta persetujuan.
- Jangan membuat UUID baru di dalam loop retry; jangan menghapus `with lock` untuk "menyederhanakan" kode.
- Jangan menambah dependensi di luar `grpcio`, `grpcio-tools`, `pytest` (dan pustaka standar) tanpa konfirmasi.
- Jangan mengerjakan laporan (§10) kecuali diminta secara eksplisit.
- Saat mengerjakan tugas, sebutkan apakah itu termasuk bagian Anggota 1 atau Anggota 2 supaya pembagian tetap seimbang, dan jangan menumpuk tugas pada satu anggota.
- Setelah mengubah `.proto`, jalankan ulang generate stub dan seluruh tes.
- Sertakan penjelasan singkat untuk setiap pilihan desain penting agar dapat dijadikan bahan laporan dan presentasi.

## 12. Identitas Kelompok (Isi Nanti)

| Peran | Nama | NPM/NIM | Kelas |
|-------|------|---------|-------|
| Anggota 1 | Muhammad Kibar Ramadhan | 247006111191 | G |
| Anggota 2 | Mochamad Dzikra Rahayu | 247006111201 | G |

- Tautan repositori: https://github.com/Barbar-code/ReservasiTiketBioskop-gRPC
- Dosen pengampu: Ir. Randi Rizal, Ph.D.
