"""Trusted child fixture: real HTTP worker protocol, synthetic I/O only."""

from http.cookiejar import Cookie
import json
import os
from pathlib import Path
import socket
import sys
from threading import Event


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in {"dns", "connect", "body", "success"}:
        return 2
    mode, temporary = sys.argv[1], Path(sys.argv[2]).resolve()
    allowed = Path(os.environ["LAZYREF_NATIVE_DOWNLOAD_ROOT"]).resolve()
    if temporary == allowed or not temporary.is_relative_to(allowed) or not temporary.is_dir():
        return 2
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

    def offline(event, args):
        if event.startswith("socket.") or event in {
            "subprocess.Popen", "os.system", "os.spawn", "os.exec", "sqlite3.connect"
        }:
            (temporary / "blocked-io.txt").write_text(event, encoding="utf-8")
            raise RuntimeError("Synthetic child blocked external operation: " + event)

    sys.addaudithook(offline)
    from lazyref import download_process, network

    def hang(stage):
        (temporary / "ready.json").write_text(json.dumps({"stage": stage, "pid": os.getpid()}),
                                               encoding="utf-8")
        Event().wait()
        raise AssertionError("A synthetic blocked operation unexpectedly returned")

    if mode == "dns":
        network.socket.getaddrinfo = lambda *args, **kwargs: hang("dns")
    elif mode == "connect":
        network.socket.getaddrinfo = lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 80))]

        class Socket:
            def __init__(self, *args, **kwargs):
                pass

            def settimeout(self, timeout):
                pass

            def connect(self, address):
                hang("connect")

            def close(self):
                pass

        network.socket.socket = Socket
    elif mode == "body":
        class Response:
            headers = {"content-length": "131072", "content-type": "image/png"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read1(self, size):
                hang("body")

            read = read1

        class Opener:
            def open(self, *args, **kwargs):
                return Response()

            def close(self):
                pass

        network.build_opener = lambda *args, **kwargs: Opener()
    else:
        requests = 0

        def synthetic_resource(self, url, **kwargs):
            nonlocal requests
            requests += 1
            previous = self.cookie_value("fixture-session", url)
            cookie = Cookie(version=0, name="fixture-session", value="anonymous", port=None,
                port_specified=False, domain="download.fixture.invalid", domain_specified=False,
                domain_initial_dot=False, path="/", path_specified=True, secure=False, expires=None,
                discard=True, comment=None, comment_url=None, rest={})
            self.cookies.set_cookie(cookie)
            return network.FetchedResource(url, f"{requests}:{previous}".encode(), "text/plain")

        network.Network._get_local = synthetic_resource
    download_process.worker()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
