# DicomExtractor on two LAN PCs

On the server PC, run `start_server.bat` from the project folder and keep the window open. Open `http://localhost:8000/` on that PC. For another device, use `http://<server IPv4 address>:8000/`; the launcher lists the server's current IPv4 addresses. Use an address the other device can reach over the LAN or VPN.

The launcher installs missing Python packages, applies database migrations, and listens on all network interfaces at port 8000. Use Python on `PATH`; the copied `venv` points to another PC's Python installation and is not used. On a new installation, the launcher asks you to set the `admin` password in its console before the server starts. Existing installations keep their current admin password; change it with `python manage.py changepassword admin` if needed. Django accepts the server's current IPv4 addresses at startup. If you access it through a DNS name or another address, add that name or address to the comma-separated `DICOM_ALLOWED_HOSTS` environment variable before starting the server. Restart the server after its network addresses change.

For the planned address `192.168.10.170`, first assign that address to this PC's network adapter and confirm it appears in `ipconfig`. Restart `start_server.bat` after the address is assigned, then open `http://192.168.10.170:8000/` from a second device that can reach this PC. The app already accepts this address as a host name. The firewall helper includes `192.168.10.0/24`, but an administrator must review and run it if Windows Firewall blocks inbound TCP 8000. An address that is not assigned to this PC cannot be served by the app.

The page's Bootstrap styles, controls, and icon fonts are included under `extractor/static/`. LAN clients do not need internet access for the interface. If a page does not load, check that this PC's server is running and that the entire `extractor/static/` folder was copied, then reload the page.

To update an existing copy on another PC, stop its server, copy the source files, and restart it. Preserve that PC's `db.sqlite3`, `media/`, `extraction_output/`, and `.django_secret_key` if it has jobs or uploaded cases. The source-only update ZIP omits those data files. The first start with this update applies the login migration. The secret key file is created locally on each PC and must remain private.

If other PCs cannot reach a server, check that they can route to its address and that inbound TCP port 8000 is allowed on the server PC. An administrator can review `enable_lan_firewall.ps1` for the known `192.168.1.0/24`, `192.168.10.0/24`, and `172.30.30.0/24` networks. Its network list must include the client network before it can help with other subnets. Firewall or VPN policy managed by your organization may also need to permit the connection.

On a managed PC, check `netsh advfirewall show currentprofile`. If it reports `LocalFirewallRules N/A (GPO-store only)`, locally added firewall rules may not apply. Ask the network administrator to allow inbound TCP 8000 from the intended client subnet in the managed policy, then test from a second PC. Do not assume that opening this PC's own LAN URL proves another PC can connect.

Each running copy has its own database and job history. For one shared job history, have everyone use one server address.

After a scan finishes extracting, its status page opens the DICOM viewer automatically. Completed jobs can also be opened from the Viewer link in the top bar or from All Jobs. Axial and the 3D mask surface are side by side, with coronal and sagittal below. The four key metadata fields are above them. Each view can be minimized or maximized. The sidebar has grayscale and threshold controls. A selected threshold generates a full-resolution binary mask, highlights it in color on the CT views, and builds a smoothed 3D mesh. Threshold edits refresh the mask automatically. The mask exports as NRRD and the 3D mesh as STL or OBJ. Hold Shift and left click a CT image to place a synchronized locator; use Clear locator to remove it. Presets are visual starting points, not validated tissue segmentation; verify orientation, scale, and anatomy before clinical use. The browser keeps up to 1 GiB of decoded CT and mask slices in memory. The first coronal, sagittal, or 3D request builds a file-backed volume under `extraction_output/.viewer_cache/`; allow extra disk space about equal to the uncompressed DICOM pixel volume. Calculated binary masks are cached under `extraction_output/.viewer_masks/`, up to four per scan.

## Accounts and recovery

Only the administrator can create new accounts from **New user** in the top bar. Every new account must have a unique email address ending in `@3ddx.com`. The sign-in and password forms have an eye button to show or hide the entered password.

**Forgot password?** creates an eight-digit, single-use recovery code. It expires after 10 minutes and locks after five attempts. Until SMTP is configured, the administrator can see pending codes under **Recovery** and give a code to the verified account owner through an existing trusted channel. An account without an email can still request a code by username for manual delivery. If the only administrator forgets their password and email is not configured, someone with access to the server can run `python manage.py changepassword admin`.

To deliver codes automatically, configure an SMTP sender in the server PC's environment before launching `start_server.bat`:

| Variable | Meaning |
| --- | --- |
| `DICOM_SMTP_HOST` | SMTP server hostname |
| `DICOM_SMTP_PORT` | Port; defaults to `587` |
| `DICOM_SMTP_FROM` | Sender email address |
| `DICOM_SMTP_USER` | SMTP username, if required |
| `DICOM_SMTP_PASSWORD` | SMTP password, if required |
| `DICOM_SMTP_TLS` | `1` for STARTTLS (default), `0` to disable |
| `DICOM_SMTP_SSL` | `1` for implicit TLS; this disables STARTTLS |

Keep SMTP credentials in the PC's environment, not in project source files. The app sends codes only to stored `@3ddx.com` account addresses. If the SMTP sender fails, the code remains available to the administrator for manual delivery.
