from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from proto import bioskop_pb2
from server.booking import BookingManager, ShowtimeInfo
from server.events import SeatEventHub
from server.service import LayananBioskopServicer


class _Context:
    def __init__(self) -> None:
        self.active = True
        self.callback = None

    def is_active(self) -> bool:
        return self.active

    def add_callback(self, callback) -> bool:
        self.callback = callback
        return True


def test_event_hub_broadcasts_only_to_matching_showtime() -> None:
    hub = SeatEventHub()
    first = hub.subscribe("show-1")
    second = hub.subscribe("show-1")
    other_showtime = hub.subscribe("show-2")

    event = bioskop_pb2.PerubahanKursi(
        jadwal_id="show-1",
        kode="A1",
        status=bioskop_pb2.DIPESAN,
    )
    hub.publish(event)

    assert first.get_nowait() == event
    assert second.get_nowait() == event
    assert other_showtime.empty()
    hub.unsubscribe("show-1", first)
    hub.unsubscribe("show-1", second)
    hub.unsubscribe("show-2", other_showtime)
    assert hub.subscriber_count == 0


def test_stream_sends_events_and_cleans_up_on_disconnect() -> None:
    hub = SeatEventHub()
    service = LayananBioskopServicer(
        BookingManager([ShowtimeInfo("show-1", "Film Uji", "19:00", 2, 3)]),
        hub,
    )
    context = _Context()
    stream = service.StreamStatusKursi(
        bioskop_pb2.DenahRequest(jadwal_id="show-1"),
        context,
    )
    started = Event()

    def receive_event():
        started.set()
        return next(stream)

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(receive_event)
        assert started.wait(timeout=1)
        for _ in range(100):
            if hub.subscriber_count == 1:
                break
            Event().wait(0.01)
        assert hub.subscriber_count == 1

        expected = bioskop_pb2.PerubahanKursi(
            jadwal_id="show-1",
            kode="A1",
            status=bioskop_pb2.DIPESAN,
        )
        hub.publish(expected)
        assert result.result(timeout=1) == expected

    assert context.callback is not None
    context.active = False
    context.callback()
    stream.close()
    assert hub.subscriber_count == 0
