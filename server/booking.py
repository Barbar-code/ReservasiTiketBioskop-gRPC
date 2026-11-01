from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Iterable, Sequence
from uuid import uuid4


class SeatStatus(Enum):
    AVAILABLE = "available"
    BOOKED = "booked"


@dataclass(frozen=True)
class ShowtimeInfo:
    jadwal_id: str
    film: str
    jam: str
    baris: int
    kolom: int


@dataclass(frozen=True)
class SeatInfo:
    kode: str
    status: SeatStatus


@dataclass(frozen=True)
class BookingResult:
    sukses: bool
    pesan: str
    pesanan_id: str = ""
    kursi_gagal: tuple[str, ...] = ()
    kursi_berubah: tuple[str, ...] = ()


@dataclass(frozen=True)
class CancellationResult:
    sukses: bool
    pesan: str
    jadwal_id: str = ""
    kursi_berubah: tuple[str, ...] = ()


class BookingError(ValueError):
    pass

class InvalidBookingError(BookingError):
    pass

class InvalidSeatError(BookingError):
    def __init__(self, seat_codes: Sequence[str]) -> None:
        self.seat_codes = tuple(seat_codes)
        super().__init__(f"Kode kursi tidak valid: {', '.join(self.seat_codes)}")


class ShowtimeNotFoundError(LookupError):
    pass

class BookingNotFoundError(LookupError):
    pass

@dataclass
class _ShowtimeState:
    info: ShowtimeInfo
    booked_seats: set[str]
    bookings: dict[str, tuple[str, ...]] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)


class BookingManager:
    def __init__(self, showtimes: Iterable[ShowtimeInfo] | None = None) -> None:
        if showtimes is None:
            showtimes = (
                ShowtimeInfo("jadwal-1", "Langit Senja", "13:00", 6, 10),
                ShowtimeInfo("jadwal-2", "Petualangan Rimba", "16:00", 6, 10),
            )

        self._showtimes: dict[str, _ShowtimeState] = {}

        for info in showtimes:
            self._validate_showtime(info)
            if info.jadwal_id in self._showtimes:
                raise ValueError(f"ID jadwal duplikat: {info.jadwal_id}")
            self._showtimes[info.jadwal_id] = _ShowtimeState(
                info=info,
                booked_seats=set(),
            )
        if not self._showtimes:
            raise ValueError("Minimal satu jadwal harus tersedia")

    @staticmethod
    def _validate_showtime(info: ShowtimeInfo) -> None:
        if not info.jadwal_id.strip() or not info.film.strip() or not info.jam.strip():
            raise ValueError("ID jadwal, nama film, dan jam tayang wajib diisi")
        if not 1 <= info.baris <= 26 or info.kolom < 1:
            raise ValueError("Jumlah baris harus 1-26 dan jumlah kolom minimal 1")

    @staticmethod
    def _seat_codes(info: ShowtimeInfo) -> tuple[str, ...]:
        return tuple(
            f"{chr(ord('A') + row)}{column}"
            for row in range(info.baris)
            for column in range(1, info.kolom + 1)
        )

    def _get_showtime(self, jadwal_id: str) -> _ShowtimeState:
        if not jadwal_id.strip():
            raise InvalidBookingError("ID jadwal wajib diisi")
        try:
            return self._showtimes[jadwal_id]
        except KeyError as exc:
            raise ShowtimeNotFoundError(f"Jadwal tidak ditemukan: {jadwal_id}") from exc

    def list_showtimes(self) -> tuple[ShowtimeInfo, ...]:
        return tuple(state.info for state in self._showtimes.values())

    def get_seats(self, jadwal_id: str) -> tuple[SeatInfo, ...]:
        state = self._get_showtime(jadwal_id)
        with state.lock:
            return tuple(
                SeatInfo(
                    kode=kode,
                    status=(
                        SeatStatus.BOOKED
                        if kode in state.booked_seats
                        else SeatStatus.AVAILABLE
                    ),
                )
                for kode in self._seat_codes(state.info)
            )

    @staticmethod
    def _normalize_seat_codes(seat_codes: Sequence[str]) -> tuple[str, ...]:
        if not seat_codes:
            raise InvalidBookingError("Minimal satu kursi harus dipilih")
        normalized: list[str] = []
        for seat_code in seat_codes:
            if not isinstance(seat_code, str) or not seat_code.strip():
                raise InvalidBookingError("Setiap kode kursi wajib berupa teks")
            normalized.append(seat_code.strip().upper())
        if len(set(normalized)) != len(normalized):
            raise InvalidBookingError("Kode kursi yang sama tidak boleh diulang")
        return tuple(normalized)

    def book_seats(
        self,
        jadwal_id: str,
        pemesan: str,
        seat_codes: Sequence[str],
    ) -> BookingResult:
        if not jadwal_id.strip():
            raise InvalidBookingError("ID jadwal wajib diisi")
        if not pemesan.strip():
            raise InvalidBookingError("Nama pemesan wajib diisi")
        normalized_codes = self._normalize_seat_codes(seat_codes)
        state = self._get_showtime(jadwal_id)
        valid_codes = set(self._seat_codes(state.info))
        invalid_codes = tuple(code for code in normalized_codes if code not in valid_codes)
        if invalid_codes:
            raise InvalidSeatError(invalid_codes)

        with state.lock:
            unavailable = tuple(
                code for code in normalized_codes if code in state.booked_seats
            )
            if unavailable:
                return BookingResult(
                    sukses=False,
                    pesan="Satu atau lebih kursi sudah dipesan",
                    kursi_gagal=unavailable,
                )

            booking_id = f"{uuid4()}:{jadwal_id}"
            while booking_id in state.bookings:
                booking_id = f"{uuid4()}:{jadwal_id}"
            state.booked_seats.update(normalized_codes)
            state.bookings[booking_id] = normalized_codes

        return BookingResult(
            sukses=True,
            pesan="Pemesanan berhasil",
            pesanan_id=booking_id,
            kursi_berubah=normalized_codes,
        )

    def cancel_booking(self, booking_id: str) -> CancellationResult:
        if not booking_id.strip():
            raise InvalidBookingError("ID pesanan wajib diisi")
        _, separator, jadwal_id = booking_id.partition(":")
        if not separator:
            raise BookingNotFoundError(f"Pesanan tidak ditemukan: {booking_id}")
        try:
            state = self._showtimes[jadwal_id]
        except KeyError as exc:
            raise BookingNotFoundError(
                f"Pesanan tidak ditemukan: {booking_id}"
            ) from exc

        with state.lock:
            seat_codes = state.bookings.pop(booking_id, None)
            if seat_codes is None:
                raise BookingNotFoundError(f"Pesanan tidak ditemukan: {booking_id}")
            state.booked_seats.difference_update(seat_codes)

        return CancellationResult(
            sukses=True,
            pesan="Pesanan berhasil dibatalkan",
            jadwal_id=jadwal_id,
            kursi_berubah=seat_codes,
        )