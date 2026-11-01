from __future__ import annotations

import pytest

from server.booking import (
    BookingManager,
    BookingNotFoundError,
    InvalidBookingError,
    InvalidSeatError,
    SeatStatus,
    ShowtimeInfo,
    ShowtimeNotFoundError,
)


@pytest.fixture
def manager() -> BookingManager:
    return BookingManager(
        [
            ShowtimeInfo("show-1", "Film Uji", "19:00", 2, 3),
            ShowtimeInfo("show-2", "Film Lain", "20:00", 2, 3),
        ]
    )


def test_get_seats_returns_full_layout(manager: BookingManager) -> None:
    seats = manager.get_seats("show-1")

    assert [seat.kode for seat in seats] == ["A1", "A2", "A3", "B1", "B2", "B3"]
    assert all(seat.status is SeatStatus.AVAILABLE for seat in seats)


def test_unknown_showtime_is_rejected(manager: BookingManager) -> None:
    with pytest.raises(ShowtimeNotFoundError):
        manager.get_seats("missing")


def test_booking_is_atomic_when_one_seat_is_unavailable(
    manager: BookingManager,
) -> None:
    first = manager.book_seats("show-1", "Ayu", ["B2"])

    result = manager.book_seats("show-1", "Bima", ["A1", "B2"])
    seats = {seat.kode: seat.status for seat in manager.get_seats("show-1")}

    assert first.sukses
    assert not result.sukses
    assert result.kursi_gagal == ("B2",)
    assert seats["A1"] is SeatStatus.AVAILABLE
    assert seats["B2"] is SeatStatus.BOOKED


def test_invalid_and_duplicate_seats_are_rejected(manager: BookingManager) -> None:
    with pytest.raises(InvalidSeatError):
        manager.book_seats("show-1", "Ayu", ["Z9"])
    with pytest.raises(InvalidBookingError):
        manager.book_seats("show-1", "Ayu", ["A1", "a1"])
    with pytest.raises(InvalidBookingError):
        manager.book_seats("show-1", "Ayu", [])


def test_cancel_restores_seats_and_cannot_be_repeated(
    manager: BookingManager,
) -> None:
    booking = manager.book_seats("show-1", "Ayu", ["A1", "A2"])

    result = manager.cancel_booking(booking.pesanan_id)

    assert result.sukses
    assert result.kursi_berubah == ("A1", "A2")
    assert all(
        seat.status is SeatStatus.AVAILABLE
        for seat in manager.get_seats("show-1")[:2]
    )
    with pytest.raises(BookingNotFoundError):
        manager.cancel_booking(booking.pesanan_id)


def test_same_seat_can_be_booked_in_another_showtime(
    manager: BookingManager,
) -> None:
    assert manager.book_seats("show-1", "Ayu", ["A1"]).sukses
    assert manager.book_seats("show-2", "Bima", ["A1"]).sukses