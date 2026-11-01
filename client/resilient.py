"""client/resilient.py — wrapper ketahanan pemanggilan RPC unary gRPC.

Pemilik tugas: Anggota 2 ("Client, Ketahanan, Kinerja & Deployment").
Semua pemanggilan client untuk konektivitas server dipakai wrapper ini.

Perilaku sesuai AGENTS.md §3.4:
- Setiap pemanggilan memakai ``timeout`` (default 2 detik).
- Retry HANYA untuk kode ``DEADLINE_EXCEEDED`` dan ``UNAVAILABLE`` dengan
  exponential backoff 0.5 s -> 1 s -> 2 s, maksimal 3 percobaan.
- Operasi NON-idempoten (PesanKursi, BatalkanPesanan) dilindungi
  ``request_id`` (UUID) yang dibuat SEKALI per operasi logis oleh pemanggil
  (client/main.py) dan dipasang di dalam request. Wrapper TIDAK PERNAH
  membuat UUID baru per percobaan — request asli dikirim ulang apa adanya,
  sehingga server (DedupStore) dapat menjalankan semantik at-most-once.
- Operasi idempoten (ListJadwal, GetDenah) boleh di-retry bebas tanpa
  request_id.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

import grpc

# Kode yang memenuhi syarat retry. Error "cerdas" dari server seperti
# ALREADY_EXISTS / FAILED_PRECONDITION terjadi deterministik sehingga
# di-retry hanya akan menghasilkan kegagalan yang sama — TIDAK di-retry.
RETRYABLE_CODES: tuple[grpc.StatusCode, ...] = (
    grpc.StatusCode.DEADLINE_EXCEEDED,
    grpc.StatusCode.UNAVAILABLE,
)

DEFAULT_TIMEOUT_S: float = 2.0
DEFAULT_MAX_ATTEMPTS: int = 3

# Exponential backoff: jeda sebelum percobaan ke-2, ke-3, ke-4 dst.
DEFAULT_BACKOFF_S: tuple[float, ...] = (0.5, 1.0, 2.0)

_PESAN_KODE: dict[str, str] = {
    "DEADLINE_EXCEEDED": "server tidak menjawab dalam batas waktu",
    "UNAVAILABLE": "server tidak dapat dihubungi (mati/koneksi bermasalah)",
    "NOT_FOUND": "objek tidak ditemukan di server",
    "INVALID_ARGUMENT": "argumen tidak valid",
    "FAILED_PRECONDITION": "prasyarat tidak terpenuhi (mis. kursi sudah terisi)",
    "ALREADY_EXISTS": "objek sudah ada (mis. kursi sudah dipesan)",
    "INTERNAL": "terjadi kesalahan internal di server",
    "UNIMPLEMENTED": "metode belum diimplementasi di server",
}


def pesan_ramah(kode: grpc.StatusCode | None, detail: str = "") -> str:
    """Terjemahkan kode gRPC + detail jadi satu kalimat ramah untuk pengguna."""
    dasar = (
        _PESAN_KODE.get(kode.name, "terjadi kesalahan gRPC")
        if kode is not None
        else "terjadi kesalahan tak terduga"
    )
    return f"{dasar} ({detail})." if detail else f"{dasar}."


class RpcGagal(Exception):
    """Dibangkitkan bila semua percobaan habis, atau kode tidak dapat di-retry.

    CLI menangkap exception ini untuk menampilkan pesan terkontrol
    (bukan traceback) — AGENTS.md §8 pengujian minimum no. 5.
    """

    def __init__(
        self,
        pesan: str,
        kode: grpc.StatusCode | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(pesan)
        self.pesan = pesan
        self.kode = kode
        self.detail = detail


def panggil_rpc(
    op: Callable[..., Any],
    request: Any,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff: Sequence[float] = DEFAULT_BACKOFF_S,
) -> Any:
    """Panggil satu RPC unary dengan timeout + retry + backoff.

    op      : method stub yang sudah ter-bind, mis. ``stub.PesanKursi``.
    request : pesan request. Untuk operasi non-idempoten, ``request_id``
              di dalamnya HARUS sudah terisi dan tetap sama pada semua
              percobaan (object sama yang dikirim ulang).
    timeout : deadline per percobaan (detik).
    max_attempts : jumlah percobaan maksimum (termasuk percobaan pertama).
    backoff : daftar jeda sebelum percobaan berikutnya (detik).

    Mengembalikan message response bila sukses, atau membangkitkan
    ``RpcGagal`` bila berakhir gagal.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts minimal 1")

    terakhir: grpc.RpcError | None = None
    for percobaan in range(1, max_attempts + 1):
        try:
            # Request yang sama dipakai ulang → request_id konsisten semua retry.
            return op(request, timeout=timeout)
        except grpc.RpcError as e:
            kode = e.code()
            terakhir = e
            if kode not in RETRYABLE_CODES:
                raise RpcGagal(pesan_ramah(kode, e.details() or ""), kode, e.details() or "") from e
            if percobaan < max_attempts:
                indeks = min(percobaan - 1, len(backoff) - 1)
                time.sleep(backoff[indeks])  # hanya di jalur retry client

    # Semua percobaan habis (DEADLINE_EXCEEDED/UNAVAILABLE).
    assert terakhir is not None
    kode = terakhir.code()
    raise RpcGagal(
        pesan_ramah(kode, terakhir.details() or ""), kode, terakhir.details() or ""
    ) from terakhir
