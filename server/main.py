"""server/main.py — entrypoint server gRPC layanan bioskop.

Pemilik tugas: Anggota 2 — argumen CLI (--port, --workers) dan flag
--simulate-slow sebagaimana AGENTS.md §6/§3.4. Seluruh logika layanan
berada di server/service.py, server/booking.py, server/events.py
(milik Anggota 1); file ini hanya membangun server dan menempelkan servicer.

Pilihan desain --simulate-slow: diimplementasi lewat gRPC ServerInterceptor,
bukan di dalam handler — servicer tidak perlu tahu mode simulasi (kontrak
dengan Anggota 1: ``BioskopServicer()`` tanpa argumen) dan jeda diberikan
SETELAH operasi selesai sehingga benar-benar memicu perilaku timeout/ retry
di client (AGENTS.md §3.4). ``time.sleep`` hanya dijalankan di jalur simulasi
ini (diizinkan §8).
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from concurrent import futures

import grpc

try:
    import bioskop_pb2_grpc as pb2_grpc
except ImportError:
    sys.stderr.write(
        "Stub proto belum di-generate. Jalankan dari root repo:\n"
        "  python -m grpc_tools.protoc -I proto --python_out=. --grpc_python_out=. proto/bioskop.proto\n"
    )
    sys.exit(1)


class InterceptorSimulasiLambat(grpc.ServerInterceptor):
    """Tunda balasan RPC unary secara acak SETELAH operasi selesai.

    Hanya aktif bila server dijalankan dengan --simulate-slow; tujuannya
    mendemokan balasan lambat → client timeout → retry dengan request_id
    sama → server menjawab dari DedupStore tanpa eksekusi ulang.
    Streaming tidak diberi jeda agar demo streaming tetap responsif.
    """

    def __init__(self, jeda_min_s: float, jeda_maks_s: float) -> None:
        if jeda_min_s < 0 or jeda_maks_s < jeda_min_s:
            raise ValueError("jeda harus memenuhi 0 <= min <= maks")
        self._min = jeda_min_s
        self._maks = jeda_maks_s

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None or handler.response_streaming or handler.unary_unary is None:
            return handler
        handler_asli = handler.unary_unary

        def unary_lambat(request, context):
            balasan = handler_asli(request, context)  # operasi dikerjakan dulu
            # Sleep hanya di jalur simulasi (AGENTS.md §8).
            time.sleep(random.uniform(self._min, self._maks))
            return balasan  # balasan menyusul — bisa memicu timeout client

        return grpc.unary_unary_rpc_method_handler(
            unary_lambat,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


def buat_server(
    port: int,
    workers: int,
    simulasi_lambat: bool,
    jeda_min_s: float,
    jeda_maks_s: float,
) -> grpc.Server:
    """Bangun server gRPC; servicer inti dari server/service.py (Anggota 1)."""
    try:
        from server.service import BioskopServicer
    except ImportError as e:
        sys.stderr.write(
            "server/service.py (tugas Anggota 1) belum berisi BioskopServicer, "
            "atau dependensinya belum generate.\n"
        )
        raise SystemExit(1) from e

    interceptors = []
    if simulasi_lambat:
        interceptors.append(InterceptorSimulasiLambat(jeda_min_s, jeda_maks_s))

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=workers),
        interceptors=interceptors,
    )
    pb2_grpc.add_LayananBioskopServicer_to_server(BioskopServicer(), server)

    # Bind IPv6 (umum tersedia di grpcio); fallback IPv4 bila gagal.
    terikat = server.add_insecure_port(f"[::]:{port}")
    if terikat == 0:
        terikat = server.add_insecure_port(f"0.0.0.0:{port}")
    if terikat == 0:
        raise RuntimeError(f"Port {port} tidak dapat dipakai (terpakai/akses ditolak)")
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="server.main", description="Server gRPC reservasi tiket bioskop"
    )
    parser.add_argument("--port", type=int, default=50051, help="port yang didengarkan")
    parser.add_argument("--workers", type=int, default=10, help="besar ThreadPoolExecutor")
    parser.add_argument(
        "--simulate-slow",
        action="store_true",
        help="beri jeda balasan acak demi mendemokan retry client + dedup server",
    )
    parser.add_argument("--jeda-min", type=float, default=0.3, help="jeda minimum simulasi (detik)")
    parser.add_argument("--jeda-maks", type=float, default=1.5, help="jeda maksimum simulasi (detik)")
    args = parser.parse_args(argv)

    server = buat_server(args.port, args.workers, args.simulate_slow, args.jeda_min, args.jeda_maks)
    server.start()
    mode = " [simulate-slow]" if args.simulate_slow else ""
    print(f"Server gRPC aktif di port {args.port}{mode} — Ctrl+C untuk berhenti.")
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        pass
    finally:
        print("Server berhenti.")
        server.stop(grace=1.0).wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
