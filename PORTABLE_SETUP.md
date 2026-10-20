# Document Finder: portable Windows setup

## Run from any folder

1. Keep `optimized_docfinder.py`, `Run_DocFinder.bat`, and `requirements.txt` together. You can place that code folder on the PC or in a OneDrive folder.
2. Double-click `Run_DocFinder.bat`. It creates a Python virtual environment and stores app data under `%LOCALAPPDATA%\DocFinder`, not beside the code. It does not need Ghostscript; that is optional.
3. Keep the PC awake while people use the app. Open the local URL printed in the console (usually `http://localhost:5001`). To share it within the office, use the PC's LAN address and have IT allow the port if needed.

Python 3 must already be available on Windows. The launcher installs Python packages into the current Windows user's Local AppData; it does not request administrator permissions. If your company blocks Python or package downloads, ask IT to install/approve them.

## Preserve an existing installation

Before moving, stop the old app and make a backup of its `docfinder.db` (and `outputs` folder if you need previously generated files). Copy the existing `docfinder.db` into `%LOCALAPPDATA%\DocFinder\docfinder.db` before the first launch of this portable bundle. Otherwise the app creates a new, empty database. Folder paths in that database are absolute; the new host must still be able to access those same folders. If paths have changed, re-add the folders in the app.

For a new database, the default first-login credentials are `Movindu` / `ChangeMe123`; change the password promptly. Existing databases retain their own users and passwords.

## OneDrive and cloud

OneDrive syncs files; it does not run the Python web app while your PC is off. The launcher intentionally keeps the live SQLite database outside OneDrive because syncing a changing database/WAL files can cause conflicts or damage. Keep code in OneDrive if convenient, but run it on one designated PC and keep the spool folders available to that PC. For OneDrive-hosted spool files, use the OneDrive desktop client and mark the needed folders **Always keep on this device** so files are actually available locally.

To use it while away from the office, the safer arrangement is to run it on an always-on office PC/server and connect through your company's VPN. A cloud VM is also possible, but it must have persistent storage and authorized access to the spool files (for example, a supported sync client or network connection). OneDrive itself is not a Python hosting service. Do not expose this app directly to the public internet; use IT-approved VPN/reverse-proxy security, change the default password, and have IT review access controls.

The launcher allows the script's optional Cloudflare quick tunnel. To get a public HTTPS link, put the approved Windows `cloudflared.exe` beside `optimized_docfinder.py` and restart the app. If it is absent, the app remains available locally/LAN only and prints that the public link is off. Change the app's default admin password before using a public tunnel; the link is reachable from the internet. Prefer your company's VPN/approved access gateway for sensitive company files, and ask IT to approve the tunnel and executable.
