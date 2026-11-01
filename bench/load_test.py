"""bench/load_test.py — uji kinerja: latensi rata-rata & throughput.

Pemilik tugas: Anggota 2 ("Client, Ketahanan, Kinerja & Deployment"),
untuk Ketentuan Wajib no. 5 (AGENTS.md §9).

Skenario:
- N client (default 1, 5, 20) memanggil GetDenah + PesanKursi pada kursi
  BERBEDA (PesanKursi dilindungi request_id, satu kursi per operasi).
- Jumlah permintaan tetap per percobaan (default 200), diulang >= 3 kali
  lalu dirata-rata per kombinasi (run, jumlah client).
- Dicatat: durasi total, throughput (req/s), latensi rata-rata + p95 (ms).
- Hasil di tulis ke bench/results/*.csv; grafik dibuat manual
  (Excel/Sheets) dari CSV — tanpa dependensi tambahan (AGENTS.md §11).

Catatan metode (untuk laporan §9):
- Pemuat sengaja memanggil stub LANGSUNG (tanpa wrapper retry) agar yang
  terukur adalah latensi RPC murni, bukan efek jeda backoff.
- PesanKursi akan menghabiskan kursi (hanya 60 kursi per jadwal); saat
  alokasi habis, worker otomatis beralih ke GetDenah supaya jumlah request
  antar skenario tetap setara — pengaruhnya dianalisis di laporan.

Pemakaian:
    python bench/load_test.py --clients 1 5 20 --requests 200 --runs 3
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
import threading
import time
import uuid
from typing import Any

import grpc

try:
    from proto import bioskop_pb2 as pb2
    from proto import bioskop_pb2_grpc as pb2_grpc
except ImportError:
    sys.stderr.write(
        "Stub proto belum di-generate. Jalankan dari root repo:\n"
        "  python -m grpc_tools.protoc -I . --python_out=. --grpc_python_out=. proto/bioskop.proto\n"
    )
    sys.exit(1)

# Denah baris A-F x kolom 1-10 = 60 kursi per jadwal (sesuai seed server).
BARIS: tuple[str, ...] = tuple("ABCDEF")
JUMLAH_KOLOM: int = 10


def persentil(nilai: list[float], q: float) -> float:
    """Persentil q (0..1) dari daftar nilai latensi (ms); NaN bila kosong."""
    if not nilai:
        return float("nan")
    terurut = sorted(nilai)
    idx = max(0, min(len(terurut) - 1, int(round(q * (len(terurut) - 1)))))
    return terurut[idx]


class PengalokasiKursi:
    """Bagikan kode kursi unik tanpa konflik antar thread; None bila habis."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter = 0

    def ambil(self) -> str | None:
        with self._lock:
            if self._counter >= len(BARIS) * JUMLAH_KOLOM:
                return None
            idx = self._counter
            self._counter += 1
        return f"{BARIS[idx // JUMLAH_KOLOM]}{idx % JUMLAH_KOLOM + 1}"


def kerjakan(
    host: str,
    jadwal_id: str,
    kuota: int,
    mode: str,
    alok: PengalokasiKursi,
    timeout: float,
    penghalang: threading.Barrier,
) -> tuple[list[float], int, int]:
    """Satu 'client': jalankan kuota request; hasil (latensi_ms, sukses, gagal).

    mode: 'denah' (GetDenah saja), 'pesan' (PesanKursi saja), 'campur'
    (bergantian, sesuai skenario §9).
    """
    # Channel per thread — realistis seperti CLI client per proses.
    channel = grpc.insecure_channel(host)
    stub = pb2_grpc.LayananBioskopStub(channel)
    latensi: list[float] = []
    sukses = gagal = 0

    penghalang.wait()  # semua client mulai seraya agar hasil adil
    try:
        for i in range(kuota):
            t0 = time.perf_counter()
            berhasil = False
            try:
                operasi_pesan = mode == "pesan" or (mode == "campur" and i % 2 == 1)
                if operasi_pesan:
                    kode = alok.ambil()
                    if kode is None:
                        # Kursi habis → GetDenah agar jumlah request tetap
                        # setara; pengaruhnya dibedah di laporan.
                        stub.GetDenah(pb2.DenahRequest(jadwal_id=jadwal_id), timeout=timeout)
                    else:
                        req = pb2.PesanKursiRequest(
                            request_id=str(uuid.uuid4()),  # satu operasi logis = satu id
                            jadwal_id=jadwal_id,
                            pemesan=f"bench-{threading.get_ident()}",
                            kode_kursi=[kode],
                        )
                        stub.PesanKursi(req, timeout=timeout)
                else:
                    stub.GetDenah(pb2.DenahRequest(jadwal_id=jadwal_id), timeout=timeout)
                berhasil = True
            except grpc.RpcError:
                pass  # dicatat sebagai gagal (mis. UNAVAILABLE saat server mati)
            latensi.append((time.perf_counter() - t0) * 1000.0)
            if berhasil:
                sukses += 1
            else:
                gagal += 1
    finally:
        channel.close()

    return latensi, sukses, gagal


def jalankan_skenario(
    host: str,
    jadwal_id: str,
    jumlah_client: int,
    total_request: int,
    mode: str,
    timeout: float,
) -> dict[str, float]:
    """Jalankan satu percobaan; kembalikan statistik yang sudah agregat."""
    alok = PengalokasiKursi()
    penghalang = threading.Barrier(jumlah_client)
    kuota_dasar = total_request // jumlah_client
    sisa = total_request % jumlah_client  # sisa dibagi ke client pertama

    hasil: list[tuple[list[float], int, int]] = []
    kunci = threading.Lock()

    def selanjutnya(idx: int) -> None:
        kuota = kuota_dasar + (1 if idx < sisa else 0)
        res = kerjakan(host, jadwal_id, kuota, mode, alok, timeout, penghalang)
        with kunci:
            hasil.append(res)

    threads = [
        threading.Thread(target=selanjutnya, args=(idx,), daemon=True)
        for idx in range(jumlah_client)
    ]
    t0 = time.perf_counter()  # semua thread sudah dibuat; barrier menyinkronkan mulai
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    durasi_s = time.perf_counter() - t0

    latensi = [v for daftar, _s, _g in hasil for v in daftar]
    sukses = sum(s for _, s, _g in hasil)
    gagal = sum(g for _, _s, g in hasil)
    jumlah = sukses + gagal

    return {
        "durasi_s": durasi_s,
        "throughput_rps": (jumlah / durasi_s) if durasi_s > 0 else 0.0,
        "latensi_rata_ms": statistics.fmean(latensi) if latensi else 0.0,
        "latensi_p95_ms": persentil(latensi, 0.95),
        "sukses": float(sukses),
        "gagal": float(gagal),
        "jumlah": float(jumlah),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bench.load_test", description="Uji kinerja 1/5/20 client (AGENTS.md §9)"
    )
    parser.add_argument("--host", default=os.environ.get("BIOSKOP_HOST", "localhost:50051"))
    parser.add_argument("--jadwal", default="jadwal-1", help="jadwal_id target (sesuai seed server)")
    parser.add_argument("--clients", nargs="+", type=int, default=[1, 5, 20])
    parser.add_argument("--requests", type=int, default=200, help="total request per percobaan (tetap)")
    parser.add_argument("--runs", type=int, default=3, help="diulang >= 3 kali lalu dirata-rata")
    parser.add_argument(
        "--mode", choices=("campur", "denah", "pesan"), default="campur",
        help="campur = bergantian GetDenah/PesanKursi (default)",
    )
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument(
        "--out-csv", dest="out_csv", default=None,
        help="path CSV keluaran (default bench/results/kinerja_<waktu>.csv)",
    )
    args = parser.parse_args(argv)

    path_csv = args.out_csv or os.path.join(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "results"),
        f"kinerja_{time.strftime('%Y%m%d_%H%M%S')}.csv",
    )
    os.makedirs(os.path.dirname(os.path.abspath(path_csv)), exist_ok=True)

    print(
        f"Target {args.host}, jadwal={args.jadwal} | {args.requests} request x "
        f"{args.runs} percobaan x client {args.clients} | mode={args.mode}\n"
    )

    baris_csv: list[dict[str, Any]] = []
    ringkasan: dict[int, list[dict[str, float]]] = {n: [] for n in args.clients}

    for run in range(1, args.runs + 1):
        for n in args.clients:
            jejak = jalankan_skenario(
                args.host, args.jadwal, n, args.requests, args.mode, args.timeout
            )
            ringkasan[n].append(jejak)
            b = {
                "run": run,
                "clients": n,
                "total_request": int(jejak["jumlah"]),
                "durasi_s": round(jejak["durasi_s"], 4),
                "throughput_rps": round(jejak["throughput_rps"], 2),
                "latensi_rata_ms": round(jejak["latensi_rata_ms"], 3),
                "latensi_p95_ms": round(jejak["latensi_p95_ms"], 3),
                "sukses": int(jejak["sukses"]),
                "gagal": int(jejak["gagal"]),
                "operasi": args.mode,
            }
            baris_csv.append(b)
            print(
                f"run {run}/{args.runs} | {n:>2} client | "
                f"throughput {b['throughput_rps']:>8.1f} req/s | "
                f"lat mean {b['latensi_rata_ms']:>8.1f} ms | "
                f"p95 {b['latensi_p95_ms']:>8.1f} ms | "
                f"sukses {b['sukses']} gagal {b['gagal']}"
            )

    # ---- Ringkasan rata-rata antar percobaan --------------------------------
    print("\n=== Rata-rata antar percobaan ===")
    print(f"{'client':>7} {'throughput req/s':>17} {'lat mean ms':>13} {'lat p95 ms':>12}")
    for n, daftar in ringkasan.items():
        throughput = statistics.fmean(d["throughput_rps"] for d in daftar)
        rata = statistics.fmean(d["latensi_rata_ms"] for d in daftar)
        p95 = statistics.fmean(d["latensi_p95_ms"] for d in daftar)
        print(f"{n:>7} {throughput:>17.1f} {rata:>13.1f} {p95:>12.1f}")

    with open(path_csv, "w", newline="", encoding="utf-8") as f:
        penulis = csv.DictWriter(f, fieldnames=list(baris_csv[0].keys()))
        penulis.writeheader()
        penulis.writerows(baris_csv)

    print(f"\nHasil disimpan ke: {path_csv}")
    print("Grafik dibuat manual (Excel/Sheets) dari CSV ini — tanpa dependensi tambahan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
