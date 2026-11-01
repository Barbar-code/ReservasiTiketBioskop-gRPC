from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from server.booking import BookingManager, SeatStatus, ShowtimeInfo


def test_fifty_concurrent_requests_cannot_double_book_one_seat() -> None:
    manager = BookingManager([ShowtimeInfo("show-1", "Film Uji", "19:00", 2, 3)])
    barrier = Barrier(50)

    def book_seat(index: int) -> bool:
        barrier.wait()
        return manager.book_seats("show-1", f"Pemesan {index}", ["A1"]).sukses

    with ThreadPoolExecutor(max_workers=50) as executor:
        results = list(executor.map(book_seat, range(50)))

    assert sum(results) == 1
    assert sum(not result for result in results) == 49
    seats = {seat.kode: seat.status for seat in manager.get_seats("show-1")}
    assert seats["A1"] is SeatStatus.BOOKED