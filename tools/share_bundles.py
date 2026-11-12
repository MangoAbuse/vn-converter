"""Let the iPad download the bundles from this PC over the home Wi-Fi (no cloud involved).

    python share_bundles.py            then open the address it prints in Safari on the iPad

It offers ONLY the .vnbundle.zip files in ..\\out, and only while this window is open.
Anyone on the same network could fetch them during that time, so run it at home and
close the window (or press Ctrl+C) when the downloads are done.

The first time, Windows Firewall asks whether to allow Python on private networks:
choose Allow, otherwise the iPad cannot connect.
"""
import html
import shutil
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

OUT = Path(__file__).resolve().parent.parent / "out"


def bundles() -> list[Path]:
    return sorted(OUT.glob("*.vnbundle.zip"))


def size_text(n: int) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Game bundles</title>
<style>body{{font:18px/1.5 -apple-system,Segoe UI,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;
background:#0e1016;color:#e9ecf5}}a{{display:block;margin:12px 0;padding:16px 18px;border-radius:12px;
background:#232737;color:#7fb4ff;text-decoration:none;font-weight:600}}small{{color:#9aa3bb;font-weight:400}}
p{{color:#9aa3bb}}</style>
<h1>Game bundles</h1>
<p>Tap a bundle, then tap <b>Download</b>. It is saved in the Files app under Downloads.</p>
{links}
<p>When the downloads have finished, close the window on the PC.</p>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        name = unquote(self.path.split("?", 1)[0].lstrip("/"))
        if name == "":
            links = "".join(
                f'<a href="/{quote(p.name)}" download>{html.escape(p.name)}<br><small>{size_text(p.stat().st_size)}</small></a>'
                for p in bundles()) or "<p>No bundles found. Run the converter first.</p>"
            body = PAGE.format(links=links).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command == "GET":
                self.wfile.write(body)
            return
        match = next((p for p in bundles() if p.name == name), None)     # only files from the list
        if not match:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(match.stat().st_size))
        self.send_header("Content-Disposition", f'attachment; filename="{match.name}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command == "GET":
            with match.open("rb") as source:
                try:
                    shutil.copyfileobj(source, self.wfile, 1024 * 1024)
                except (ConnectionError, OSError):
                    pass                                    # the download was cancelled

    do_HEAD = do_GET

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s  %s\n" % (self.address_string(), fmt % args))


def lan_addresses() -> list[str]:
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 9))                      # no packet is sent; this only picks the outgoing interface
        found.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    return sorted(a for a in found if not a.startswith("127."))


if __name__ == "__main__":
    host = "0.0.0.0"
    port = 8080
    args = sys.argv[1:]
    if "--host" in args:
        host = args[args.index("--host") + 1]
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    server = ThreadingHTTPServer((host, port), Handler)
    print("Bundles on offer:")
    for p in bundles():
        print(f"  {p.name}  ({size_text(p.stat().st_size)})")
    print("\nOn the iPad (same Wi-Fi as this PC), open one of these in Safari:")
    for address in lan_addresses() or ["<this PC's IP address>"]:
        print(f"  http://{address}:{port}/")
    print("\nLeave this window open while downloading. Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
