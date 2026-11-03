"""server/main.py — entrypoint server gRPC layanan bioskop.

Hasil integrasi dua sisi:
- Anggota 1: wiring servicer inti (``BookingManager`` + ``SeatEventHub``),
  validasi port/worker, logging.
- Anggota 2: argumen CLI lengkap + flag ``--simulate-slow`` via gRPC
  ``ServerInterceptor`` — servicer tidak perlu tahu mode simulasi dan jeda
  diberikan SETELAH operasi selesai sehingga benar-benar memicu perilaku
  timeout/ retry di client (AGENTS.md §3.4). ``time.sleep`` hanya dijalankan
  di jalur simulasi (diizinkan §8). Fallback bind IPv4 bila IPv6 gagal.
"""

from __future__ import annotations

import argparse
import logging
import random
import threading
import time
from concurrent import futures

import grpc

from proto import bioskop_pb2_grpc
from server.booking import BookingManager
from server.events import SeatEventHub
from server.service import LayananBioskopServicer
from server.web import create_web_server


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
            return balasan  # balasan menyusul — bisa memicu deadline client

        return grpc.unary_unary_rpc_method_handler(
            unary_lambat,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


def create_server(
    port: int = 50051,
    workers: int = 10,
    simulate_slow: bool = False,
    jeda_min_s: float = 0.3,
    jeda_maks_s: float = 1.5,
) -> grpc.Server:
    """Bangun server gRPC; servicer inti dari server/service.py (Anggota 1)."""
    if not 1 <= port <= 65535:
        raise ValueError("Port harus berada dalam rentang 1-65535")
    if workers < 1:
        raise ValueError("Jumlah worker minimal 1")

    interceptors = []
    if simulate_slow:
        interceptors.append(InterceptorSimulasiLambat(jeda_min_s, jeda_maks_s))

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=workers),
        interceptors=interceptors,
    )
    bioskop_pb2_grpc.add_LayananBioskopServicer_to_server(
        LayananBioskopServicer(BookingManager(), SeatEventHub()),
        server,
    )

    # Bind IPv6 (umum tersedia di grpcio); fallback IPv4 bila gagal.
    terikat = server.add_insecure_port(f"[::]:{port}")
    if terikat == 0:
        terikat = server.add_insecure_port(f"0.0.0.0:{port}")
    if terikat == 0:
        raise OSError(f"Port {port} tidak dapat dipakai (terpakai/akses ditolak)")
    return server


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="server.main", description="Server gRPC reservasi tiket bioskop"
    )
    parser.add_argument("--port", type=int, default=50051, help="port yang didengarkan")
    parser.add_argument("--web-host", default="0.0.0.0", help="alamat dashboard web")
    parser.add_argument("--web-port", type=int, default=8080, help="port dashboard web")
    parser.add_argument("--workers", type=int, default=10, help="besar ThreadPoolExecutor")
    parser.add_argument(
        "--simulate-slow",
        action="store_true",
        help="beri jeda balasan acak demi mendemokan retry client + dedup server",
    )
    parser.add_argument("--jeda-min", type=float, default=0.3, help="jeda minimum simulasi (detik)")
    parser.add_argument("--jeda-maks", type=float, default=1.5, help="jeda maksimum simulasi (detik)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    server = create_server(
        args.port,
        args.workers,
        simulate_slow=args.simulate_slow,
        jeda_min_s=args.jeda_min,
        jeda_maks_s=args.jeda_maks,
    )
    web_server = create_web_server(f"127.0.0.1:{args.port}", args.web_host, args.web_port)
    thread_web = threading.Thread(target=web_server.serve_forever, daemon=True)
    mode = " [simulate-slow]" if args.simulate_slow else ""
    try:
        server.start()
        thread_web.start()
        logging.info("Server gRPC berjalan di port %s%s.", args.port, mode)
        logging.info("Dashboard web tersedia di http://%s:%s.", args.web_host, web_server.server_port)
        server.wait_for_termination()
    except KeyboardInterrupt:
        logging.info("Menghentikan server")
    finally:
        if thread_web.is_alive():
            web_server.shutdown()
        web_server.server_close()
        server.stop(grace=5).wait()


if __name__ == "__main__":
    main()
