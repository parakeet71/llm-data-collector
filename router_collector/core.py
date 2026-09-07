from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web
from multidict import CIMultiDict
from yarl import URL
from .storage import StorageBudget, StorageLimit, CompressedWriter

UPSTREAMS = {
    "anthropic": "https://api.anthropic.com",
    "glm": "https://api.z.ai/api/coding/paas/v4",
    "glm-api": "https://api.z.ai/api/paas/v4",
}
SESSION = web.AppKey("session", aiohttp.ClientSession)
DEFAULT_DATA = Path.home() / "datasets" / "router-collector"
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailer", "transfer-encoding", "upgrade", "host"}
SAFE_HEADERS = {"content-type", "content-length", "content-encoding", "request-id",
                "x-request-id", "retry-after", "anthropic-version"}


def now():
    return datetime.now(timezone.utc).isoformat()


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as f:
            os.chmod(temporary, 0o600)
            json.dump(value, f, indent=2, ensure_ascii=False)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_headers(headers):
    return {k.lower(): v for k, v in headers.items()
            if k.lower() in SAFE_HEADERS or k.lower().startswith(("anthropic-ratelimit-", "x-ratelimit-"))}


def forward_headers(headers):
    blocked = HOP | {v.strip().lower() for v in headers.get("Connection", "").split(",")}
    return CIMultiDict((k, v) for k, v in headers.items() if k.lower() not in blocked)


class Capture:
    def __init__(self, root, provider, request, run_id, budget):
        self.budget = budget
        self.disabled = False
        self.files = {}
        budget.check()
        self.id = str(uuid.uuid4())
        self.path = private_dir(root / "active" / self.id)
        self.metadata = {"schema_version": 2, "id": self.id, "run_id": run_id,
                         "provider": provider, "started_at": now(),
                         "method": request.method,
                         # Query strings and arbitrary paths can contain credentials.
                         "endpoint": request.path if request.path in {
                             "/v1/messages", "/v1/messages/count_tokens", "/v1/models",
                             "/chat/completions", "/models"} else "other",
                         "query_present": bool(request.query_string),
                         "request_headers": safe_headers(request.headers)}
        self.hashes = {key: hashlib.sha256() for key in ("request", "response")}
        self.sizes = {key: 0 for key in self.hashes}
        self.complete = {key: False for key in self.hashes}
        try:
            for key in self.hashes:
                self.files[key] = CompressedWriter(self.path / (key + ".bin.gz"), budget)
        except OSError:
            self.abort()
            raise

    def abort(self):
        self.disabled = True
        for f in self.files.values():
            f.abort()
        shutil.rmtree(self.path, ignore_errors=True)

    def append(self, key, chunk):
        if self.disabled:
            return
        self.files[key].write(chunk)
        self.hashes[key].update(chunk)
        self.sizes[key] += len(chunk)

    def close(self, root):
        if self.disabled:
            return
        for f in self.files.values():
            f.close()
        self.metadata["finished_at"] = now()
        self.metadata["bodies"] = {
            key: {"bytes": self.sizes[key], "sha256": self.hashes[key].hexdigest(),
                  "complete": self.complete[key], "file": key + ".bin.gz",
                  "encoding": "gzip", "stored_bytes": self.files[key].stored_bytes} for key in self.hashes}
        self.budget.reserve(len(json.dumps(self.metadata, indent=2, ensure_ascii=False).encode("utf-8")))
        write_json(self.path / "metadata.json", self.metadata)
        self.path.rename(private_dir(root / "records") / self.id)


def create_app(provider, data_dir, run_id=None, *, _upstream=None, storage_budget=None):
    """_upstream is dependency injection for offline tests, never a CLI option."""
    if provider not in UPSTREAMS:
        raise ValueError("Unknown provider")
    root = private_dir(Path(data_dir))
    budget = storage_budget or StorageBudget(root)
    warned = False

    def pause():
        nonlocal warned
        budget.paused = True
        if not warned:
            print("Recording paused: storage limit or disk write failure. Requests still forward. Free space and restart to resume.", file=sys.stderr)
            warned = True

    upstream = _upstream or UPSTREAMS[provider]
    app = web.Application(client_max_size=0, handler_args={"auto_decompress": False})

    async def lifecycle(app):
        async with aiohttp.ClientSession(auto_decompress=False,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=30),
                skip_auto_headers={"User-Agent", "Content-Type", "Accept-Encoding"}) as session:
            app[SESSION] = session
            yield
    app.cleanup_ctx.append(lifecycle)

    async def handle(request):
        try:
            capture = Capture(root, provider, request, run_id, budget)
        except OSError:
            pause()
            capture = None

        def append(key, chunk):
            if capture is not None and not capture.disabled:
                try:
                    capture.append(key, chunk)
                except OSError:
                    capture.abort()
                    pause()
        response = None
        async def body():
            async for chunk in request.content.iter_chunked(65536):
                append("request", chunk)
                yield chunk
            if capture is not None:
                capture.complete["request"] = True
        # Concatenation keeps the configured authority fixed, even for // paths.
        url = URL(upstream.rstrip("/") + "/" + request.raw_path.lstrip("/"), encoded=True)
        try:
            async with app[SESSION].request(request.method, url, headers=forward_headers(request.headers),
                    data=body(), allow_redirects=False) as remote:
                if capture is not None:
                    capture.metadata.update(status=remote.status, response_headers=safe_headers(remote.headers))
                response = web.StreamResponse(status=remote.status, headers=forward_headers(remote.headers))
                await response.prepare(request)
                async for chunk in remote.content.iter_chunked(65536):
                    append("response", chunk)
                    await response.write(chunk)
                if capture is not None:
                    capture.complete["response"] = True
                await response.write_eof()
                return response
        except asyncio.CancelledError:
            if capture is not None:
                capture.metadata["error"] = "cancelled"
            raise
        except Exception as exc:
            # Exception strings can include a URL, query, or credentials.
            if capture is not None:
                capture.metadata["error"] = type(exc).__name__
            if response is not None and response.prepared:
                if request.transport:
                    request.transport.close()
                return response
            return web.json_response({"error": "upstream_or_capture_failure", "record_id": capture.id if capture else None}, status=502)
        finally:
            if capture is not None:
                try:
                    capture.close(root)
                except OSError:
                    capture.abort()
                    pause()
    async def health(request):
        return web.json_response({"status": "ok", "provider": provider, "run_id": run_id, "version": "0.2.0", "storage": budget.status()})
    app.router.add_get("/health", health)
    app.router.add_route("*", "/{path:.*}", handle)
    return app


def export_records(root, destination):
    root, destination = Path(root), Path(destination)
    if destination.exists():
        raise ValueError("Export destination already exists")
    files = []
    for category in ("records", "outcomes", "events", "attachments"):
        folder = root / category
        if not folder.exists() or folder.is_symlink():
            continue
        if category == "records":
            for record in sorted(folder.iterdir()):
                if record.is_dir() and not record.is_symlink() and (record / "metadata.json").is_file():
                    files.extend(p for p in (record / "metadata.json", record / "request.bin", record / "response.bin", record / "request.bin.gz", record / "response.bin.gz") if p.is_file() and not p.is_symlink())
        else:
            files.extend(p for p in sorted(folder.rglob("*")) if p.is_file() and not p.is_symlink() and not p.name.endswith((".tmp", ".part")))
    manifest = {"schema_version": 1, "created_at": now(), "files": {}}
    created = False
    try:
        with destination.open("xb") as out:
            created = True
            os.chmod(destination, 0o600)
            with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for source in files:
                    name = source.relative_to(root).as_posix()
                    digest = hashlib.sha256()
                    info = zipfile.ZipInfo(name)
                    info.compress_type = zipfile.ZIP_STORED if source.suffix == ".gz" else zipfile.ZIP_DEFLATED
                    with source.open("rb") as src, archive.open(info, "w", force_zip64=True) as dst:
                        while chunk := src.read(65536):
                            digest.update(chunk)
                            dst.write(chunk)
                    manifest["files"][name] = digest.hexdigest()
                archive.writestr("manifest.json", json.dumps(manifest, indent=2))
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return len(files)
