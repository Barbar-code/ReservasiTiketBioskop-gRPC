from __future__ import annotations

from queue import Queue
from threading import Lock

from proto import bioskop_pb2


class SeatEventHub:
    def __init__(self) -> None:
        self._lock = Lock()
        self._subscribers: dict[str, set[Queue[bioskop_pb2.PerubahanKursi]]] = {}

    def subscribe(self, jadwal_id: str) -> Queue[bioskop_pb2.PerubahanKursi]:
        subscriber: Queue[bioskop_pb2.PerubahanKursi] = Queue()
        with self._lock:
            self._subscribers.setdefault(jadwal_id, set()).add(subscriber)
        return subscriber

    def unsubscribe(
        self,
        jadwal_id: str,
        subscriber: Queue[bioskop_pb2.PerubahanKursi],
    ) -> None:
        with self._lock:
            subscribers = self._subscribers.get(jadwal_id)
            if subscribers is None:
                return
            subscribers.discard(subscriber)
            if not subscribers:
                del self._subscribers[jadwal_id]

    def publish(self, event: bioskop_pb2.PerubahanKursi) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers.get(event.jadwal_id, ()))
        for subscriber in subscribers:
            subscriber.put(event)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return sum(len(subscribers) for subscribers in self._subscribers.values())