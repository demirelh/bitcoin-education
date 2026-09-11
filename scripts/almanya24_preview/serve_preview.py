from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

if __package__:
    from .paths import data_root
else:  # executed directly, e.g. by the systemd unit
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from paths import data_root

ROOT = data_root()
SITE = ROOT / "site" / "current"
SCREENSHOTS = (ROOT / "screenshots").resolve()


class PreviewHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE), **kwargs)

    def translate_path(self, path: str) -> str:
        request_path = unquote(urlsplit(path).path)
        prefix = "/_screenshots/"
        if request_path.startswith(prefix):
            name = request_path.removeprefix(prefix)
            if "/" in name or name not in {
                "start-desktop.png",
                "start-mobile.png",
                "article-desktop.png",
                "article-mobile.png",
            }:
                return str(SCREENSHOTS / "__not_found__")
            return str(SCREENSHOTS / name)
        return super().translate_path(path)

    def list_directory(self, path):
        self.send_error(404)
        return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), PreviewHandler)
    print(f"ALMANYA24 preview: http://127.0.0.1:{args.port}/", flush=True)
    server.serve_forever()
