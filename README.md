# CART3R Video Downloader

FastAPI + yt-dlp backend and a vanilla HTML/CSS/JS frontend.

## Local setup

Python 3.10+ is recommended.

```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows PowerShell:
# .venv\\Scripts\\Activate.ps1

pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000.

One-line run command after installing dependencies:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## API

- `GET /api/health` — health check.
- `POST /api/info` — JSON body `{"url":"https://..."}`; returns video metadata and formats.
- `POST /api/download` — JSON body `{"url":"https://...","format_id":"..."}`; returns the selected file with `Content-Disposition: attachment`.

## GitHub

Commit the project files to a GitHub repository. GitHub is the source-control/editor platform; it does not run the FastAPI server by itself.

## Vercel

The repository includes `api/index.py` and `vercel.json` as a Vercel Python Function adapter. Import the repository into Vercel and deploy.

Important deployment limits:
- Vercel functions have plan-dependent execution duration, memory, temporary-storage, and response constraints. Large or long-running video downloads may fail or be cut off.
- The function needs yt-dlp and any required media tooling available in its runtime. The Python package is listed in `requirements.txt`; platform extractor behavior and native binary availability can vary.
- For reliable large-file downloads, deploy the same FastAPI app to a persistent Python host/container with adequate disk, bandwidth, and execution limits, and use Vercel for the frontend if desired.

## Security and usage notes

- The app rejects non-HTTP(S) URLs, embedded credentials, nonstandard ports, and hostnames resolving to non-public IP addresses.
- DNS validation is a best-effort SSRF guard, not a complete network sandbox. In production, enforce outbound egress restrictions at the hosting/network layer and consider request rate limits and authentication.
- Do not use this application to bypass DRM, access private content, or violate copyright, platform terms, or other people's rights.
- Keep yt-dlp updated: supported sites and extraction behavior change frequently.
- Downloads are written to a per-request temporary directory and removed after the response completes. If a process is forcibly terminated, the host may retain temporary files until its runtime cleans them up.
