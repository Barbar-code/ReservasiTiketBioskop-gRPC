from __future__ import annotations

import grpc
import pytest

from proto import bioskop_pb2
from server.booking import BookingManager, ShowtimeInfo
from server.events import SeatEventHub
from server.service import LayananBioskopServicer


class _Abort(Exception):
    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        self.code = code
        self.details = details


class _Context:
    def abort(self, code: grpc.StatusCode, details: str) -> None:
        raise _Abort(code, details)


@pytest.fixture
def service_and_hub() -> tuple[LayananBioskopServicer, SeatEventHub]:
    hub = SeatEventHub()
    manager = BookingManager(
        [ShowtimeInfo("show-1", "Film Uji", "19:00", 2, 3)]
    )
    return LayananBioskopServicer(manager, hub), hub


def test_unary_rpcs_list_showtime_seats_book_and_cancel(
    service_and_hub: tuple[LayananBioskopServicer, SeatEventHub],
) -> None:
    service, hub = service_and_hub
    context = _Context()
    subscriber = hub.subscribe("show-1")

    showtimes = service.ListJadwal(bioskop_pb2.ListJadwalRequest(), context)
    layout = service.GetDenah(bioskop_pb2.DenahRequest(jadwal_id="show-1"), context)
    booking = service.PesanKursi(
        bioskop_pb2.PesanKursiRequest(
            request_id="request-1",
            jadwal_id="show-1",
            pemesan="Ayu",
            kode_kursi=["A1"],
        ),
        context,
    )

    assert showtimes.jadwal[0].jadwal_id == "show-1"
    assert len(layout.kursi) == 6
    assert booking.sukses
    assert subscriber.get_nowait().status == bioskop_pb2.DIPESAN

    conflict = service.PesanKursi(
        bioskop_pb2.PesanKursiRequest(
            request_id="request-2",
            jadwal_id="show-1",
            pemesan="Bima",
            kode_kursi=["A1", "A2"],
        ),
        context,
    )
    assert not conflict.sukses
    assert list(conflict.kursi_gagal) == ["A1"]

    cancelled = service.BatalkanPesanan(
        bioskop_pb2.BatalRequest(
            request_id="request-3",
            pesanan_id=booking.pesanan_id,
        ),
        context,
    )
    assert cancelled.sukses
    assert subscriber.get_nowait().status == bioskop_pb2.TERSEDIA
    hub.unsubscribe("show-1", subscriber)


@pytest.mark.parametrize(
    ("rpc_name", "rpc_request", "expected_code"),
    [
        (
            "GetDenah",
            bioskop_pb2.DenahRequest(jadwal_id="missing"),
            grpc.StatusCode.NOT_FOUND,
        ),
        (
            "GetDenah",
            bioskop_pb2.DenahRequest(),
            grpc.StatusCode.INVALID_ARGUMENT,
        ),
        (
            "PesanKursi",
            bioskop_pb2.PesanKursiRequest(
                jadwal_id="show-1",
                pemesan="Ayu",
            ),
            grpc.StatusCode.INVALID_ARGUMENT,
        ),
    ],
)
def test_invalid_rpc_requests_use_grpc_status_codes(
    service_and_hub: tuple[LayananBioskopServicer, SeatEventHub],
    rpc_name: str,
    rpc_request: object,
    expected_code: grpc.StatusCode,
) -> None:
    service, _ = service_and_hub

    with pytest.raises(_Abort) as error:
        getattr(service, rpc_name)(rpc_request, _Context())

    assert error.value.code == expected_code
