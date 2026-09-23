Both options are reasonable and each has trade-offs.

Native Windows avoids the WSL2 NAT layer and puts files directly on NTFS, but Windows file locking and rename semantics differ from macOS, where the code was developed. WSL2 gives a Linux environment closer to the Mac and makes CUDA tooling familiar, but LAN access needs port forwarding and files under /mnt/c are slow to reach from Linux.

The right choice depends on priorities such as performance, familiarity and maintenance effort. These should be discussed with everyone involved before anything is set up.
