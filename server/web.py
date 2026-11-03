from __future__ import annotations

import json
import logging
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import grpc

from client.resilient import RpcGagal, panggil_rpc
from proto import bioskop_pb2, bioskop_pb2_grpc

_log = logging.getLogger(__name__)
_WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/assets/poster-langit-senja.svg": ("assets/poster-langit-senja.svg", "image/svg+xml"),
    "/assets/poster-petualangan-rimba.svg": (
        "assets/poster-petualangan-rimba.svg",
        "image/svg+xml",
    ),
}
_MAX_BODY_BYTES = 16_384


class PayloadTooLargeError(ValueError):
    pass


def _error_status(code: grpc.StatusCode | None) -> int:
    return {
        grpc.StatusCode.INVALID_ARGUMENT: 400,
        grpc.StatusCode.NOT_FOUND: 404,
        grpc.StatusCode.ALREADY_EXISTS: 409,
        grpc.StatusCode.FAILED_PRECONDITION: 409,
        grpc.StatusCode.UNAVAILABLE: 503,
        grpc.StatusCode.DEADLINE_EXCEEDED: 504,
    }.get(code, 502)


class BioskopHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], grpc_target: str) -> None:
        super().__init__(address, BioskopRequestHandler)
        self.channel = grpc.insecure_channel(grpc_target)
        self.stub = bioskop_pb2_grpc.LayananBioskopStub(self.channel)

    def server_close(self) -> None:
        super().server_close()
        self.channel.close()


class BioskopRequestHandler(BaseHTTPRequestHandler):
    server: BioskopHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in _STATIC_FILES:
            self._serve_static(*_STATIC_FILES[parsed.path])
        elif parsed.path == "/api/jadwal":
            self._list_showtimes()
        elif parsed.path == "/api/denah":
            self._get_seats(parse_qs(parsed.query).get("jadwal_id", [""])[0])
        elif parsed.path == "/api/events":
            self._stream_events(parse_qs(parsed.query).get("jadwal_id", [""])[0])
        else:
            self._send_json(404, {"pesan": "Halaman atau endpoint tidak ditemukan."})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/pesan":
                self._book_seats(payload)
            elif parsed.path == "/api/batal":
                self._cancel_booking(payload)
            else:
                self._send_json(404, {"pesan": "Endpoint tidak ditemukan."})
        except PayloadTooLargeError as exc:
            self._send_json(413, {"pesan": str(exc)})
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"pesan": "Isi permintaan harus berupa JSON yang valid."})
        except ValueError as exc:
            self._send_json(400, {"pesan": str(exc)})

    def _serve_static(self, filename: str, content_type: str) -> None:
        path = _WEB_ROOT / filename
        try:
            content = path.read_bytes()
        except OSError:
            _log.exception("Tidak dapat membaca aset web %s", path)
            self._send_json(500, {"pesan": "Aset web tidak dapat dimuat."})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def _read_json(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Panjang permintaan tidak valid.") from exc
        if size > _MAX_BODY_BYTES:
            self.close_connection = True
            raise PayloadTooLargeError("Ukuran permintaan maksimal 16 KB.")
        if size <= 0:
            raise ValueError("Ukuran permintaan harus antara 1 dan 16 KB.")
        payload = json.loads(self.rfile.read(size).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Isi permintaan harus berupa objek JSON.")
        return payload

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _rpc_error(self, error: RpcGagal) -> None:
        self._send_json(
            _error_status(error.kode),
            {"pesan": error.detail or error.pesan, "kode": error.kode.name if error.kode else ""},
        )

    def _list_showtimes(self) -> None:
        try:
            response = panggil_rpc(
                self.server.stub.ListJadwal,
                bioskop_pb2.ListJadwalRequest(),
            )
        except RpcGagal as exc:
            self._rpc_error(exc)
            return
        self._send_json(
            200,
            {
                "jadwal": [
                    {
                        "jadwal_id": item.jadwal_id,
                        "film": item.film,
                        "jam": item.jam,
                        "baris": item.baris,
                        "kolom": item.kolom,
                    }
                    for item in response.jadwal
                ]
            },
        )

    def _get_seats(self, showtime_id: str) -> None:
        try:
            response = panggil_rpc(
                self.server.stub.GetDenah,
                bioskop_pb2.DenahRequest(jadwal_id=showtime_id),
            )
        except RpcGagal as exc:
            self._rpc_error(exc)
            return
        self._send_json(
            200,
            {
                "jadwal_id": response.jadwal_id,
                "kursi": [
                    {
                        "kode": seat.kode,
                        "status": "dipesan" if seat.status == bioskop_pb2.DIPESAN else "tersedia",
                    }
                    for seat in response.kursi
                ],
            },
        )

    def _book_seats(self, payload: dict[str, Any]) -> None:
        showtime_id = payload.get("jadwal_id")
        name = payload.get("pemesan")
        seats = payload.get("kode_kursi")
        request_id = payload.get("request_id") or str(uuid4())
        if not isinstance(showtime_id, str) or not isinstance(name, str):
            raise ValueError("Jadwal dan nama pemesan wajib diisi.")
        if not isinstance(seats, list) or not seats or not all(isinstance(item, str) for item in seats):
            raise ValueError("Pilih minimal satu kursi yang valid.")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("ID permintaan tidak valid.")

        request = bioskop_pb2.PesanKursiRequest(
            request_id=request_id,
            jadwal_id=showtime_id,
            pemesan=name,
            kode_kursi=seats,
        )
        try:
            response = panggil_rpc(self.server.stub.PesanKursi, request)
        except RpcGagal as exc:
            self._rpc_error(exc)
            return
        self._send_json(
            200 if response.sukses else 409,
            {
                "sukses": response.sukses,
                "pesan": response.pesan,
                "pesanan_id": response.pesanan_id,
                "kursi_gagal": list(response.kursi_gagal),
                "request_id": request_id,
            },
        )

    def _cancel_booking(self, payload: dict[str, Any]) -> None:
        booking_id = payload.get("pesanan_id")
        request_id = payload.get("request_id") or str(uuid4())
        if not isinstance(booking_id, str) or not booking_id.strip():
            raise ValueError("ID pesanan wajib diisi.")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("ID permintaan tidak valid.")
        request = bioskop_pb2.BatalRequest(
            request_id=request_id,
            pesanan_id=booking_id,
        )
        try:
            response = panggil_rpc(self.server.stub.BatalkanPesanan, request)
        except RpcGagal as exc:
            self._rpc_error(exc)
            return
        self._send_json(
            200 if response.sukses else 409,
            {"sukses": response.sukses, "pesan": response.pesan, "request_id": request_id},
        )

    def _stream_events(self, showtime_id: str) -> None:
        call = self.server.stub.StreamStatusKursi(
            bioskop_pb2.DenahRequest(jadwal_id=showtime_id)
        )
        events: queue.Queue[Any] = queue.Queue()

        def receive_events() -> None:
            try:
                for event in call:
                    events.put(event)
            except grpc.RpcError as exc:
                events.put(exc)
            finally:
                events.put(None)

        threading.Thread(target=receive_events, daemon=True).start()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                try:
                    event = events.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                if event is None:
                    break
                if isinstance(event, grpc.RpcError):
                    payload = {
                        "pesan": event.details() or "Streaming status kursi terputus.",
                        "kode": event.code().name,
                    }
                    self.wfile.write(
                        f"event: error\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
                    )
                    self.wfile.flush()
                    break
                payload = json.dumps(
                    {
                        "jadwal_id": event.jadwal_id,
                        "kode": event.kode,
                        "status": "dipesan" if event.status == bioskop_pb2.DIPESAN else "tersedia",
                        "waktu": event.waktu,
                    }
                )
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            call.cancel()

    def log_message(self, format: str, *args: Any) -> None:
        _log.info("%s - %s", self.address_string(), format % args)


def create_web_server(
    grpc_target: str,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> BioskopHTTPServer:
    """Create the HTTP dashboard server and its gRPC client connection."""
    if not 0 <= port <= 65535:
        raise ValueError("Port web harus berada dalam rentang 0-65535")
    return BioskopHTTPServer((host, port), grpc_target)
