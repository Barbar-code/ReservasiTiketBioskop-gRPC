"""client/main.py — CLI reservasi tiket bioskop (subcommand + menu interaktif).

Pemilik tugas: Anggota 2 ("Client, Ketahanan, Kinerja & Deployment").

Pemakaian:
    python -m client.main                       # menu interaktif
    python -m client.main jadwal
    python -m client.main denah --jadwal J1
    python -m client.main pesan --jadwal J1 --nama Dzikra --kursi A1,B2
    python -m client.main batal --pesanan PESAN-1
    python -m client.main stream --jadwal J1 [--max-detik 10]

Ketahanan (timeout/retry/backoff) ada di client/resilient.py. Untuk operasi
non-idempoten, request_id (UUID) dibuat SEKALI di modul ini per operasi
logis dan dipakai ulang pada setiap retry — server melakukan deduplikasi
(at-most-once). ``--request-id`` tersedia untuk mendemokan retry dengan
request_id yang sama secara manual.

Host default mengambil env ``BIOSKOP_HOST`` (dipakai di docker-compose);
overlay dengan ``--host``.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from typing import Any

import grpc

from client.resilient import DEFAULT_TIMEOUT_S, RpcGagal, panggil_rpc, pesan_ramah

try:
    from proto import bioskop_pb2 as pb2
    from proto import bioskop_pb2_grpc as pb2_grpc
except ImportError:
    sys.stderr.write(
        "Stub proto belum di-generate. Jalankan dari root repo:\n"
        "  python -m grpc_tools.protoc -I proto --python_out=. --grpc_python_out=. proto/bioskop.proto\n"
    )
    sys.exit(1)

# Tanda denah kursi (safe karakter ASCII untuk konsol Windows).
TANDA_STATUS: dict[int, str] = {pb2.TERSEDIA: ".", pb2.DIPESAN: "X"}


def buat_stub(host: str) -> tuple[grpc.Channel, Any]:
    """Bikin channel + stub; koneksi dibuat lazily saat panggilan pertama."""
    channel = grpc.insecure_channel(host)
    return channel, pb2_grpc.LayananBioskopStub(channel)


# ---------------------------------------------------------------------------
# Penangan operasi — dipakai mode subcommand maupun menu interaktif
# ---------------------------------------------------------------------------

def tampilkan_jadwal(stub: Any, timeout: float) -> None:
    """Unary ListJadwal (idempoten, boleh bebas di-retry)."""
    resp = panggil_rpc(stub.ListJadwal, pb2.ListJadwalRequest(), timeout=timeout)
    if not resp.jadwal:
        print("Belum ada jadwal tayang.")
        return
    print(f"{'ID':<8} {'FILM':<32} {'JAM':<10} DENAH")
    for j in resp.jadwal:
        print(f"{j.jadwal_id:<8} {j.film:<32} {j.jam:<10} {j.baris}x{j.kolom}")


def tampilkan_denah(stub: Any, jadwal_id: str, timeout: float) -> None:
    """Unary GetDenah: render grid kursi per baris."""
    resp = panggil_rpc(
        stub.GetDenah, pb2.DenahRequest(jadwal_id=jadwal_id), timeout=timeout
    )
    # Huruf baris -> [(nomor kolom, status), ...]
    per_baris: dict[str, list[tuple[int, int]]] = {}
    for k in resp.kursi:
        huruf, nomor = k.kode[:1], int(k.kode[1:])
        per_baris.setdefault(huruf, []).append((nomor, k.status))

    jumlah_kolom = max((max(nomor for nomor, _ in daftar) for daftar in per_baris.values()), default=0)
    print(f"Denah {resp.jadwal_id}:")
    print("     " + "".join(f"{kolom:>4}" for kolom in range(1, jumlah_kolom + 1)))
    for huruf in sorted(per_baris):
        daftar = sorted(per_baris[huruf])
        baris = "".join(f"{TANDA_STATUS.get(status, '?'):>4}" for _nomor, status in daftar)
        print(f" {huruf} | {baris}")
    print("Keterangan: '.' = tersedia, 'X' = dipesan")


def pesan_kursi(
    stub: Any,
    jadwal_id: str,
    nama: str,
    kode_kursi: list[str],
    timeout: float,
    request_id: str | None = None,
) -> None:
    """Unary PesanKursi (NON-idempoten) — request_id dibuat SEKALI di sini."""
    rid = request_id or str(uuid.uuid4())
    req = pb2.PesanKursiRequest(
        request_id=rid,
        jadwal_id=jadwal_id,
        pemesan=nama,
        kode_kursi=kode_kursi,  # semua-atau-tidak-sama-sekali (server yang menjamin)
    )
    resp = panggil_rpc(stub.PesanKursi, req, timeout=timeout)
    if resp.sukses:
        print(f"Berhasil: pesanan_id={resp.pesanan_id} kursi={','.join(kode_kursi)}")
    else:
        gagal = ",".join(resp.kursi_gagal) or "-"
        print(f"Gagal: {resp.pesan} (kursi gagal: {gagal})")
    # request_id ditampilkan agar bisa dipakai ulang di --request-id untuk
    # demo "balasan hilang lalu retry" → pesanan tidak terbudakan (dedup).
    print(f"request_id={rid}")


def batal_pesanan(
    stub: Any, pesanan_id: str, timeout: float, request_id: str | None = None
) -> None:
    """Unary BatalkanPesanan (NON-idempoten) — request_id dibuat SEKALI di sini."""
    rid = request_id or str(uuid.uuid4())
    resp = panggil_rpc(
        stub.BatalkanPesanan,
        pb2.BatalRequest(request_id=rid, pesanan_id=pesanan_id),
        timeout=timeout,
    )
    print(("Berhasil" if resp.sukses else "Gagal") + f": {resp.pesan}")
    print(f"request_id={rid}")


def pantau_stream(stub: Any, jadwal_id: str, max_detik: float = 0.0) -> None:
    """Server streaming StreamStatusKursi.

    Catatan desain: batas ``--max-detik`` ditegakkan lewat deadline level
    stream (``timeout=``), bukan per-``next`` — iterator gRPC tidak
    menyediakan next dengan timeout. ``DEADLINE_EXCEEDED`` padanya
    dianggap berhenti normal. Bila tanpa ``--max-detik``, tidak ada timeout
    (aliran panjang); berhenti dengan Ctrl+C. Cleanup subscriber di server
    ditangani via context.add_callback (Anggota 1).
    """
    req = pb2.DenahRequest(jadwal_id=jadwal_id)
    mulai = time.monotonic()
    print(f"Memantau {jadwal_id}... (Ctrl+C untuk berhenti)")
    try:
        aliran = stub.StreamStatusKursi(req, timeout=max_detik or None)
        for perubahan in aliran:
            status = "DIPESAN" if perubahan.status == pb2.DIPESAN else "TERSEDIA"
            waktu = perubahan.waktu or time.strftime("%H:%M:%S")
            print(f"[{waktu}] {perubahan.kode} -> {status}")
    except KeyboardInterrupt:
        pass
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
            print(f"Berhenti setelah {time.monotonic() - mulai:.1f} detik.")
        else:
            # Server mati/koneksi putus: pesan terkontrol, bukan traceback.
            print(f"!! Error: {pesan_ramah(e.code(), e.details() or '')}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Menu interaktif
# ---------------------------------------------------------------------------

MENU = (
    "\n=== Reservasi Tiket Bioskop (gRPC) ===\n"
    "1. Lihat jadwal tayang\n"
    "2. Lihat denah kursi\n"
    "3. Pesan kursi\n"
    "4. Batalkan pesanan\n"
    "5. Pantau status kursi (streaming)\n"
    "0. Keluar\n"
)


def tanya(tany: Any, prompt: str) -> str:
    """input() dengan penanganan EOF/Ctrl+C terkontrol."""
    try:
        return tany(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        raise KeyboardInterrupt from None


def menu_interaktif(stub: Any, timeout: float) -> None:
    while True:
        try:
            pilihan = tanya(input, MENU + "Pilihan: ")
        except KeyboardInterrupt:
            print("\nSampai jumpa!")
            return
        if pilihan == "0":
            print("Sampai jumpa!")
            return
        if pilihan == "":
            continue
        try:
            if pilihan == "1":
                tampilkan_jadwal(stub, timeout)
            elif pilihan == "2":
                tampilkan_denah(stub, tanya(input, "Jadwal ID (mis. J1): ") or "J1", timeout)
            elif pilihan == "3":
                jadwal_id = tanya(input, "Jadwal ID (mis. J1): ") or "J1"
                nama = tanya(input, "Nama pemesan: ")
                kursi = [
                    k.strip().upper()
                    for k in tanya(input, "Kursi (pisahkan koma, mis. A1,B2): ").split(",")
                    if k.strip()
                ]
                if not nama or not kursi:
                    print("Nama dan daftar kursi tidak boleh kosong.")
                    continue
                pesan_kursi(stub, jadwal_id, nama, kursi, timeout)
            elif pilihan == "4":
                pesanan_id = tanya(input, "ID pesanan yang akan dibatalkan: ")
                if pesanan_id:
                    batal_pesanan(stub, pesanan_id, timeout)
            elif pilihan == "5":
                pantau_stream(stub, tanya(input, "Jadwal ID (mis. J1): ") or "J1")
            else:
                print("Pilihan tidak dikenal.")
        except RpcGagal as e:
            print(f"!! Error: {e.pesan}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="client.main", description="CLI Reservasi Tiket Bioskop (gRPC)"
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("BIOSKOP_HOST", "localhost:50051"),
        help="alamat server (default env BIOSKOP_HOST atau localhost:50051)",
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="deadline per panggilan (detik)"
    )
    sub = parser.add_subparsers(dest="perintah")

    sub.add_parser("jadwal", help="Daftar jadwal tayang")

    p = sub.add_parser("denah", help="Denah kursi satu jadwal")
    p.add_argument("--jadwal", required=True)

    p = sub.add_parser("pesan", help="Pesan kursi (non-idempoten, dilindungi request_id)")
    p.add_argument("--jadwal", required=True)
    p.add_argument("--nama", required=True, help="nama pemesan")
    p.add_argument("--kursi", required=True, help="kode kursi dipisah koma, mis. A1,B2")
    p.add_argument("--request-id", help="pack request_id lama (untuk demo retry/dedup)")

    p = sub.add_parser("batal", help="Batalkan pesanan (non-idempoten)")
    p.add_argument("--pesanan", required=True, help="ID pesanan")
    p.add_argument("--request-id", help="request_id lama (untuk demo retry/dedup)")

    p = sub.add_parser("stream", help="Pantau perubahan status kursi (server streaming)")
    p.add_argument("--jadwal", required=True)
    p.add_argument("--max-detik", type=float, default=0.0, help="berhenti otomatis setelah N detik (0 = tanpa batas)")

    args = parser.parse_args(argv)

    channel, stub = buat_stub(args.host)
    try:
        if args.perintah is None:
            menu_interaktif(stub, args.timeout)
        elif args.perintah == "jadwal":
            tampilkan_jadwal(stub, args.timeout)
        elif args.perintah == "denah":
            tampilkan_denah(stub, args.jadwal, args.timeout)
        elif args.perintah == "pesan":
            kode_kursi = [k.strip().upper() for k in args.kursi.split(",") if k.strip()]
            if not kode_kursi:
                parser.error("--kursi tidak boleh kosong")
            pesan_kursi(stub, args.jadwal, args.nama, kode_kursi, args.timeout, args.request_id)
        elif args.perintah == "batal":
            batal_pesanan(stub, args.pesanan, args.timeout, args.request_id)
        elif args.perintah == "stream":
            pantau_stream(stub, args.jadwal, args.max_detik)
        return 0
    except RpcGagal as e:
        # Error server terkontrol — tanpa traceback (AGENTS.md §8 no. 5).
        print(f"!! Error: {e.pesan}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nDibatalkan oleh pengguna.")
        return 130
    finally:
        channel.close()


if __name__ == "__main__":
    sys.exit(main())
