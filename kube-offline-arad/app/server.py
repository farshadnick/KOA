from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from typing import Annotated

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from downloader import (
    ensure_dirs,
    get_download_preview,
    prepare_download_list,
    push_saved_images,
    registry_login,
    run_full_download,
    write_generated_offline_yml,
)
from job_state import JobStatus, job
from proxy_manager import (
    proxy_operation_lock,
    restart_v2ray,
    wait_for_proxy,
    write_normalized_config,
)
from settings import settings

app = FastAPI(title="Kube Offline Arad", version="1.0.0")

_worker_lock = threading.Lock()


class StartRequest(BaseModel):
    kubespray_version: str | None = None
    push_images: bool = True
    save_image_tars: bool = True
    push_to_registry: bool = True
    skip_os_repos: bool = False
    skip_pypi: bool = False


class PrepareRequest(BaseModel):
    kubespray_version: str | None = None


class KubesprayConfigRequest(BaseModel):
    http_public_url: str
    registry_public_host: str


class PushRequest(BaseModel):
    """Push saved images to the local registry or another registry."""

    registry_host: str | None = None
    registry_project: str | None = None
    username: str | None = None
    password: str | None = None
    tls_verify: bool = False


def _start_job(fn, *args, job_type: str | None = None, **kwargs) -> None:
    if not _worker_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A job is already running")

    def _run() -> None:
        try:
            job.reset(job_type or fn.__name__)
            fn(*args, **kwargs)
            job.finish(True, job.snapshot()["message"] or "Completed successfully")
        except Exception as exc:  # noqa: BLE001
            job.log(f"FATAL: {exc}")
            job.finish(False, str(exc))
        finally:
            _worker_lock.release()

    threading.Thread(target=_run, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


@app.get("/api/health")
def health() -> dict:
    v2ray = Path("/config/v2ray.json")
    return {
        "ok": True,
        "v2ray_config_present": v2ray.exists(),
        "kubespray_version": settings.kubespray_version,
        "registry_public_host": settings.registry_public_host,
        "http_public_url": settings.http_public_url,
        "offline_config_url": f"{settings.http_public_url.rstrip('/')}/offline.yml",
        "outputs_dir": str(settings.outputs_dir),
    }


@app.get("/api/status")
def status() -> dict:
    return job.snapshot()


@app.get("/api/jobs")
def list_jobs(limit: int = 50) -> dict:
    records = job.history(limit)
    current = job.snapshot()
    if current["status"] == JobStatus.running.value:
        records = [current, *records]
    return {
        "jobs": [
            {key: value for key, value in record.items() if key != "log_tail"}
            for record in records
        ]
    }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    current = job.snapshot()
    if current["id"] == job_id:
        return current
    record = job.history_job(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return record


@app.post("/api/v2ray")
async def upload_v2ray(file: UploadFile = File(...)) -> dict:
    if job.status == JobStatus.running or _worker_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="Cannot replace V2Ray config while a download job is running",
        )
    dest = settings.config_dir / "v2ray.json"
    content = await file.read()
    if not content.strip():
        raise HTTPException(status_code=400, detail="Empty config")
    if not proxy_operation_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="Another V2Ray upload or readiness check is already running",
        )
    try:
        try:
            write_normalized_config(dest, content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        job.log(
            f"Wrote normalized V2Ray config to {dest}; restarting "
            f"{settings.v2ray_container}"
        )
        try:
            await asyncio.to_thread(restart_v2ray, settings.v2ray_container)
            await asyncio.to_thread(
                wait_for_proxy,
                proxy_url=settings.http_proxy,
                test_url=settings.proxy_test_url,
                attempts=settings.proxy_ready_attempts,
                delay_seconds=settings.proxy_ready_delay_seconds,
                log=job.log,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "ok": True,
            "path": str(dest),
            "hint": "V2Ray restarted and the tunneled HTTPS check passed",
        }
    finally:
        proxy_operation_lock.release()


@app.post("/api/download")
def start_download(body: StartRequest) -> dict:
    if job.status == JobStatus.running:
        raise HTTPException(status_code=409, detail="Job already running")
    version = body.kubespray_version or settings.kubespray_version
    preview = get_download_preview()
    if not preview["prepared"] or preview["version"] != version.lstrip("v"):
        raise HTTPException(
            status_code=409,
            detail="Prepare and review the download list for this Kubespray version first",
        )
    _start_job(
        run_full_download,
        kubespray_version=version,
        push_images=body.push_images,
        save_image_tars=body.save_image_tars,
        push_to_registry=body.push_to_registry,
        skip_os_repos=body.skip_os_repos,
        skip_pypi=body.skip_pypi,
        job_type="download-all",
    )
    return {
        "ok": True,
        "message": (
            f"download-all started for Kubespray {version}; outbound via V2Ray, "
            f"nginx outputs enabled, registry={body.push_to_registry}"
        ),
    }


@app.post("/api/prepare")
def start_prepare(body: PrepareRequest) -> dict:
    if job.status == JobStatus.running:
        raise HTTPException(status_code=409, detail="Job already running")
    version = body.kubespray_version or settings.kubespray_version
    _start_job(
        prepare_download_list,
        kubespray_version=version,
        job_type="prepare-list",
    )
    return {
        "ok": True,
        "message": f"Preparing download list for Kubespray {version}",
    }


@app.get("/api/preview")
def download_preview() -> dict:
    return get_download_preview()


def _push_fields(req: PushRequest) -> tuple[str | None, str | None, str | None, str | None]:
    registry_host = (req.registry_host or "").strip() or None
    registry_project = (
        req.registry_project.strip()
        if req.registry_project and req.registry_project.strip()
        else None
    )
    username = (req.username or "").strip() or None
    password = req.password if username else None
    return registry_host, registry_project, username, password


@app.post("/api/registry/login")
def api_registry_login(
    body: Annotated[PushRequest | None, Body()] = None,
) -> dict:
    """Test skopeo login against the given registry (does not start a push job)."""
    if job.status == JobStatus.running:
        raise HTTPException(status_code=409, detail="Job already running")
    req = body or PushRequest()
    registry_host, _, username, password = _push_fields(req)
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    try:
        server = registry_login(
            registry_host,
            username=username,
            password=password or "",
            tls_verify=req.tls_verify,
            quiet=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {"ok": True, "message": f"Logged in to {server}", "server": server}


@app.post("/api/push")
def start_push(
    body: Annotated[PushRequest | None, Body()] = None,
) -> dict:
    if job.status == JobStatus.running:
        raise HTTPException(status_code=409, detail="Job already running")
    req = body or PushRequest()
    registry_host, registry_project, username, password = _push_fields(req)
    if registry_host and not username:
        raise HTTPException(
            status_code=400,
            detail="Username is required when pushing to an external registry",
        )
    if registry_host:
        proj = registry_project or settings.registry_project
        target = (
            registry_host
            if "/" in registry_host.rstrip("/")
            else f"{registry_host.rstrip('/')}/{proj}"
            if proj
            else registry_host
        )
    else:
        target = "registry:5000 (local)"
    _start_job(
        push_saved_images,
        registry_host=registry_host,
        registry_project=registry_project,
        username=username,
        password=password,
        tls_verify=req.tls_verify,
        job_type="push-images",
    )
    return {
        "ok": True,
        "message": f"Push all images to {target} started",
    }


@app.get("/api/artifacts")
def list_artifacts() -> dict:
    out = settings.outputs_dir
    def _count(p: Path) -> int:
        if not p.exists():
            return 0
        return sum(1 for x in p.rglob("*") if x.is_file())

    return {
        "files": _count(out / "files"),
        "images": _count(out / "images"),
        "charts": _count(out / "charts"),
        "pypi": _count(out / "pypi"),
        "debs": _count(out / "debs"),
        "rpms": _count(out / "rpms"),
        "kubespray": _count(out / "kubespray"),
        "scripts": _count(out / "scripts"),
        "paths": {
            "outputs": str(out),
            "http": settings.http_public_url,
            "registry": settings.registry_public_host,
        },
    }


@app.get("/api/kubespray-config", response_class=PlainTextResponse)
def kubespray_config(download: bool = False) -> PlainTextResponse:
    path = settings.outputs_dir / "offline.yml"
    if not path.exists():
        ensure_dirs()
        write_generated_offline_yml()
    headers = (
        {"Content-Disposition": 'attachment; filename="offline.yml"'}
        if download
        else None
    )
    return PlainTextResponse(path.read_text(encoding="utf-8"), headers=headers)


@app.post("/api/kubespray-config", response_class=PlainTextResponse)
def generate_kubespray_config(body: KubesprayConfigRequest) -> PlainTextResponse:
    http_url = body.http_public_url.strip().rstrip("/")
    registry_host = body.registry_public_host.strip()
    if not http_url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="HTTP server URL must start with http:// or https://",
        )
    if not registry_host or "://" in registry_host:
        raise HTTPException(
            status_code=400,
            detail="Registry must use HOST:PORT format without a URL scheme",
        )
    settings.http_public_url = http_url
    settings.registry_public_host = registry_host
    ensure_dirs()
    write_generated_offline_yml()
    path = settings.outputs_dir / "offline.yml"
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@app.post("/api/config/version")
def set_version(kubespray_version: str = Form(...)) -> dict:
    settings.kubespray_version = kubespray_version.lstrip("v")
    env_path = settings.config_dir / "app.env"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
        out = []
        replaced = False
        for line in lines:
            if line.startswith("KUBESPRAY_VERSION="):
                out.append(f"KUBESPRAY_VERSION={settings.kubespray_version}")
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.append(f"KUBESPRAY_VERSION={settings.kubespray_version}")
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return {"ok": True, "kubespray_version": settings.kubespray_version}


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Kube Offline Arad</title>
  <style>
    :root {
      --bg: #0f1419;
      --panel: #1a2332;
      --text: #e7ecf3;
      --muted: #8b9bb4;
      --accent: #3d9a6a;
      --accent2: #c4a35a;
      --danger: #c45c5c;
      --border: #2a3648;
      --mono: "IBM Plex Mono", "SF Mono", Menlo, monospace;
      --sans: "IBM Plex Sans", "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: var(--sans);
      color: var(--text);
      background:
        radial-gradient(ellipse 80% 50% at 10% -10%, #1e3a2f 0%, transparent 55%),
        radial-gradient(ellipse 60% 40% at 100% 0%, #2a2438 0%, transparent 50%),
        var(--bg);
    }
    .wrap { max-width: 960px; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }
    h1 {
      font-size: clamp(1.8rem, 4vw, 2.6rem);
      font-weight: 600;
      letter-spacing: -0.03em;
      margin: 0 0 0.35rem;
    }
    .tagline { color: var(--muted); margin: 0 0 2rem; line-height: 1.5; }
    .grid { display: grid; gap: 1rem; }
    @media (min-width: 800px) {
      .grid.two { grid-template-columns: 1fr 1fr; }
    }
    section {
      background: color-mix(in srgb, var(--panel) 92%, transparent);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1.25rem 1.35rem;
      backdrop-filter: blur(6px);
    }
    section h2 {
      margin: 0 0 0.85rem;
      font-size: 0.95rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--accent2);
      font-weight: 600;
    }
    label { display: block; font-size: 0.85rem; color: var(--muted); margin-bottom: 0.35rem; }
    input[type="text"], input[type="password"], input[type="file"] {
      width: 100%;
      background: #0c1118;
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 8px;
      padding: 0.65rem 0.75rem;
      font-family: var(--mono);
      font-size: 0.9rem;
      margin-bottom: 0.85rem;
    }
    .row { display: flex; flex-wrap: wrap; gap: 0.6rem; align-items: center; }
    .checks { display: flex; flex-direction: column; gap: 0.4rem; margin-bottom: 0.9rem; color: var(--muted); font-size: 0.9rem; }
    button {
      appearance: none;
      border: none;
      border-radius: 8px;
      padding: 0.7rem 1.1rem;
      font-weight: 600;
      font-size: 0.92rem;
      cursor: pointer;
      background: var(--accent);
      color: #04140c;
      transition: transform .15s ease, filter .15s ease;
    }
    button:hover { filter: brightness(1.08); transform: translateY(-1px); }
    button.secondary { background: transparent; color: var(--text); border: 1px solid var(--border); }
    button:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
    .meta { font-family: var(--mono); font-size: 0.8rem; color: var(--muted); line-height: 1.6; }
    .meta strong { color: var(--text); font-weight: 500; }
    .status-pill {
      display: inline-block;
      padding: 0.2rem 0.55rem;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      background: #243044;
    }
    .status-pill.running { background: #2a4a6a; color: #9fd0ff; }
    .status-pill.success { background: #1d3d2c; color: #7ddea8; }
    .status-pill.failed { background: #4a2222; color: #ffb0b0; }
    pre {
      margin: 0;
      max-height: 340px;
      overflow: auto;
      background: #0c1118;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.85rem;
      font-family: var(--mono);
      font-size: 0.78rem;
      line-height: 1.45;
      white-space: pre-wrap;
    }
    .hint { font-size: 0.82rem; color: var(--muted); margin-top: 0.75rem; line-height: 1.45; }
    .menu { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 1rem; }
    .menu button { background: transparent; color: var(--muted); border: 1px solid var(--border); }
    .menu button.active { background: var(--accent); color: #04140c; border-color: var(--accent); }
    .page { display: none; }
    .page.active { display: block; }
    .preview-list { max-height: 430px; overflow: auto; display: grid; gap: 0.45rem; }
    .preview-item {
      padding: 0.65rem 0.75rem;
      border: 1px solid var(--border);
      border-radius: 7px;
      background: #0c1118;
      font-family: var(--mono);
      font-size: 0.76rem;
      overflow-wrap: anywhere;
    }
    .preview-item.cached { border-left: 3px solid var(--accent); }
    .preview-item.pending { border-left: 3px solid var(--accent2); }
    .preview-dest { color: var(--muted); margin-top: 0.25rem; }
    a { color: #7eb8ff; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Kube Offline Arad</h1>
    <p class="tagline">
      Full offline packager (like kubespray-offline <code>download-all.sh</code>):
      Kubespray files, container images, PyPI mirror, Ubuntu apt repo, Helm charts —
      downloaded through V2Ray and served by nginx plus Docker Registry v2.
    </p>

    <nav class="menu">
      <button class="active" data-page="downloads" onclick="showPage('downloads')">Downloads</button>
      <button data-page="artifacts" onclick="showPage('artifacts')">Artifacts</button>
      <button data-page="kubespray" onclick="showPage('kubespray')">Kubespray config</button>
      <button data-page="proxy" onclick="showPage('proxy')">V2Ray</button>
      <button data-page="job" onclick="showPage('job')">Job & logs</button>
    </nav>

    <div class="page active" id="page-downloads">
      <div class="grid two">
        <section>
          <h2>Download settings</h2>
          <label for="version">Kubespray version</label>
          <input id="version" type="text" value="2.27.0" />
          <div class="checks">
            <label><input id="pushReg" type="checkbox" checked /> Push images → Docker registry</label>
            <label><input id="tars" type="checkbox" checked /> Save image tarballs</label>
            <label><input id="pypi" type="checkbox" checked /> Build PyPI mirror</label>
            <label><input id="osrepo" type="checkbox" checked /> Build Ubuntu apt repo</label>
          </div>
          <div class="row">
            <button id="btnPrepare" onclick="prepareList()">1. Prepare list</button>
            <button id="btnDownload" onclick="startDownload()" disabled>2. Start download</button>
          </div>
          <p class="hint">Prepare first, review every source and destination below, then start downloading.</p>
        </section>
        <section>
          <h2>Manifest summary</h2>
          <div class="meta" id="previewSummary">No list prepared.</div>
          <div class="row" style="margin-top:0.8rem">
            <button class="secondary" onclick="showPreviewType('files')">Files</button>
            <button class="secondary" onclick="showPreviewType('images')">Images</button>
            <button class="secondary" onclick="showPreviewType('charts')">Helm charts</button>
          </div>
        </section>
      </div>
      <section style="margin-top:1rem">
        <h2>Push images to registry</h2>
        <label for="pushRegistry">Registry address</label>
        <input id="pushRegistry" type="text" placeholder="hub.aradarpanet.ir (empty = local registry:5000)" autocomplete="off" />
        <div class="grid two">
          <div>
            <label for="pushProject">Harbor project</label>
            <input id="pushProject" type="text" value="local" placeholder="local" autocomplete="off" />
          </div>
          <div>
            <label for="pushUser">Username</label>
            <input id="pushUser" type="text" placeholder="optional" autocomplete="username" />
          </div>
        </div>
        <div class="grid two">
          <div>
            <label for="pushPass">Password</label>
            <input id="pushPass" type="password" placeholder="optional" autocomplete="current-password" />
          </div>
          <div></div>
        </div>
        <div class="checks">
          <label><input id="pushTls" type="checkbox" /> Verify TLS certificate</label>
        </div>
        <div class="row">
          <button class="secondary" id="btnLogin" onclick="registryLogin()">Login to registry</button>
          <button id="btnPush" onclick="startPush()">Push all images</button>
        </div>
        <p class="hint" id="loginHint">
          Uses <code>skopeo login</code> before push. External registries get
          <code>&lt;host&gt;/&lt;project&gt;/…</code> (default project <code>local</code>).
          Leave the address empty for the local Compose registry.
        </p>
      </section>
      <section style="margin-top:1rem">
        <h2 id="previewTitle">Download list</h2>
        <div class="preview-list" id="previewList">Prepare a list to preview downloads.</div>
      </section>
    </div>

    <div class="page" id="page-artifacts">
      <section>
        <h2>Published artifacts</h2>
        <div class="meta" id="health">Loading…</div>
      </section>
    </div>

    <div class="page" id="page-kubespray">
      <section>
        <h2>Kubespray offline.yml</h2>
        <label for="configHttp">Nginx URL reachable from Kubernetes nodes</label>
        <input id="configHttp" type="text" placeholder="http://192.168.1.10:8080" />
        <label for="configRegistry">Docker Registry reachable from Kubernetes nodes</label>
        <input id="configRegistry" type="text" placeholder="192.168.1.10:35000" />
        <div class="row">
          <button onclick="generateKubesprayConfig()">Generate config</button>
          <button class="secondary" onclick="copyKubesprayConfig()">Copy</button>
          <button class="secondary" onclick="downloadKubesprayConfig()">Download offline.yml</button>
        </div>
        <p class="hint">Copy this file to <code>inventory/&lt;cluster&gt;/group_vars/all/offline.yml</code> before running Kubespray.</p>
        <pre id="kubesprayConfig" style="margin-top:0.8rem">Loading…</pre>
      </section>
    </div>

    <div class="page" id="page-proxy">
      <section>
        <h2>V2Ray config</h2>
        <label for="v2ray">Upload config.json</label>
        <input id="v2ray" type="file" accept=".json,application/json" />
        <div class="row">
          <button id="btnV2ray" onclick="uploadV2ray()">Upload and test</button>
          <button class="secondary" type="button" onclick="refreshHealth()">Refresh status</button>
        </div>
        <p class="hint">Upload validates the JSON, adds the internal HTTP/SOCKS listeners, restarts V2Ray, and tests the tunnel.</p>
      </section>
    </div>

    <div class="page" id="page-job">
      <section>
        <h2>Job status and logs</h2>
        <div class="meta" style="margin-bottom:0.75rem">
          Status: <span class="status-pill" id="pill">idle</span>
          &nbsp; Phase: <strong id="phase">—</strong><br />
          <span id="jobIdentity"></span><br />
          <span id="message"></span><br />
          <span id="timing"></span><br />
          <span id="stats"></span>
        </div>
        <pre id="log">Waiting for job…</pre>
      </section>
      <section style="margin-top:1rem">
        <h2>Job history</h2>
        <div class="row" style="margin-bottom:0.8rem">
          <button class="secondary" onclick="refreshJobHistory()">Refresh history</button>
        </div>
        <div class="preview-list" id="jobHistory">No completed jobs yet.</div>
        <pre id="historyLog" style="margin-top:0.8rem;display:none"></pre>
      </section>
    </div>
  </div>
  <script>
    let previewData = null;
    let previewType = 'files';
    let jobBusy = false;
    let wasBusy = false;
    let lastPhase = '';

    function showPage(name) {
      document.querySelectorAll('.page').forEach(el => el.classList.toggle('active', el.id === `page-${name}`));
      document.querySelectorAll('.menu button').forEach(el => el.classList.toggle('active', el.dataset.page === name));
      if (name === 'kubespray') loadKubesprayConfig();
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
      })[ch]);
    }

    function formatBytes(value) {
      if (!value) return '—';
      const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
      let size = Number(value);
      let unit = 0;
      while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
      return `${size.toFixed(1)} ${units[unit]}`;
    }

    function showPreviewType(type) {
      previewType = type;
      renderPreview();
    }

    function renderPreview() {
      const list = document.getElementById('previewList');
      const title = document.getElementById('previewTitle');
      title.textContent = previewType === 'charts' ? 'Helm chart list' : `${previewType[0].toUpperCase()}${previewType.slice(1)} list`;
      if (!previewData || !previewData.prepared) {
        list.textContent = 'Prepare a list to preview downloads.';
        return;
      }
      const items = previewData[previewType] || [];
      if (!items.length) {
        list.textContent = `No ${previewType} configured.`;
        return;
      }
      list.innerHTML = items.map((item, index) => `
        <div class="preview-item ${item.cached ? 'cached' : 'pending'}">
          <strong>${index + 1}. ${item.cached ? 'CACHED' : 'PENDING'}</strong>
          · ${item.cached ? formatBytes(item.size) : 'will download'}<br/>
          ${escapeHtml(item.source)}
          <div class="preview-dest">→ ${escapeHtml(item.destination)}</div>
        </div>
      `).join('');
    }

    async function refreshPreview() {
      previewData = await (await fetch('/api/preview')).json();
      const cached = type => (previewData[type] || []).filter(item => item.cached).length;
      document.getElementById('previewSummary').innerHTML = previewData.prepared
        ? `Kubespray <strong>${escapeHtml(previewData.version)}</strong><br/>` +
          `Files: <strong>${previewData.files.length}</strong> (${cached('files')} cached)<br/>` +
          `Images: <strong>${previewData.images.length}</strong> (${cached('images')} cached)<br/>` +
          `Helm charts: <strong>${previewData.charts.length}</strong> (${cached('charts')} cached)`
        : 'No list prepared.';
      const version = document.getElementById('version').value.replace(/^v/, '');
      document.getElementById('btnDownload').disabled =
        jobBusy || !previewData.prepared || previewData.version !== version;
      renderPreview();
    }

    async function refreshHealth() {
      const r = await fetch('/api/health');
      const h = await r.json();
      const a = await (await fetch('/api/artifacts')).json();
      document.getElementById('health').innerHTML = `
        V2Ray: <strong>${h.v2ray_config_present ? 'present' : 'missing'}</strong> ·
        Kubespray: <strong>${h.kubespray_version}</strong><br/>
        Registry: <strong>${h.registry_public_host}</strong><br/>
        HTTP: <strong><a href="${h.http_public_url}" target="_blank">${h.http_public_url}</a></strong><br/>
        Kubespray config: <strong><a href="${h.offline_config_url}" target="_blank">offline.yml</a></strong><br/>
        Artifacts — files:${a.files} images:${a.images} pypi:${a.pypi} debs:${a.debs} charts:${a.charts}
      `;
      document.getElementById('version').value = h.kubespray_version;
      const browserHost = window.location.hostname;
      const httpInput = document.getElementById('configHttp');
      const registryInput = document.getElementById('configRegistry');
      if (!httpInput.value) {
        httpInput.value = h.http_public_url.includes('localhost')
          ? `http://${browserHost}:8080`
          : h.http_public_url;
      }
      if (!registryInput.value) {
        registryInput.value = h.registry_public_host.startsWith('localhost:')
          ? `${browserHost}:35000`
          : h.registry_public_host;
      }
    }

    async function refreshStatus() {
      const s = await (await fetch('/api/status')).json();
      const pill = document.getElementById('pill');
      pill.textContent = s.status;
      pill.className = 'status-pill ' + s.status;
      document.getElementById('phase').textContent = s.phase || '—';
      document.getElementById('jobIdentity').textContent =
        s.id ? `Job: ${s.id} · Type: ${s.type || 'job'}` : '';
      document.getElementById('message').textContent = s.message || '';
      document.getElementById('timing').textContent =
        `Started: ${s.started_at || '—'} · Finished: ${s.finished_at || '—'}`;
      document.getElementById('stats').textContent =
        'Progress: ' + (Object.keys(s.stats || {}).length ? JSON.stringify(s.stats) : '—');
      document.getElementById('log').textContent = (s.log_tail || []).join(String.fromCharCode(10)) || 'Waiting for job…';
      const busy = s.status === 'running';
      jobBusy = busy;
      ['btnPrepare','btnPush'].forEach(id => { const el = document.getElementById(id); if (el) el.disabled = busy; });
      if (s.phase === 'ready' && lastPhase !== 'ready') refreshPreview();
      if (wasBusy && !busy) {
        refreshPreview();
        refreshJobHistory();
      }
      wasBusy = busy;
      lastPhase = s.phase;
      const version = document.getElementById('version').value.replace(/^v/, '');
      document.getElementById('btnDownload').disabled =
        busy || !previewData || !previewData.prepared || previewData.version !== version;
    }

    async function uploadV2ray() {
      const f = document.getElementById('v2ray').files[0];
      if (!f) { alert('Choose a v2ray json file'); return; }
      const button = document.getElementById('btnV2ray');
      button.disabled = true;
      const fd = new FormData();
      fd.append('file', f);
      try {
        const r = await fetch('/api/v2ray', { method: 'POST', body: fd });
        const j = await r.json();
        if (!r.ok) { alert(j.detail || 'Upload failed'); return; }
        alert(j.hint || 'Uploaded');
        refreshHealth();
      } finally {
        button.disabled = false;
      }
    }

    async function loadKubesprayConfig() {
      const response = await fetch('/api/kubespray-config');
      const content = await response.text();
      document.getElementById('kubesprayConfig').textContent =
        response.ok ? content : `Failed: ${content}`;
    }

    async function generateKubesprayConfig() {
      const response = await fetch('/api/kubespray-config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          http_public_url: document.getElementById('configHttp').value,
          registry_public_host: document.getElementById('configRegistry').value,
        }),
      });
      const content = await response.text();
      if (!response.ok) {
        try { alert(JSON.parse(content).detail || 'Generation failed'); }
        catch { alert(content || 'Generation failed'); }
        return;
      }
      document.getElementById('kubesprayConfig').textContent = content;
      refreshHealth();
    }

    async function copyKubesprayConfig() {
      const content = document.getElementById('kubesprayConfig').textContent;
      await navigator.clipboard.writeText(content);
    }

    function downloadKubesprayConfig() {
      window.location.href = '/api/kubespray-config?download=true';
    }

    async function startDownload() {
      if (!previewData || !previewData.prepared) {
        alert('Prepare and review the download list first');
        return;
      }
      const body = {
        kubespray_version: document.getElementById('version').value,
        push_images: document.getElementById('pushReg').checked,
        push_to_registry: document.getElementById('pushReg').checked,
        save_image_tars: document.getElementById('tars').checked,
        skip_pypi: !document.getElementById('pypi').checked,
        skip_os_repos: !document.getElementById('osrepo').checked,
      };
      const r = await fetch('/api/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const j = await r.json();
      if (!r.ok) { alert(j.detail || 'Failed'); return; }
      showPage('job');
      refreshStatus();
    }

    async function prepareList() {
      const r = await fetch('/api/prepare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kubespray_version: document.getElementById('version').value }),
      });
      const j = await r.json();
      if (!r.ok) { alert(j.detail || 'Failed'); return; }
      previewData = null;
      refreshStatus();
    }

    function pushFormBody() {
      return {
        registry_host: document.getElementById('pushRegistry').value.trim() || null,
        registry_project: document.getElementById('pushProject').value.trim() || null,
        username: document.getElementById('pushUser').value.trim() || null,
        password: document.getElementById('pushPass').value || null,
        tls_verify: document.getElementById('pushTls').checked,
      };
    }

    async function registryLogin() {
      const body = pushFormBody();
      const hint = document.getElementById('loginHint');
      if (!body.username) {
        alert('Username is required to login');
        return;
      }
      if (!body.registry_host && !confirm('No registry address set. Login to local registry:5000?')) {
        return;
      }
      const btn = document.getElementById('btnLogin');
      btn.disabled = true;
      try {
        const r = await fetch('/api/registry/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const j = await r.json();
        if (!r.ok) {
          hint.textContent = 'Login failed: ' + (j.detail || 'unknown error');
          alert(j.detail || 'Login failed');
          return;
        }
        hint.textContent = j.message || 'Login ok';
      } finally {
        btn.disabled = false;
      }
    }

    async function startPush() {
      const body = pushFormBody();
      if (!body.registry_host && !confirm('No registry address set. Push all images to the local registry?')) {
        return;
      }
      if (body.registry_host && !body.username) {
        alert('Username is required when pushing to an external registry');
        return;
      }
      const r = await fetch('/api/push', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const j = await r.json();
      if (!r.ok) { alert(j.detail || 'Failed'); return; }
      showPage('job');
      refreshStatus();
    }

    async function refreshJobHistory() {
      const data = await (await fetch('/api/jobs?limit=100')).json();
      const container = document.getElementById('jobHistory');
      if (!data.jobs.length) {
        container.textContent = 'No completed jobs yet.';
        return;
      }
      container.innerHTML = data.jobs.map(record => `
        <button class="preview-item ${record.status === 'success' ? 'cached' : 'pending'}"
                style="color:var(--text);text-align:left;cursor:pointer"
                onclick="viewJob('${record.id}')">
          <strong>${escapeHtml(record.type || 'job')}</strong> · ${escapeHtml(record.status)}
          <div class="preview-dest">${escapeHtml(record.id)} · ${escapeHtml(record.started_at || '—')}<br/>
          ${escapeHtml(record.message || '')}</div>
        </button>
      `).join('');
    }

    async function viewJob(id) {
      const response = await fetch(`/api/jobs/${encodeURIComponent(id)}`);
      const record = await response.json();
      if (!response.ok) { alert(record.detail || 'Job not found'); return; }
      const output = document.getElementById('historyLog');
      output.style.display = 'block';
      output.textContent = [
        `${record.id} · ${record.type} · ${record.status}`,
        `${record.started_at || '—'} → ${record.finished_at || '—'}`,
        record.message || '',
        `Stats: ${JSON.stringify(record.stats || {})}`,
        '',
        ...(record.log_tail || []),
      ].join(String.fromCharCode(10));
    }

    refreshHealth();
    loadKubesprayConfig();
    refreshPreview();
    refreshJobHistory();
    refreshStatus();
    document.getElementById('version').addEventListener('input', refreshPreview);
    setInterval(refreshStatus, 2000);
    setInterval(refreshHealth, 10000);
    setInterval(refreshJobHistory, 10000);
  </script>
</body>
</html>
"""
