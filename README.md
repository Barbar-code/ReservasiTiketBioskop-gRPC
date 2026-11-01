# Sistem Reservasi Tiket Bioskop berbasis gRPC

Proyek ini menyediakan layanan gRPC untuk melihat jadwal dan denah kursi,
memesan atau membatalkan kursi, serta memantau perubahan status kursi.

## Instalasi dan generate stub

Gunakan Python 3.10 atau yang lebih baru:

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m grpc_tools.protoc -I . --python_out=. --grpc_python_out=. proto/bioskop.proto
```

Stub juga dapat dibuat dengan `make proto` atau `scripts/gen_proto.sh` pada
lingkungan POSIX. File hasil generate tidak disimpan di Git.

## Menjalankan server

```powershell
python -m server.main --port 50051 --workers 10
```

Server menyediakan dua jadwal seed (`jadwal-1` dan `jadwal-2`) dengan denah
6 baris x 10 kursi. Data pemesanan berada di memori dan akan kembali ke kondisi
awal saat server dimulai ulang. Setiap jadwal memiliki lock tersendiri; semua
kursi dalam satu transaksi diperiksa dan dipesan dalam critical section yang
sama. Peristiwa streaming dipancarkan setelah lock dilepas agar subscriber
tidak memperlambat pemesanan.

## Pengujian

```powershell
python -m pytest tests/ -v
```

Client CLI, retry/dedup, benchmark, dan deployment dikerjakan pada bagian
Anggota 2.