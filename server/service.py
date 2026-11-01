from __future__ import annotations

from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Iterator, NoReturn

import grpc

from proto import bioskop_pb2, bioskop_pb2_grpc
from server.booking import (
    BookingManager,
    BookingNotFoundError,
    InvalidBookingError,
    InvalidSeatError,
    SeatStatus,
    ShowtimeNotFoundError,
)
from server.events import SeatEventHub


class LayananBioskopServicer(bioskop_pb2_grpc.LayananBioskopServicer):
    def __init__(
        self,
        booking_manager: BookingManager | None = None,
        event_hub: SeatEventHub | None = None,
    ) -> None:
        self._booking_manager = booking_manager or BookingManager()
        self._event_hub = event_hub or SeatEventHub()

    @staticmethod
    def _abort(
        context: grpc.ServicerContext,
        code: grpc.StatusCode,
        details: str,
    ) -> NoReturn:
        context.abort(code, details)
        raise RuntimeError("gRPC context.abort() returned unexpectedly")

    @staticmethod
    def _event(
        jadwal_id: str,
        kode: str,
        status: int,
    ) -> bioskop_pb2.PerubahanKursi:
        return bioskop_pb2.PerubahanKursi(
            jadwal_id=jadwal_id,
            kode=kode,
            status=status,
            waktu=datetime.now(timezone.utc).isoformat(),
        )

    def ListJadwal(
        self,
        request: bioskop_pb2.ListJadwalRequest,
        context: grpc.ServicerContext,
    ) -> bioskop_pb2.ListJadwalResponse:
        response = bioskop_pb2.ListJadwalResponse()
        for showtime in self._booking_manager.list_showtimes():
            response.jadwal.add(
                jadwal_id=showtime.jadwal_id,
                film=showtime.film,
                jam=showtime.jam,
                baris=showtime.baris,
                kolom=showtime.kolom,
            )
        return response

    def GetDenah(
        self,
        request: bioskop_pb2.DenahRequest,
        context: grpc.ServicerContext,
    ) -> bioskop_pb2.DenahResponse:
        try:
            seats = self._booking_manager.get_seats(request.jadwal_id)
        except ShowtimeNotFoundError as exc:
            self._abort(context, grpc.StatusCode.NOT_FOUND, str(exc))
        except InvalidBookingError as exc:
            self._abort(context, grpc.StatusCode.INVALID_ARGUMENT, str(exc))

        response = bioskop_pb2.DenahResponse(jadwal_id=request.jadwal_id)
        for seat in seats:
            response.kursi.add(
                kode=seat.kode,
                status=(
                    bioskop_pb2.DIPESAN
                    if seat.status is SeatStatus.BOOKED
                    else bioskop_pb2.TERSEDIA
                ),
            )
        return response

    def PesanKursi(
        self,
        request: bioskop_pb2.PesanKursiRequest,
        context: grpc.ServicerContext,
    ) -> bioskop_pb2.PesanKursiResponse:
        try:
            result = self._booking_manager.book_seats(
                request.jadwal_id,
                request.pemesan,
                request.kode_kursi,
            )
        except ShowtimeNotFoundError as exc:
            self._abort(context, grpc.StatusCode.NOT_FOUND, str(exc))
        except (InvalidBookingError, InvalidSeatError) as exc:
            self._abort(context, grpc.StatusCode.INVALID_ARGUMENT, str(exc))

        if result.sukses:
            for seat_code in result.kursi_berubah:
                self._event_hub.publish(
                    self._event(request.jadwal_id, seat_code, bioskop_pb2.DIPESAN)
                )
        return bioskop_pb2.PesanKursiResponse(
            sukses=result.sukses,
            pesan=result.pesan,
            pesanan_id=result.pesanan_id,
            kursi_gagal=result.kursi_gagal,
        )

    def BatalkanPesanan(
        self,
        request: bioskop_pb2.BatalRequest,
        context: grpc.ServicerContext,
    ) -> bioskop_pb2.BatalResponse:
        try:
            result = self._booking_manager.cancel_booking(request.pesanan_id)
        except BookingNotFoundError as exc:
            self._abort(context, grpc.StatusCode.NOT_FOUND, str(exc))
        except InvalidBookingError as exc:
            self._abort(context, grpc.StatusCode.INVALID_ARGUMENT, str(exc))

        if result.sukses:
            for seat_code in result.kursi_berubah:
                self._event_hub.publish(
                    self._event(
                        result.jadwal_id,
                        seat_code,
                        bioskop_pb2.TERSEDIA,
                    )
                )
        return bioskop_pb2.BatalResponse(sukses=result.sukses, pesan=result.pesan)

    def StreamStatusKursi(
        self,
        request: bioskop_pb2.DenahRequest,
        context: grpc.ServicerContext,
    ) -> Iterator[bioskop_pb2.PerubahanKursi]:
        subscriber: Queue[bioskop_pb2.PerubahanKursi] = self._event_hub.subscribe(
            request.jadwal_id
        )
        try:
            self._booking_manager.get_seats(request.jadwal_id)
        except ShowtimeNotFoundError as exc:
            self._event_hub.unsubscribe(request.jadwal_id, subscriber)
            self._abort(context, grpc.StatusCode.NOT_FOUND, str(exc))
        except InvalidBookingError as exc:
            self._event_hub.unsubscribe(request.jadwal_id, subscriber)
            self._abort(context, grpc.StatusCode.INVALID_ARGUMENT, str(exc))

        try:
            context.add_callback(
                lambda: self._event_hub.unsubscribe(request.jadwal_id, subscriber)
            )
            while context.is_active():
                try:
                    yield subscriber.get(timeout=0.5)
                except Empty:
                    continue
        finally:
            self._event_hub.unsubscribe(request.jadwal_id, subscriber)