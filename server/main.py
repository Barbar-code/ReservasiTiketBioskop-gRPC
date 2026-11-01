from __future__ import annotations

import argparse
import logging
from concurrent import futures

import grpc

from proto import bioskop_pb2_grpc
from server.booking import BookingManager
from server.events import SeatEventHub
from server.service import LayananBioskopServicer


def create_server(port: int = 50051, workers: int = 10) -> grpc.Server:
    if not 1 <= port <= 65535:
        raise ValueError("Port harus berada dalam rentang 1-65535")
    if workers < 1:
        raise ValueError("Jumlah worker minimal 1")

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=workers))
    bioskop_pb2_grpc.add_LayananBioskopServicer_to_server(
        LayananBioskopServicer(BookingManager(), SeatEventHub()),
        server,
    )
    if server.add_insecure_port(f"[::]:{port}") == 0:
        raise OSError(f"Gagal membuka port {port}")
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Server reservasi tiket bioskop")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    server = create_server(args.port, args.workers)
    server.start()
    logging.info("Server gRPC berjalan di port %s", args.port)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logging.info("Menghentikan server")
        server.stop(grace=5).wait()


if __name__ == "__main__":
    main()