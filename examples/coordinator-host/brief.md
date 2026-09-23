Decision: where should the job coordinator run on the Windows 11 desktop, natively on Windows or inside WSL2?

Context: a personal two-machine compute lab. The desktop (Ryzen 9800X3D, RTX 5070, 32 GB) is the always-on backbone with the permanent storage. A MacBook Pro is an optional worker that must reach the coordinator over the home network. The coordinator is a small Python standard-library HTTP service backed by one SQLite file. Checkpoints rely on atomic rename and fsync.

Hard constraints:
- Remote access to the desktop is NOT configured and must not be assumed. Nothing may depend on SSH or remote desktop existing.
- The desktop's WSL status is unknown; nothing may assume WSL is installed or working.
- The desktop's CUDA setup is unknown; nothing may assume a working CUDA stack.
- The desktop's network link is unknown; nothing may assume the LAN works until checked.
- The Mac worker must be able to reach the coordinator over the LAN.
- Checkpoint files must end up on the desktop's permanent storage.
- Nothing may be assumed to behave on Windows or WSL2 as it does on macOS until it has been tested there.
