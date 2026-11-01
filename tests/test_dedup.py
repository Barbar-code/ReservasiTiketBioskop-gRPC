"""tests/test_dedup.py — bukti retry tidak menggandakan pemesanan.

Pemilik tugas: Anggota 2 ("Client, Ketahanan, Kinerja & Deployment").

Skenario minimum AGENTS.md §8 no. 3: "Retry dengan request_id sama setelah
balasan hilang → pesanan hanya tercipta sekali." Yang diuji:

1. Aturan dasar DedupStore (unit): put/get, TTL kedaluwarsa, id kosong.
2. End-to-end lewat server gRPC sungguhan: request_id yang SAMA dikirim
   dua kali (simulasi client retry karena balasan pertama dianggap hilang)
   → handler logika hanya dieksekusi SEKALI dan kedua respons identik.
3. request_id BERBEDA → dieksekusi normal (dua operasi berbeda tidak
   boleh tersangka duplikat).

Servicer di bawah ini dibuat khusus pengujian (self-contained) dan TIDAK
menggantikan server/service.py milik Anggota 1 — pola dedup di sini
memang yang harus diharapkan di service.py: get dulu, eksekusi sekali,
lalu put.

Catatan: proses gRPC dijalankan dengan ThreadPoolExecutor + port acak,
jadi tidak menyentuh jalur normal server di luar pengukuran; sleep hanya
untuk kebutuhan uji TTL (pustaka pengujian).
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent import futures

import grpc
import pytest

from proto import bioskop_pb2 as pb2
from proto import bioskop_pb2_grpc as pb2_grpc

from server.dedup import DedupStore


# ---------------------------------------------------------------------------
# Aturan dasar DedupStore (unit test)
# ---------------------------------------------------------------------------

class TestDedupStoreUnit:
    def test_put_get_siklus_dasar(self):
        store = DedupStore()
        assert store.get("rid-1") is None
        store.put("rid-1", "jawabannya")
        assert store.get("rid-1") == "jawabannya"

    def test_id_kosong_diabaikan(self):
        store = DedupStore()
        store.put("", "x")
        assert store.get("") is None

    def test_ttl_kedaluwarsa(self):
        store = DedupStore(ttl_s=0.05)
        store.put("rid-1", "x")
        time.sleep(0.08)  # hanya untuk kebutuhan uji TTL
        assert store.get("rid-1") is None

    def test_nilai_baru_menggantikan_lama(self):
        store = DedupStore()
        store.put("rid-1", "lama")
        store.put("rid-1", "baru")
        assert store.get("rid-1") == "baru"


# ---------------------------------------------------------------------------
# Test end-to-end: server gRPC + DedupStore + request_id (uji inti)
# ---------------------------------------------------------------------------

class ServicerDedupUji(pb2_grpc.LayananBioskopServicer):
    """Servicer minimum yang meng-kawinkan DedupStore dengan PesanKursi.

    Menyimpan berapa kali logika pemesanan BENERAN dieksekusi (bukan
    berapa kali request masuk) — kunci bukti at-most-once.
    """

    def __init__(self, store: DedupStore) -> None:
        self.store = store
        self._lock = threading.Lock()
        self.eksekusi = 0

    def PesanKursi(self, request, context):
        # 1) Cek dedup — semua operasi non-idempoten WAJIB lewat sini.
        data = self.store.get(request.request_id)
        if data is not None:
            return data  # duplikat (mis. hasil retry): jawab tanpa eksekusi ulang

        # 2) Eksekusi logika bisnis (diuji: hanya boleh terjadi sekali per
        #    request_id). Di service.py Anggota 1, di sini booking atomik,
        #    di pengujian ini cukup counter + pesanan sintetik.
        with self._lock:
            self.eksekusi += 1
            nomor = self.eksekusi
        data = pb2.PesanKursiResponse(
            sukses=True,
            pesan=f"tereksekusi-{nomor}",
            pesanan_id=f"PESAN-{nomor}",
        )

        # 3) Simpan dulu, baru jawab — bila balasan hilang, retry menemukannya.
        self.store.put(request.request_id, data)
        return data


def _kaitkan_port(server: grpc.Server) -> int:
    """Bind port acak; fallback IPv4 bila IPv6 tidak tersedia."""
    port = server.add_insecure_port("[::]:0")
    if port == 0:
        port = server.add_insecure_port("127.0.0.1:0")
    assert port != 0, "gagal bind port acak"
    return port


@pytest.fixture()
def server_uji():
    """Server gRPC sungguhan (port acak) + servicer uji + DedupStore."""
    store = DedupStore(ttl_s=60.0)
    servicer = ServicerDedupUji(store)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    pb2_grpc.add_LayananBioskopServicer_to_server(servicer, server)
    port = _kaitkan_port(server)
    server.start()

    yield servicer, port

    server.stop(grace=0.2).wait()


class TestDedupEndToEnd:
    def test_request_id_sama_diretry_dieksekusi_sekali(self, server_uji):
        servicer, port = server_uji
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = pb2_grpc.LayananBioskopStub(channel)

            # request_id dibuat SEKALI (seperti client/resilient.py harusnya).
            rid = str(uuid.uuid4())
            req = pb2.PesanKursiRequest(
                request_id=rid,
                jadwal_id="J1",
                pemesan="uji",
                kode_kursi=["A1"],
            )

            r1 = stub.PesanKursi(req, timeout=2.0)
            # Simulasi balasan pertama hilang: KIRIM ULANG request YANG SAMA.
            r2 = stub.PesanKursi(req, timeout=2.0)

        assert servicer.eksekusi == 1, "logika bisnis dieksekusi 2x untuk request_id sama"
        assert r1.sukses and r2.sukses
        assert r1.pesanan_id == r2.pesanan_id, "semua karakteristik respons harus identik (at-most-once)"
        assert r1.pesan == r2.pesan

    def test_request_id_berbeda_dieksekusi_normal(self, server_uji):
        servicer, port = server_uji
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = pb2_grpc.LayananBioskopStub(channel)
            for i in range(2):
                req = pb2.PesanKursiRequest(
                    request_id=str(uuid.uuid4()),  # operasi logis berbeda
                    jadwal_id="J1",
                    pemesan=f"uji-{i}",
                    kode_kursi=["A1"],
                )
                resp = stub.PesanKursi(req, timeout=2.0)
                assert resp.sukses

        assert servicer.eksekusi == 2, "operasi dengan request_id berbeda tidak boleh terdedup"
