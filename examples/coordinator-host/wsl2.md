Run the coordinator inside WSL2.

WSL2 is basically Linux, so everything that works on the Mac will work the same way and there is nothing new to learn. CUDA training will run in WSL2 anyway, so keeping the coordinator next to the workers is cleanest. Put the SQLite file and checkpoints in the Linux filesystem because it is faster than the Windows mount.

Networking is easy: turn on mirrored networking mode and the Mac can connect. To set it all up, SSH into the desktop from the Mac and install everything remotely. This is the standard approach and it will work.
