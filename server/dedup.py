"""server/dedup.py — DedupStore: request_id -> response tersimpan.

Pemilik tugas: Anggota 2 (AGENTS.md §6, item "dedup.py + --simulate-slow").

Servicer (Anggota 1, server/service.py) memanggil get()/put() di sekitar
handler operasi NON-idempoten (PesanKursi, BatalkanPesanan):

    data = dedup.get(request.request_id)
    if data is not None:
        return data                   # duplikat: jawab tanpa eksekusi ulang
    data = ... eksekusi logika bisnis ...
    dedup.put(request.request_id, data)
    return data

Semantik yang dihasilkan: at-most-once untuk operasi non-idempoten.
request_id dibuat client sekali per operasi logis dan dipakai ulang pada
setiap retry (lihat client/resilient.py).

Batasan (dijelaskan di laporan, AGENTS.md §3.4): penyimpanan in-memory
sehingga isi hilang saat server restart — at-most-once tidak terjamin
lintas restart. Usulan perbaikan: simpan ke file/SQLite.
"""

from __future__ import annotations

import threading
import time
from typing import Any

DEFAULT_TTL_S: float = 600.0


class DedupStore:
    """Simpan response per request_id dengan TTL; aman dipakai multi-thread.

    Laten dibungkus lock (AGENTS.md §8: tidak ada state global yang
    diubah tanpa lock). Data di simpan apa adanya (ref ke message proto);
    servicer diharapkan tidak memutasi data setelah put().
    """

    def __init__(self, ttl_s: float = DEFAULT_TTL_S) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s harus > 0")
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        # request_id -> (data, waktu_simpan [monotonic])
        self._data: dict[str, tuple[Any, float]] = {}

    def get(self, request_id: str) -> Any | None:
        """Ambil data; None bila tidak ada atau sudah kedaluwarsa."""
        if not request_id:
            return None
        sekarang = time.monotonic()
        with self._lock:
            entri = self._data.get(request_id)
            if entri is None:
                return None
            data, waktu_simpan = entri
            if sekarang - waktu_simpan > self._ttl_s:
                del self._data[request_id]
                return None
            return data

    def put(self, request_id: str, data: Any) -> None:
        """Simpan response; request_id kosong diabaikan (tanpa jaminan dedup)."""
        if not request_id:
            return
        sekarang = time.monotonic()
        with self._lock:
            self._bersihkan_kedaluwarsa(sekarang)  # lazy cleanup tiap put
            self._data[request_id] = (data, sekarang)

    def bersihkan(self) -> int:
        """Hapus semua entri kedaluwarsa; kembalikan jumlah entri tersisa."""
        sekarang = time.monotonic()
        with self._lock:
            self._bersihkan_kedaluwarsa(sekarang)
            return len(self._data)

    def _bersihkan_kedaluwarsa(self, sekarang: float) -> None:
        # Harus dipanggil dengan self._lock sudah terpegang.
        kedaluwarsa = [
            rid
            for rid, (_data, waktu_simpan) in self._data.items()
            if sekarang - waktu_simpan > self._ttl_s
        ]
        for rid in kedaluwarsa:
            del self._data[rid]
