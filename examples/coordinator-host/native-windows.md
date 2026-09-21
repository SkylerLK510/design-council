Run the coordinator natively on Windows.

A coordinator is a small network service with a database and files, and native Windows gives it direct LAN reachability with no NAT layer in between, so the Mac can connect to the desktop's address directly. It sits on the permanent NTFS storage without crossing the slow mount boundary that WSL uses for Windows drives, and it keeps running whether or not the WSL virtual machine is up. WSL would be used only for training workers, if it is needed at all.

The cost is that Windows file locking, rename-over-existing and service management differ from macOS where the code was developed. I assume, without having tested it, that SQLite and atomic rename behave acceptably on NTFS. To find out, run the existing test suite on Windows before relying on it; if rename or locking tests fail there, that is the signal to reconsider.
