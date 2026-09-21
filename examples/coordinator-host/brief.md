Decision: where should the job coordinator run on the Windows 11 desktop, natively on Windows or inside WSL2?

Context: a personal two-machine compute lab. The desktop (Ryzen 9800X3D, RTX 5070, 32 GB) is the always-on backbone with the permanent storage. A MacBook Pro is an optional worker that must reach the coordinator over the home network. The coordinator is a small Python standard-library HTTP service backed by one SQLite file. Checkpoints rely on atomic rename and fsync.

Hard constraints:
- Remote access to the desktop is NOT configured and must not be assumed. Nothing may depend on SSH or remote desktop existing.
- The desktop's WSL status, CUDA setup and network link are unknown. No stack assumption may be treated as verified.
- The Mac worker must be able to reach the coordinator over the LAN.
- Checkpoint files must end up on the desktop's permanent storage.
- So far everything has been developed and tested only on macOS.
