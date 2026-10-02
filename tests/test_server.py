from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from replay_renderer.server import SessionState, make_handler


def analysis() -> dict:
    return {
        "meta": {"title": "A/B", "version": "Hard"},
        "timeline": {"startMs": 0, "endMs": 100},
        "notes": [],
        "inputs": [],
    }


class ServerExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = SessionState()
        self.state.analysis = analysis()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.state.close()

    def request(self, path: str, method: str = "GET", body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            response = urllib.request.urlopen(request, timeout=3)
            return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            result = error.code, error.read(), error.headers
            error.close()
            return result

    @staticmethod
    def options() -> dict:
        return {"showChartInfo": False, "showRealtimeJudgements": False, "trackWidth": 240, "trackHeight": 360, "fallSpeed": 600, "audioDelayMs": 0}

    def wait_for(self, job_id: str, state: str) -> dict:
        for _ in range(100):
            status, raw, _ = self.request(f"/api/export/status?id={job_id}")
            self.assertEqual(status, 200)
            body = json.loads(raw)
            if body["state"] == state:
                return body
            time.sleep(.01)
        self.fail(f"Export did not reach {state}")

    def test_complete_download_and_replace_old_result(self) -> None:
        def fake_render(payload, audio, mime, options, output, cancel, progress):
            progress(1, 1)
            output.write_bytes(b"video")
            return "silent"

        with patch("replay_renderer.server.render_export", fake_render):
            status, raw, _ = self.request("/api/export", "POST", self.options())
            self.assertEqual(status, 202)
            job_id = json.loads(raw)["id"]
            result = self.wait_for(job_id, "complete")
            self.assertEqual(result["warning"], "silent")
            status, raw, headers = self.request(f"/api/export/download?id={job_id}")
            self.assertEqual((status, raw), (200, b"video"))
            self.assertIn("A_B%20-%20Hard.mp4", headers["Content-Disposition"])

    def test_concurrent_rejection_and_cancel(self) -> None:
        started = threading.Event()

        def fake_render(payload, audio, mime, options, output, cancel, progress):
            started.set()
            progress(1, 2)
            cancel.wait(2)
            if cancel.is_set():
                raise InterruptedError

        with patch("replay_renderer.server.render_export", fake_render):
            status, raw, _ = self.request("/api/export", "POST", self.options())
            job_id = json.loads(raw)["id"]
            self.assertTrue(started.wait(1))
            status, _, _ = self.request("/api/export", "POST", self.options())
            self.assertEqual(status, 409)
            status, _, _ = self.request(f"/api/export/cancel?id={job_id}", "POST")
            self.assertEqual(status, 202)
            self.wait_for(job_id, "cancelled")


if __name__ == "__main__":
    unittest.main()
