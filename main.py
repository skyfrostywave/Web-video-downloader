from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import shutil
import socket
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.background import BackgroundTask

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
MAX_URL_LENGTH = 4096
PROCESS_TIMEOUT_SECONDS = 900
INFO_TIMEOUT_SECONDS = 90
FORMAT_ID_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,128}$")
LOG = logging.getLogger("cart3r-downloader")

app = FastAPI(
    title="CART3R Video Downloader",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
)

# Same-origin deployment is recommended. CORS is intentionally not opened to "*".
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class URLRequest(BaseModel):
    url: str = Field(min_length=8, max_length=MAX_URL_LENGTH)

    @field_validator("url")
    @classmethod
    def validate_url_syntax(cls, value: str) -> str:
        value = value.strip()
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("Enter a valid public http:// or https:// URL.")
        if parts.username or parts.password:
            raise ValueError("URLs containing embedded credentials are not allowed.")
        if parts.port not in (None, 80, 443):
            raise ValueError("Only standard HTTP and HTTPS ports are allowed.")
        return value


class DownloadRequest(URLRequest):
    format_id: str = Field(min_length=1, max_length=128)

    @field_validator("format_id")
    @classmethod
    def validate_format_id(cls, value: str) -> str:
        if not FORMAT_ID_RE.fullmatch(value):
            raise ValueError("Invalid format identifier.")
        return value


def _is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip.is_global


async def validate_public_destination(url: str) -> None:
    """Reject obvious local/private destinations before handing a URL to yt-dlp.

    Production deployments should additionally enforce outbound network/egress
    rules because DNS can change between validation and the downloader request.
    """
    host = urlsplit(url).hostname
    if not host:
        raise ValueError("URL has no hostname.")
    lowered = host.rstrip(".").lower()
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".localhost"):
        raise ValueError("Local network URLs are not allowed.")

    try:
        literal_ip = ipaddress.ip_address(lowered)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        if not literal_ip.is_global:
            raise ValueError("Private or reserved network addresses are not allowed.")
        return

    loop = asyncio.get_running_loop()
    try:
        records = await asyncio.wait_for(
            loop.getaddrinfo(lowered, None, type=socket.SOCK_STREAM),
            timeout=5,
        )
    except (OSError, asyncio.TimeoutError):
        raise ValueError("The URL hostname could not be resolved.") from None

    addresses = {record[4][0] for record in records}
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise ValueError("The URL resolves to a private or reserved network address.")


async def run_ytdlp(args: list[str], timeout: int) -> tuple[int, str, str]:
    command = ["yt-dlp", "--no-warnings", "--no-call-home", *args]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("yt-dlp is not installed. Install the requirements first.") from None
    except asyncio.TimeoutError:
        try:
            process.kill()
            await process.wait()
        except Exception:
            pass
        raise RuntimeError("The video service timed out. Try a shorter or smaller video.") from None

    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def clean_error(stderr: str, fallback: str) -> str:
    message = stderr.strip().splitlines()
    if not message:
        return fallback
    # Avoid returning local paths, command lines, or huge diagnostic dumps.
    result = message[-1][:400]
    lower = result.lower()
    if "private" in lower or "sign in" in lower or "login" in lower:
        return "This video is private or requires authentication."
    if "unsupported url" in lower or "no suitable extractor" in lower:
        return "This URL is not supported by yt-dlp."
    if "not available" in lower or "unavailable" in lower:
        return "This video is unavailable or has been removed."
    if "geo" in lower or "country" in lower:
        return "This video is not available in this server's region."
    return result


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "CART3R Video Downloader", "version": "1.0.0"}


@app.get("/")
async def home():
    if not INDEX_FILE.is_file():
        return JSONResponse(
            {"error": "Frontend file index.html was not found beside main.py."},
            status_code=500,
        )
    return FileResponse(INDEX_FILE, media_type="text/html")


@app.post("/api/info")
async def video_info(payload: URLRequest):
    try:
        await validate_public_destination(payload.url)
        code, stdout, stderr = await run_ytdlp(
            [
                "--dump-single-json",
                "--skip-download",
                "--no-playlist",
                "--",
                payload.url,
            ],
            INFO_TIMEOUT_SECONDS,
        )
        if code != 0:
            return JSONResponse(
                {"error": clean_error(stderr, "Could not retrieve video information.")},
                status_code=422,
            )

        try:
            info: dict[str, Any] = json.loads(stdout)
        except json.JSONDecodeError:
            LOG.warning("yt-dlp returned non-JSON output for /api/info")
            return JSONResponse(
                {"error": "The video service returned invalid metadata."},
                status_code=502,
            )

        formats = []
        for item in info.get("formats") or []:
            format_id = str(item.get("format_id") or "")
            if not FORMAT_ID_RE.fullmatch(format_id):
                continue

            width = item.get("width")
            height = item.get("height")
            resolution = item.get("resolution")
            if not resolution and height:
                resolution = f"{height}p"
            if not resolution and width and height:
                resolution = f"{width}x{height}"
            if not resolution:
                resolution = item.get("format_note") or "Unknown"

            size = item.get("filesize") or item.get("filesize_approx")
            formats.append(
                {
                    "format_id": format_id,
                    "ext": item.get("ext") or "unknown",
                    "resolution": str(resolution),
                    "width": width,
                    "height": height,
                    "vcodec": item.get("vcodec") or "unknown",
                    "acodec": item.get("acodec") or "unknown",
                    "filesize": size,
                    "filesize_approx": bool(item.get("filesize_approx") and not item.get("filesize")),
                    "format_note": item.get("format_note") or "",
                    "fps": item.get("fps"),
                    "has_video": item.get("vcodec") not in (None, "none"),
                    "has_audio": item.get("acodec") not in (None, "none"),
                }
            )

        # Some extractors expose a combined best format separately.
        if not formats and info.get("format_id"):
            formats.append(
                {
                    "format_id": str(info["format_id"]),
                    "ext": info.get("ext") or "unknown",
                    "resolution": info.get("resolution") or "Best available",
                    "width": info.get("width"),
                    "height": info.get("height"),
                    "vcodec": info.get("vcodec") or "unknown",
                    "acodec": info.get("acodec") or "unknown",
                    "filesize": info.get("filesize") or info.get("filesize_approx"),
                    "filesize_approx": bool(not info.get("filesize") and info.get("filesize_approx")),
                    "format_note": "",
                    "fps": info.get("fps"),
                    "has_video": info.get("vcodec") not in (None, "none"),
                    "has_audio": info.get("acodec") not in (None, "none"),
                }
            )

        formats.sort(
            key=lambda f: (
                not f["has_video"],
                -(f["height"] or 0),
                -(f["filesize"] or 0),
            )
        )
        return {
            "title": info.get("title") or "Untitled video",
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "extractor": info.get("extractor") or info.get("extractor_key") or "Video",
            "webpage_url": info.get("webpage_url") or payload.url,
            "formats": formats,
        }
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception:
        LOG.exception("Unexpected error in /api/info")
        return JSONResponse({"error": "An unexpected error occurred while reading this video."}, status_code=500)


@app.post("/api/download")
async def download_video(payload: DownloadRequest):
    work_dir = Path(tempfile.mkdtemp(prefix="cart3r-video-"))
    try:
        await validate_public_destination(payload.url)
        output_template = str(work_dir / "%(title).160B-%(id)s.%(ext)s")
        code, stdout, stderr = await run_ytdlp(
            [
                "--no-playlist",
                "--no-part",
                "--restrict-filenames",
                "--format",
                payload.format_id,
                "--output",
                output_template,
                "--print",
                "after_move:filepath",
                "--",
                payload.url,
            ],
            PROCESS_TIMEOUT_SECONDS,
        )
        if code != 0:
            shutil.rmtree(work_dir, ignore_errors=True)
            return JSONResponse(
                {"error": clean_error(stderr, "The video could not be downloaded in that format.")},
                status_code=422,
            )

        output_path = None
        for line in reversed(stdout.splitlines()):
            candidate = Path(line.strip())
            if candidate.is_file() and candidate.resolve().parent == work_dir.resolve():
                output_path = candidate
                break

        if output_path is None:
            files = [p for p in work_dir.iterdir() if p.is_file()]
            output_path = files[0] if len(files) == 1 else None

        if output_path is None or not output_path.is_file():
            shutil.rmtree(work_dir, ignore_errors=True)
            return JSONResponse(
                {"error": "The download completed but no output file was found."},
                status_code=502,
            )

        suffix = output_path.suffix.lower()
        media_type = {
            ".mp4": "video/mp4",
            ".webm": "video/webm",
            ".mkv": "video/x-matroska",
            ".mov": "video/quicktime",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
        }.get(suffix, "application/octet-stream")

        return FileResponse(
            path=output_path,
            media_type=media_type,
            filename=output_path.name,
            headers={"X-Content-Type-Options": "nosniff"},
            background=BackgroundTask(shutil.rmtree, work_dir, ignore_errors=True),
        )
    except ValueError as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        LOG.exception("Unexpected error in /api/download")
        return JSONResponse({"error": "An unexpected error occurred during download."}, status_code=500)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
