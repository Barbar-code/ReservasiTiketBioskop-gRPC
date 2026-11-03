from __future__ import annotations

import json
import threading
from concurrent import futures
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import grpc
import pytest

from proto import bioskop_pb2_grpc
from server.booking import BookingManager
from server.events import SeatEventHub
from server.service import LayananBioskopServicer
from server.web import create_web_server


@pytest.fixture
def web_url():
    grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=5))
    bioskop_pb2_grpc.add_LayananBioskopServicer_to_server(
        LayananBioskopServicer(BookingManager(), SeatEventHub()),
        grpc_server,
    )
    grpc_port = grpc_server.add_insecure_port("127.0.0.1:0")
    grpc_server.start()

    web_server = create_web_server(
        f"127.0.0.1:{grpc_port}",
        host="127.0.0.1",
        port=0,
    )
    web_thread = threading.Thread(target=web_server.serve_forever, daemon=True)
    web_thread.start()
    try:
        yield f"http://127.0.0.1:{web_server.server_port}"
    finally:
        web_server.shutdown()
        web_server.server_close()
        grpc_server.stop(grace=0).wait()


def _request(url: str, method: str = "GET", payload: dict | None = None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        response = urlopen(request, timeout=3)
    except HTTPError as error:
        response = error
    with response:
        content = response.read()
        if response.headers.get_content_type() == "application/json":
            content = json.loads(content)
        return response.status, content


def test_dashboard_assets_and_booking_flow_use_grpc_service(web_url: str) -> None:
    status, page = _request(web_url)
    assert status == 200
    assert b"Layar" in page
    assert b"/favicon.svg" in page

    for asset in (
        "/favicon.svg",
        "/assets/poster-langit-senja.svg",
        "/assets/poster-petualangan-rimba.svg",
    ):
        asset_status, asset_content = _request(f"{web_url}{asset}")
        assert asset_status == 200
        assert b"<svg" in asset_content

    status, schedule_result = _request(f"{web_url}/api/jadwal")
    assert status == 200
    assert schedule_result["jadwal"][0]["jadwal_id"] == "jadwal-1"

    status, seat_result = _request(f"{web_url}/api/denah?jadwal_id=jadwal-1")
    assert status == 200
    assert len(seat_result["kursi"]) == 60

    status, booking = _request(
        f"{web_url}/api/pesan",
        "POST",
        {
            "request_id": "web-request-1",
            "jadwal_id": "jadwal-1",
            "pemesan": "Pengunjung",
            "kode_kursi": ["A1"],
        },
    )
    assert status == 200
    assert booking["sukses"]
    assert booking["pesanan_id"]

    status, conflict = _request(
        f"{web_url}/api/pesan",
        "POST",
        {
            "request_id": "web-request-2",
            "jadwal_id": "jadwal-1",
            "pemesan": "Pengunjung lain",
            "kode_kursi": ["A1"],
        },
    )
    assert status == 409
    assert conflict["kursi_gagal"] == ["A1"]

    status, cancelled = _request(
        f"{web_url}/api/batal",
        "POST",
        {
            "request_id": "web-request-3",
            "pesanan_id": booking["pesanan_id"],
        },
    )
    assert status == 200
    assert cancelled["sukses"]
