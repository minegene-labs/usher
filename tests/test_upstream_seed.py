"""The upstream check's Jellyfin seeding waits for the setup calls and retries a 503."""

import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from tests import upstream_seed


def serve(not_ready_for):
    """A fake Jellyfin whose setup calls answer 503 for the first `not_ready_for` requests."""
    state = {"left": not_ready_for, "paths": []}

    class Handler(BaseHTTPRequestHandler):
        def respond(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            state["paths"].append(self.path)
            if self.path == "/Startup/Broken":
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path.startswith("/Startup") and state["left"] > 0:
                state["left"] -= 1
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps({}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = respond

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


class SeedJellyfinTests(unittest.TestCase):
    def start(self, not_ready_for):
        server, state = serve(not_ready_for)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}", state

    def test_waits_while_setup_calls_answer_503(self):
        base, state = self.start(3)
        with mock.patch.object(upstream_seed.time, "sleep"):
            upstream_seed.wait_jellyfin(base)
        self.assertEqual(state["left"], 0)

    def test_retries_a_503_on_a_setup_call(self):
        base, state = self.start(2)
        with mock.patch.object(upstream_seed.time, "sleep"):
            upstream_seed.setup_call(f"{base}/Startup/Complete", "POST")
        self.assertEqual(state["paths"].count("/Startup/Complete"), 3)

    def test_gives_up_with_a_clear_message(self):
        base, _ = self.start(10**6)
        with mock.patch.object(upstream_seed.time, "sleep"):
            with self.assertRaises(SystemExit) as caught:
                upstream_seed.wait_jellyfin(base, tries=3)
            self.assertIn("never got ready", str(caught.exception))
            with self.assertRaises(SystemExit) as caught:
                upstream_seed.setup_call(f"{base}/Startup/Complete", "POST", tries=3)
            self.assertIn("503", str(caught.exception))

    def test_another_error_is_not_retried(self):
        base, state = self.start(0)
        with self.assertRaises(urllib.error.HTTPError):
            upstream_seed.setup_call(f"{base}/Startup/Broken", "POST")
        self.assertEqual(state["paths"], ["/Startup/Broken"])


class MakeDownloadDirTests(unittest.TestCase):
    def test_creates_the_folder_transmission_reports(self):
        settings = {"TRANSMISSION_URL": "http://127.0.0.1:1/rpc", "TR_USER": "u", "TR_PASS": "p"}
        with (
            mock.patch.object(upstream_seed.Transmission, "call", return_value={"download-dir": "/data/torrents"}),
            mock.patch.object(upstream_seed.subprocess, "run") as run,
        ):
            upstream_seed.make_download_dir(settings)
        self.assertEqual(run.call_args.args[0], ["docker", "exec", "transmission", "mkdir", "-p", "/data/torrents"])


if __name__ == "__main__":
    unittest.main()
