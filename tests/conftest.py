"""tests/conftest.py — persiapan umum untuk seluruh folder tests/.

Stub proto (*_pb2.py) tidak di-commit (AGENTS.md §4), jadi bila belum ada,
conftest me-generate dari proto/bioskop.proto sebelum tes dijalankan.
Bila file proto di repo masih kosong (menunggu persetujuan kontrak final
kedua anggota), stub harus dibuat manual — lihat README.md.

Pemilik tugas: Anggota 2 (bersama tugas pengujian).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Agar 'server.*', 'client.*', dan 'bioskop_pb2*' dapat diimpor dari tests/.
sys.path.insert(0, str(ROOT))


def _stub_tersedia() -> bool:
    return (ROOT / "proto" / "bioskop_pb2.py").exists() and (
        ROOT / "proto" / "bioskop_pb2_grpc.py"
    ).exists()


def _generate_stub_jika_perlu() -> None:
    if _stub_tersedia():
        return
    proto = ROOT / "proto" / "bioskop.proto"
    if not (proto.exists() and proto.stat().st_size > 0):
        # Proto final belum ada — tes yang butuh stub akan gagal import;
        # lewati generate agar pesan errornya tetap jelas.
        return
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            "-I",
            str(ROOT),
            "--python_out",
            str(ROOT),
            "--grpc_python_out",
            str(ROOT),
            "proto/bioskop.proto",
        ],
        check=True,
        cwd=str(ROOT),
    )


_generate_stub_jika_perlu()
