from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import threading
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .analysis import analyze_files
from .chart import MAX_ARCHIVE_BYTES
from .errors import ReplayRendererError
from .export import ExportOptions, render_export, safe_video_filename
from .replay import MAX_REPLAY_BYTES


APP_VERSION = "5.1"
STATIC_DIR = Path(__file__).with_name("static")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
MAX_EXPORT_REQUEST_BYTES = 16 * 1024


@dataclass
class ExportJob:
    id: str
    output_path: Path
    filename: str
    state: str = "queued"
    rendered_frames: int = 0
    total_frames: int = 0
    error: str | None = None
    warning: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "renderedFrames": self.rendered_frames,
            "totalFrames": self.total_frames,
            "error": self.error,
            "warning": self.warning,
        }


class SessionState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.chart: bytes | None = None
        self.replay: bytes | None = None
        self.audio: bytes | None = None
        self.audio_mime: str | None = None
        self.analysis: dict | None = None
        self.export_dir = Path(tempfile.mkdtemp(prefix="malody-replay-export-"))
        self.export_job: ExportJob | None = None

    def exporting(self) -> bool:
        return self.export_job is not None and self.export_job.state in {"queued", "running"}

    def clear_export(self) -> None:
        if self.export_job is not None:
            self.export_job.output_path.unlink(missing_ok=True)
        self.export_job = None

    def close(self) -> None:
        with self.lock:
            if self.export_job is not None:
                self.export_job.cancel_event.set()
        shutil.rmtree(self.export_dir, ignore_errors=True)


def make_handler(state: SessionState):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"MalodyReplayRenderer/{APP_VERSION}"

        def _json(self, status: int, value: object) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, code: str, message: str) -> None:
            self._json(status, {"error": {"code": code, "message": message}})

        def _upload(self, kind: str, suffixes: tuple[str, ...], max_bytes: int) -> None:
            with state.lock:
                if state.exporting():
                    self._error(409, "export_in_progress", "Cannot replace files while a video export is running.")
                    return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._error(400, "invalid_content_length", "Invalid upload size.")
                return
            name = self.headers.get("X-Filename", "")
            if not name.lower().endswith(suffixes):
                expected = " or ".join(suffixes)
                self._error(400, "wrong_file_type", f"Expected a {expected} file.")
                return
            if length <= 0:
                self._error(413, "invalid_upload_size", "The uploaded file is empty.")
                return
            if length > max_bytes:
                limit_mib = max_bytes // (1024 * 1024)
                self._error(413, "invalid_upload_size", f"Upload exceeds the {limit_mib} MiB limit.")
                return
            body = self.rfile.read(length)
            if len(body) != length:
                self._error(400, "truncated_upload", "Upload ended before the declared size.")
                return
            with state.lock:
                setattr(state, kind, body)
                state.audio = None
                state.audio_mime = None
                state.analysis = None
                state.clear_export()
            self._json(200, {"ok": True, "name": name, "bytes": length})

        def _query_job(self) -> ExportJob | None:
            job_id = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            with state.lock:
                job = state.export_job
                if job is None or job.id != job_id:
                    self._error(404, "export_not_found", "Export task not found.")
                    return None
                return job

        def _read_json(self) -> object | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_EXPORT_REQUEST_BYTES:
                    raise ValueError
                return json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError):
                self._error(400, "invalid_json", "Expected a small JSON request body.")
                return None

        def _run_export(self, job: ExportJob, payload: dict, audio: bytes | None, audio_mime: str | None, options: ExportOptions) -> None:
            with state.lock:
                job.state = "running"

            def progress(done: int, total: int) -> None:
                with state.lock:
                    job.rendered_frames = done
                    job.total_frames = total

            try:
                warning = render_export(payload, audio, audio_mime, options, job.output_path, job.cancel_event, progress)
                with state.lock:
                    job.warning = warning
                    job.state = "cancelled" if job.cancel_event.is_set() else "complete"
            except InterruptedError:
                job.output_path.unlink(missing_ok=True)
                with state.lock:
                    job.state = "cancelled"
            except Exception as exc:
                job.output_path.unlink(missing_ok=True)
                print(f"Video export failed: {exc}")
                with state.lock:
                    job.state = "failed"
                    job.error = str(exc)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/chart":
                self._upload("chart", (".mcz", ".osz"), MAX_ARCHIVE_BYTES)
                return
            if path == "/api/replay":
                self._upload("replay", (".mrv",), MAX_REPLAY_BYTES)
                return
            if path == "/api/analyze":
                with state.lock:
                    chart, replay = state.chart, state.replay
                if chart is None or replay is None:
                    self._error(
                        400,
                        "missing_upload",
                        "Select a .mcz/.osz chart package and a .mrv replay.",
                    )
                    return
                try:
                    result, audio, audio_mime = analyze_files(chart, replay)
                except ReplayRendererError as exc:
                    self._error(422, exc.code, str(exc))
                    return
                except Exception:
                    self._error(500, "internal_error", "Unexpected parser error; see the terminal for details.")
                    raise
                with state.lock:
                    state.audio = audio
                    state.audio_mime = audio_mime
                    state.analysis = result
                self._json(200, result)
                return
            if path == "/api/export":
                body = self._read_json()
                if body is None:
                    return
                try:
                    options = ExportOptions.from_json(body)
                except ReplayRendererError as exc:
                    self._error(400, exc.code, str(exc))
                    return
                with state.lock:
                    if state.exporting():
                        self._error(409, "export_in_progress", "Another video export is already running.")
                        return
                    if state.analysis is None:
                        self._error(400, "missing_analysis", "Analyze a chart and replay before exporting.")
                        return
                    state.clear_export()
                    job_id = uuid.uuid4().hex
                    filename = safe_video_filename(state.analysis["meta"]["title"], state.analysis["meta"]["version"])
                    job = ExportJob(job_id, state.export_dir / f"{job_id}.mp4", filename)
                    state.export_job = job
                    payload, audio, audio_mime = state.analysis, state.audio, state.audio_mime
                thread = threading.Thread(
                    target=self._run_export,
                    args=(job, payload, audio, audio_mime, options),
                    name=f"video-export-{job.id[:8]}",
                    daemon=True,
                )
                thread.start()
                self._json(202, {"id": job.id, "state": job.state})
                return
            if path == "/api/export/cancel":
                job = self._query_job()
                if job is None:
                    return
                with state.lock:
                    if job.state not in {"queued", "running"}:
                        self._error(409, "export_not_running", "Export task is not running.")
                        return
                    job.cancel_event.set()
                self._json(202, job.public())
                return
            self._error(404, "not_found", "Unknown endpoint.")

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/version":
                self._json(200, {"version": APP_VERSION})
                return
            if path == "/api/export/status":
                job = self._query_job()
                if job is not None:
                    with state.lock:
                        self._json(200, job.public())
                return
            if path == "/api/export/download":
                job = self._query_job()
                if job is None:
                    return
                with state.lock:
                    if job.state != "complete" or not job.output_path.is_file():
                        self._error(409, "export_not_complete", "Export is not ready for download.")
                        return
                    output_path, filename = job.output_path, job.filename
                size = output_path.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with output_path.open("rb") as handle:
                    shutil.copyfileobj(handle, self.wfile)
                return
            if path == "/api/audio":
                with state.lock:
                    audio, mime = state.audio, state.audio_mime
                if audio is None:
                    self._error(404, "missing_audio", "The selected chart has no available audio.")
                    return
                start, end = 0, len(audio) - 1
                range_header = self.headers.get("Range")
                if range_header:
                    try:
                        unit, requested = range_header.split("=", 1)
                        first, last = requested.split("-", 1)
                        if unit != "bytes" or "," in requested:
                            raise ValueError
                        if first:
                            start = int(first)
                            end = int(last) if last else end
                        else:
                            suffix_length = int(last)
                            start = max(0, len(audio) - suffix_length)
                        end = min(end, len(audio) - 1)
                        if start < 0 or start > end:
                            raise ValueError
                    except ValueError:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(audio)}")
                        self.end_headers()
                        return
                body = audio[start : end + 1]
                self.send_response(206 if range_header else 200)
                self.send_header("Content-Type", mime or "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Accept-Ranges", "bytes")
                if range_header:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(audio)}")
                self.end_headers()
                self.wfile.write(body)
                return

            relative = "index.html" if path == "/" else path.lstrip("/")
            if relative not in {"index.html", "style.css", "app.js", "third-party.html"}:
                self._error(404, "not_found", "File not found.")
                return
            body = (STATIC_DIR / relative).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES[Path(relative).suffix])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            print(f"[{self.log_date_time_string()}] {format % args}")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Malody V replay track player")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    state = SessionState()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state))
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Malody replay renderer is running at {url}")
    print("Press Ctrl+C to stop. Uploads stay in memory; video exports use a temporary directory.")
    if not args.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()
        state.close()
