from __future__ import annotations

import concurrent.futures
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from job_state import job
from proxy_manager import (
    proxy_operation_lock,
    recent_v2ray_logs,
    restart_v2ray,
    wait_for_proxy,
    write_normalized_config,
)
from settings import settings


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


def _proxy_env() -> dict[str, str]:
    env = os.environ.copy()
    env["HTTP_PROXY"] = settings.http_proxy
    env["HTTPS_PROXY"] = settings.https_proxy
    env["http_proxy"] = settings.http_proxy
    env["https_proxy"] = settings.https_proxy
    env["ALL_PROXY"] = settings.all_proxy
    env["all_proxy"] = settings.all_proxy
    env["NO_PROXY"] = settings.no_proxy
    env["no_proxy"] = settings.no_proxy
    return env


def _direct_env() -> dict[str, str]:
    """Copy process env with proxy vars removed (direct / LAN access)."""
    env = os.environ.copy()
    for key in _PROXY_ENV_KEYS:
        env.pop(key, None)
    return env


def _redact_cmd(args: list[str]) -> str:
    """Hide credential values from job logs."""
    redacted: list[str] = []
    hide_next = False
    for arg in args:
        if hide_next:
            redacted.append("***")
            hide_next = False
            continue
        if arg in ("--dest-creds", "--src-creds"):
            redacted.append(arg)
            hide_next = True
            continue
        if arg.startswith("--dest-creds=") or arg.startswith("--src-creds="):
            key = arg.split("=", 1)[0]
            redacted.append(f"{key}=***")
            continue
        redacted.append(arg)
    return " ".join(redacted)


def run_cmd(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    use_proxy: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    # Compose injects HTTP(S)_PROXY into the app container. use_proxy=False must
    # strip those vars; otherwise skopeo still tunnels LAN registry pushes via V2Ray.
    env = _proxy_env() if use_proxy else _direct_env()
    if extra_env:
        env.update(extra_env)
    job.log("$ " + _redact_cmd(args))
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=True,
    )
    if proc.stdout:
        for line in proc.stdout.splitlines()[-30:]:
            job.log(line)
    if proc.stderr:
        for line in proc.stderr.splitlines()[-30:]:
            job.log(line)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {_redact_cmd(args)}"
        )
    job.log(
        f"Command finished rc={proc.returncode} in {time.monotonic() - started:.1f}s"
    )
    return proc


def ensure_dirs() -> None:
    for p in [
        settings.outputs_dir / "files",
        settings.outputs_dir / "images",
        settings.outputs_dir / "charts",
        settings.outputs_dir / "kubespray",
        settings.outputs_dir / "scripts",
        settings.outputs_dir / "pypi",
        settings.outputs_dir / "debs",
        settings.outputs_dir / "rpms",
        settings.cache_dir,
    ]:
        p.mkdir(parents=True, exist_ok=True)


def validate_download_environment() -> None:
    job.log("Acquiring exclusive V2Ray configuration lock")
    with proxy_operation_lock:
        _validate_download_environment()


def _validate_download_environment() -> None:
    config = settings.config_dir / "v2ray.json"
    if not config.exists() or config.stat().st_size == 0:
        raise RuntimeError(
            "V2Ray config is missing or empty; upload config/v2ray.json before downloading"
        )
    try:
        write_normalized_config(config, config.read_bytes())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"V2Ray config is invalid: {exc}") from exc
    job.log(f"V2Ray config: {config} ({_format_bytes(config.stat().st_size)})")
    job.log(
        "V2Ray listeners enforced: HTTP 0.0.0.0:10809, "
        "SOCKS 0.0.0.0:10808; both routed only to the tunnel outbound"
    )
    job.log(f"Restarting V2Ray container {settings.v2ray_container}")
    restart_v2ray(settings.v2ray_container)
    try:
        wait_for_proxy(
            proxy_url=settings.http_proxy,
            test_url=settings.proxy_test_url,
            attempts=settings.proxy_ready_attempts,
            delay_seconds=settings.proxy_ready_delay_seconds,
            log=job.log,
        )
    except RuntimeError:
        logs = recent_v2ray_logs(settings.v2ray_container)
        if logs:
            job.log("Recent V2Ray logs:")
            for line in logs.splitlines():
                job.log(line)
        raise
    job.log(
        f"Outbound downloads use HTTP proxy {settings.http_proxy}; "
        f"local pushes bypass proxy via NO_PROXY={settings.no_proxy}"
    )
    job.log(
        f"Publish targets: files/charts={settings.http_public_url}, "
        f"images={settings.registry_public_host}"
    )


def kubespray_dir() -> Path:
    ver = settings.kubespray_version.lstrip("v")
    return settings.cache_dir / f"kubespray-{ver}"


def fetch_kubespray() -> Path:
    job.set_phase("kubespray", "Downloading Kubespray source")
    ver = settings.kubespray_version.lstrip("v")
    dest = kubespray_dir()
    if dest.exists() and (dest / "contrib/offline/generate_list.sh").exists():
        job.log(f"Using cached Kubespray at {dest}")
        return dest

    if dest.exists():
        shutil.rmtree(dest)

    tarball = settings.cache_dir / f"kubespray-{ver}.tar.gz"
    url = f"https://github.com/kubernetes-sigs/kubespray/archive/refs/tags/v{ver}.tar.gz"
    job.log(f"Downloading {url}")
    run_cmd(["curl", "-fL", "--retry", "5", "-o", str(tarball), url])

    with tarfile.open(tarball, "r:gz") as tf:
        tf.extractall(settings.cache_dir)

    extracted = settings.cache_dir / f"kubespray-{ver}"
    if not extracted.exists():
        # some tags extract as kubespray-X.Y.Z
        candidates = list(settings.cache_dir.glob(f"kubespray-{ver}*"))
        dirs = [c for c in candidates if c.is_dir()]
        if not dirs:
            raise RuntimeError("Failed to extract Kubespray tarball")
        extracted = dirs[0]
        if extracted != dest:
            extracted.rename(dest)

    # Copy tarball into outputs for air-gap use
    out_tar = settings.outputs_dir / "kubespray" / f"kubespray-{ver}.tar.gz"
    shutil.copy2(tarball, out_tar)
    job.log(f"Kubespray ready: {dest}")
    return dest


def generate_lists(ks_dir: Path) -> tuple[Path, Path]:
    job.set_phase("lists", "Generating offline file/image lists from Kubespray")
    gen = ks_dir / "contrib/offline/generate_list.sh"
    if not gen.exists():
        raise RuntimeError(f"Missing {gen}")

    # Official script expands Jinja via ansible-playbook (needs ansible-core in PATH).
    # Extra args are forwarded to ansible-playbook by generate_list.sh.
    try:
        run_cmd(
            ["bash", str(gen), "-i", "localhost,", "-c", "local"],
            cwd=ks_dir,
            extra_env={"ANSIBLE_HOST_KEY_CHECKING": "False"},
        )
        files_list = ks_dir / "contrib/offline/temp/files.list"
        images_list = ks_dir / "contrib/offline/temp/images.list"
        if files_list.exists() and images_list.exists() and files_list.stat().st_size > 0:
            job.log(
                f"Generated lists: {sum(1 for _ in files_list.open())} files, "
                f"{sum(1 for _ in images_list.open())} images"
            )
            return files_list, images_list
    except Exception as exc:  # noqa: BLE001
        job.log(f"generate_list.sh failed ({exc}); using fallback parser")

    return _fallback_generate_lists(ks_dir)


def _fallback_generate_lists(ks_dir: Path) -> tuple[Path, Path]:
    """Best-effort list generation without full ansible offline tooling."""
    temp = settings.cache_dir / "lists"
    temp.mkdir(parents=True, exist_ok=True)
    files_list = temp / "files.list"
    images_list = temp / "images.list"

    # Use Kubespray's contrib/offline/offline.yml helper if present after generate fails
    # Minimal core set for common kubeadm/kubespray installs.
    # Prefer extracting URLs from roles/download/defaults if available.
    defaults_candidates = [
        ks_dir / "roles/download/defaults/main.yml",
        ks_dir / "roles/kubespray_defaults/defaults/main/download.yml",
        ks_dir / "roles/kubespray_defaults/vars/main/download.yml",
    ]
    text = ""
    for c in defaults_candidates:
        if c.exists():
            text += c.read_text(encoding="utf-8", errors="ignore") + "\n"

    urls = sorted(set(re.findall(r"https?://[^\s\"']+", text)))
    file_urls = [
        u
        for u in urls
        if any(
            x in u
            for x in (
                "kubeadm",
                "kubectl",
                "kubelet",
                "etcd-",
                "cni-plugins",
                "crictl",
                "helm-",
                "runc.",
                "containerd-",
                "nerdctl-",
                "calicoctl",
                "cilium-linux",
            )
        )
    ]
    # Replace {{ }} templated URLs — keep only concrete ones
    file_urls = [u for u in file_urls if "{{" not in u and "}}" not in u]

    image_refs: list[str] = []
    for line in text.splitlines():
        m = re.search(
            r"(?:registry|repo).*=\s*[\"']?([a-z0-9][a-z0-9./:_-]+)",
            line,
            re.I,
        )
        if m and ":" in m.group(1) and "/" in m.group(1):
            image_refs.append(m.group(1))

    # Always include registry + nginx helper images used by this project
    image_refs.extend(
        [
            "registry:2.8.3",
            "nginx:1.27-alpine",
            "registry.k8s.io/pause:3.10",
        ]
    )

    # Also try images.list from a previous successful run in kubespray temp
    prev_images = ks_dir / "contrib/offline/temp/images.list"
    if prev_images.exists():
        image_refs.extend(
            [
                ln.strip()
                for ln in prev_images.read_text().splitlines()
                if ln.strip() and not ln.startswith("#")
            ]
        )

    files_list.write_text("\n".join(sorted(set(file_urls))) + "\n", encoding="utf-8")
    images_list.write_text("\n".join(sorted(set(image_refs))) + "\n", encoding="utf-8")
    job.log(
        f"Fallback lists: {len(set(file_urls))} files, {len(set(image_refs))} images"
    )
    return files_list, images_list


def decide_relative_dir(url: str) -> str:
    rdir = url
    replacements = [
        (r".*/(v[0-9.]*)/.*/kube(adm|ctl|let)", r"kubernetes/\1"),
        (r".*/etcd-.*\.tar\.gz", "kubernetes/etcd"),
        (r".*/cni-plugins.*\.tgz", "kubernetes/cni"),
        (r".*/crictl-.*\.tar\.gz", "kubernetes/cri-tools"),
        (r".*/(v[^/]*)/calicoctl-.*", r"kubernetes/calico/\1"),
        (r".*/(v[^/]*)/runc\.[^/]+", r"runc/\1"),
        (r".*/(v[^/]*)/cilium-linux-.*", r"cilium-cli/\1"),
        (r".*/helm-v.*\.tar\.gz", "."),
        (r".*/containerd-.*\.tar\.gz", "."),
        (r".*/nerdctl-.*\.tar\.gz", "."),
    ]
    for pat, repl in replacements:
        new = re.sub(pat, repl, rdir)
        if new != rdir:
            return new
    return "."


def _manifest_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        raw.strip()
        for raw in path.read_text(encoding="utf-8").splitlines()
        if raw.strip() and not raw.strip().startswith("#") and "{{" not in raw
    ]


def prepare_download_list(kubespray_version: str | None = None) -> None:
    """Generate manifests and offline.yml without downloading any artifacts."""
    if kubespray_version:
        settings.kubespray_version = kubespray_version.lstrip("v")
    ensure_dirs()
    write_generated_offline_yml()
    job.set_phase("proxy", "Testing V2Ray before preparing manifests")
    validate_download_environment()
    ks_dir = fetch_kubespray()
    files_list, images_list = generate_lists(ks_dir)
    shutil.copy2(files_list, settings.outputs_dir / "files" / "files.list")
    shutil.copy2(images_list, settings.outputs_dir / "images" / "images.list")
    preview = get_download_preview()
    job.update_stats(
        preview_files=len(preview["files"]),
        preview_images=len(preview["images"]),
        preview_charts=len(preview["charts"]),
    )
    job.set_phase(
        "ready",
        f"Download list ready: {len(preview['files'])} files, "
        f"{len(preview['images'])} images, {len(preview['charts'])} charts",
    )


def get_download_preview() -> dict[str, object]:
    files_dir = settings.outputs_dir / "files"
    images_dir = settings.outputs_dir / "images"
    charts_dir = settings.outputs_dir / "charts"

    file_items: list[dict[str, object]] = []
    for url in _manifest_lines(files_dir / "files.list"):
        filename = urlparse(url).path.rstrip("/").split("/")[-1]
        if not filename:
            continue
        relative = Path(decide_relative_dir(url)) / filename
        destination = files_dir / relative
        file_items.append(
            {
                "source": url,
                "destination": f"/files/{relative}",
                "cached": destination.exists() and destination.stat().st_size > 0,
                "size": destination.stat().st_size if destination.exists() else 0,
            }
        )

    refs = _manifest_lines(images_dir / "images.list")
    refs.extend(_manifest_lines(settings.config_dir / "extra-images.txt"))
    image_items: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_ref in refs:
        ref = _normalize_image(raw_ref)
        if ref in seen:
            continue
        seen.add(ref)
        tar_path = images_dir / _safe_tar_name(ref)
        image_items.append(
            {
                "source": ref,
                "destination": _registry_dest(ref),
                "cached": tar_path.exists() and tar_path.stat().st_size > 0,
                "size": tar_path.stat().st_size if tar_path.exists() else 0,
            }
        )

    chart_items: list[dict[str, object]] = []
    for spec in _manifest_lines(settings.config_dir / "extra-charts.txt"):
        expected = ""
        if "|" in spec:
            _, name, version = [part.strip() for part in spec.split("|", 2)]
            expected = f"{name}-{version}.tgz"
        elif spec.startswith("oci://"):
            chart_ref = spec.rsplit("/", 1)[-1]
            if ":" in chart_ref:
                name, version = chart_ref.rsplit(":", 1)
                expected = f"{name}-{version}.tgz"
        destination = charts_dir / expected if expected else None
        chart_items.append(
            {
                "source": spec,
                "destination": f"/charts/{expected}" if expected else "/charts/",
                "cached": bool(
                    destination
                    and destination.exists()
                    and destination.stat().st_size > 0
                ),
                "size": destination.stat().st_size if destination and destination.exists() else 0,
            }
        )

    return {
        "version": settings.kubespray_version,
        "prepared": bool(file_items or image_items),
        "files": file_items,
        "images": image_items,
        "charts": chart_items,
    }


def download_files(files_list: Path) -> int:
    files_dir = settings.outputs_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(files_list, files_dir / "files.list")

    urls = [
        raw.strip()
        for raw in files_list.read_text(encoding="utf-8").splitlines()
        if raw.strip() and not raw.strip().startswith("#") and "{{" not in raw
    ]
    job.set_phase(
        "files",
        f"Downloading {len(urls)} files through V2Ray into nginx storage",
    )
    count = 0
    downloaded = 0
    skipped = 0
    failures = 0
    total_bytes = 0
    for index, url in enumerate(urls, start=1):
        filename = urlparse(url).path.rstrip("/").split("/")[-1]
        if not filename:
            job.log(f"FILE [{index}/{len(urls)}] skipped URL without filename: {url}")
            failures += 1
            continue
        rdir = decide_relative_dir(url)
        dest_dir = files_dir / rdir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        if dest.exists() and dest.stat().st_size > 0:
            size = dest.stat().st_size
            job.log(
                f"FILE [{index}/{len(urls)}] cached "
                f"{dest.relative_to(files_dir)} ({_format_bytes(size)})"
            )
            count += 1
            skipped += 1
            total_bytes += size
            job.update_stats(
                files=count,
                files_total=len(urls),
                files_downloaded=downloaded,
                files_cached=skipped,
                files_completed=index,
                file_errors=failures,
                file_bytes=total_bytes,
            )
            continue
        dest.unlink(missing_ok=True)
        job.log(
            f"FILE [{index}/{len(urls)}] download {url} "
            f"→ /files/{dest.relative_to(files_dir)}"
        )
        partial = dest.with_name(dest.name + ".part")
        partial.unlink(missing_ok=True)
        try:
            run_cmd(
                [
                    "curl",
                    "-fL",
                    "--show-error",
                    "--retry",
                    "5",
                    "--retry-delay",
                    "2",
                    "-o",
                    str(partial),
                    url,
                ],
            )
            partial.replace(dest)
            size = dest.stat().st_size
            job.log(
                f"FILE [{index}/{len(urls)}] ready "
                f"{dest.relative_to(files_dir)} ({_format_bytes(size)})"
            )
            count += 1
            downloaded += 1
            total_bytes += size
        except Exception as exc:  # noqa: BLE001
            partial.unlink(missing_ok=True)
            failures += 1
            job.log(
                f"FILE [{index}/{len(urls)}] unavailable: {url} ({exc}); "
                "continuing with remaining artifacts"
            )
        job.update_stats(
            files=count,
            files_total=len(urls),
            files_downloaded=downloaded,
            files_cached=skipped,
            files_completed=index,
            file_errors=failures,
            file_bytes=total_bytes,
        )
    job.update_stats(
        files=count,
        files_total=len(urls),
        files_downloaded=downloaded,
        files_cached=skipped,
        files_completed=len(urls),
        file_errors=failures,
        file_bytes=total_bytes,
    )
    job.log(
        f"FILES complete: {count}/{len(urls)} ready, {downloaded} downloaded, "
        f"{skipped} cached, {failures} unavailable, "
        f"{_format_bytes(total_bytes)} served by nginx"
    )
    return count


def _normalize_image(ref: str) -> str:
    ref = ref.strip()
    if ref.startswith("docker.io/") is False and ref.count("/") == 0:
        return f"docker.io/library/{ref}"
    if "/" in ref and not any(
        ref.startswith(p)
        for p in (
            "docker.io/",
            "registry.k8s.io/",
            "quay.io/",
            "gcr.io/",
            "ghcr.io/",
            "registry.k8s.io",
        )
    ):
        # short docker hub user/image
        if ref.count("/") == 1 and "." not in ref.split("/")[0]:
            return f"docker.io/{ref}"
    return ref


def _image_path(ref: str) -> str:
    """Strip registry host; keep path/name:tag for re-tagging."""
    ref = _normalize_image(ref)
    ref = re.sub(r"^https?://", "", ref)
    parts = ref.split("/")
    if "." in parts[0] or ":" in parts[0] or parts[0] == "localhost":
        return "/".join(parts[1:])
    return ref


def _normalize_registry_host(host: str | None) -> str:
    """Normalize user-provided registry host (strip scheme / trailing slash)."""
    if not host or not host.strip():
        return "registry:5000"
    value = host.strip()
    value = re.sub(r"^docker://", "", value)
    value = re.sub(r"^https?://", "", value)
    return value.rstrip("/")


def _registry_dest(ref: str, host: str | None = None) -> str:
    return f"{_normalize_registry_host(host)}/{_image_path(ref)}"


def _safe_tar_name(ref: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", ref) + ".tar"


def _skopeo_push(
    src_transport: str,
    dest: str,
    *,
    use_proxy: bool,
    username: str | None = None,
    password: str | None = None,
    tls_verify: bool = False,
) -> None:
    args = [
        "skopeo",
        "copy",
        "--insecure-policy",
        f"--dest-tls-verify={'true' if tls_verify else 'false'}",
        "--retry-times",
        "3",
    ]
    if username:
        args += ["--dest-creds", f"{username}:{password or ''}"]
    args += [src_transport, dest]
    run_cmd(args, use_proxy=use_proxy)


def pull_and_push_image(
    ref: str,
    *,
    registry_host: str | None = None,
    username: str | None = None,
    password: str | None = None,
    tls_verify: bool = False,
) -> None:
    ref = _normalize_image(ref)
    images_dir = settings.outputs_dir / "images"
    tar_path = images_dir / _safe_tar_name(ref)

    src = f"docker://{ref}"
    archive = f"docker-archive:{tar_path}:{ref}"
    if settings.save_image_tars:
        if not tar_path.exists():
            job.log(f"IMAGE pull via V2Ray: {ref} → {tar_path.name}")
            run_cmd(
                [
                    "skopeo",
                    "copy",
                    "--insecure-policy",
                    "--retry-times",
                    "3",
                    "--override-os",
                    "linux",
                    "--override-arch",
                    settings.image_arch,
                    src,
                    archive,
                ]
            )
        else:
            job.log(
                f"IMAGE cached: {ref} ({_format_bytes(tar_path.stat().st_size)})"
            )

    if not settings.push_images:
        return

    transport = archive if tar_path.exists() else src
    # Custom host always pushes; otherwise honor the download-time toggle.
    if settings.push_to_registry or registry_host:
        dest_ref = _registry_dest(ref, registry_host)
        dest = f"docker://{dest_ref}"
        job.log(f"IMAGE push: {ref} → {dest_ref}")
        _skopeo_push(
            transport,
            dest,
            use_proxy=False,
            username=username,
            password=password,
            tls_verify=tls_verify,
        )
        job.log(f"IMAGE ready in registry: {dest_ref}")


def download_images(images_list: Path) -> int:
    images_dir = settings.outputs_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(images_list, images_dir / "images.list")

    refs: list[str] = []
    for raw in images_list.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "{{" in line:
            continue
        refs.append(line)

    extra = settings.config_dir / "extra-images.txt"
    if extra.exists():
        for raw in extra.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                refs.append(line)

    # unique preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for r in refs:
        n = _normalize_image(r)
        if n not in seen:
            seen.add(n)
            uniq.append(n)

    workers = max(1, int(settings.image_workers))
    errors: list[str] = []
    progress_lock = threading.Lock()
    completed = 0
    job.set_phase(
        "images",
        f"Pulling {len(uniq)} images through V2Ray into Docker Registry "
        f"({workers} workers)",
    )
    job.update_stats(images_total=len(uniq), images_completed=0, image_errors=0)

    def _one(ref: str) -> None:
        nonlocal completed
        try:
            pull_and_push_image(ref)
        except Exception as exc:  # noqa: BLE001
            with progress_lock:
                errors.append(f"{ref}: {exc}")
            job.log(f"ERROR {ref}: {exc}")
        finally:
            with progress_lock:
                completed += 1
                failed = len(errors)
                job.update_stats(
                    images=completed - failed,
                    images_total=len(uniq),
                    images_completed=completed,
                    image_errors=failed,
                )
                job.log(
                    f"IMAGE progress [{completed}/{len(uniq)}] "
                    f"succeeded={completed - failed} failed={failed}: {ref}"
                )

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_one, uniq))

    job.update_stats(images=len(uniq) - len(errors), image_errors=len(errors))
    if errors:
        job.log(f"{len(errors)} image(s) failed; continuing")
    job.log(
        f"IMAGES complete: {len(uniq) - len(errors)}/{len(uniq)} available "
        f"in registry, {len(errors)} failed"
    )
    return len(uniq)


def download_helm_charts() -> int:
    charts_dir = settings.outputs_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    chart_file = settings.config_dir / "extra-charts.txt"
    if not chart_file.exists():
        job.log(f"CHARTS skipped: {chart_file} does not exist")
        return 0

    charts = [
        raw.strip()
        for raw in chart_file.read_text(encoding="utf-8").splitlines()
        if raw.strip() and not raw.strip().startswith("#")
    ]
    job.set_phase(
        "charts",
        f"Downloading {len(charts)} Helm charts through V2Ray into nginx storage",
    )
    count = 0
    failures = 0
    env = _proxy_env()
    for index, line in enumerate(charts, start=1):
        try:
            if line.startswith("oci://"):
                # oci://host/path:version
                job.log(f"CHART [{index}/{len(charts)}] pull {line}")
                run_cmd(
                    ["helm", "pull", line, "--destination", str(charts_dir)],
                )
                count += 1
            elif "|" in line:
                repo_url, name, version = [x.strip() for x in line.split("|", 2)]
                job.log(
                    f"CHART [{index}/{len(charts)}] pull "
                    f"{name}:{version} from {repo_url}"
                )
                repo_name = re.sub(r"[^a-zA-Z0-9]+", "", urlparse(repo_url).netloc) or "repo"
                subprocess.run(
                    ["helm", "repo", "add", repo_name, repo_url],
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                run_cmd(["helm", "repo", "update", repo_name], check=False)
                run_cmd(
                    [
                        "helm",
                        "pull",
                        f"{repo_name}/{name}",
                        "--version",
                        version,
                        "--destination",
                        str(charts_dir),
                    ]
                )
                count += 1
            else:
                job.log(f"Unknown chart format: {line}")
                failures += 1
        except Exception as exc:  # noqa: BLE001
            job.log(f"Chart download failed ({line}): {exc}")
            failures += 1
        job.update_stats(
            charts=count,
            charts_total=len(charts),
            charts_completed=index,
            chart_errors=failures,
        )
    chart_bytes = sum(p.stat().st_size for p in charts_dir.glob("*.tgz"))
    job.update_stats(charts=count, chart_errors=failures, chart_bytes=chart_bytes)
    job.log(
        f"CHARTS complete: {count}/{len(charts)} downloaded, {failures} failed, "
        f"{_format_bytes(chart_bytes)} served at {settings.http_public_url.rstrip('/')}/charts/"
    )
    return count


def build_pypi_mirror(ks_dir: Path) -> int:
    """Mirror Kubespray Python deps (like pypi-mirror.sh)."""
    if settings.skip_pypi:
        job.log("Skipping PyPI mirror (SKIP_PYPI=true)")
        return 0

    job.set_phase("pypi", "Building PyPI mirror for Kubespray requirements")
    req = ks_dir / "requirements.txt"
    if not req.exists():
        job.log("No requirements.txt in Kubespray; skip pypi")
        return 0

    dest = settings.outputs_dir / "pypi" / "files"
    dest.mkdir(parents=True, exist_ok=True)
    tmp_req = settings.cache_dir / "requirements.tmp"
    text = req.read_text(encoding="utf-8")
    tmp_req.write_text(text + "\nPyYAML\nruamel.yaml\nselinux\nflit_core\n", encoding="utf-8")

    platform = f"manylinux2014_{'x86_64' if settings.image_arch == 'amd64' else 'aarch64'}"
    for pyver in ("3.10", "3.11", "3.12"):
        job.log(f"pip download binaries for python {pyver}")
        run_cmd(
            [
                "pip3",
                "download",
                "-d",
                str(dest),
                "--only-binary",
                ":all:",
                "--python-version",
                pyver,
                "--platform",
                platform,
                "-r",
                str(tmp_req),
            ],
            check=False,
        )

    job.log("pip download sources + tooling")
    run_cmd(
        ["pip3", "download", "-d", str(dest), "--no-binary", ":all:", "-r", str(req)],
        check=False,
    )
    run_cmd(
        ["pip3", "download", "-d", str(dest), "pip", "setuptools", "wheel"],
        check=False,
    )

    # Create simple index layout used by pypi-mirror
    mirror_root = settings.outputs_dir / "pypi"
    run_cmd(
        ["pypi-mirror", "create", "-d", str(dest), "-m", str(mirror_root)],
        check=False,
    )
    count = sum(1 for _ in dest.glob("*") if _.is_file())
    job.stats["pypi"] = count
    return count


def create_os_repo() -> int:
    """Build Ubuntu apt offline repo via docker (like create-repo.sh)."""
    if settings.skip_os_repos:
        job.log("Skipping OS repos (SKIP_OS_REPOS=true)")
        return 0

    job.set_phase("os-repo", f"Downloading Ubuntu {settings.ubuntu_version} apt packages")
    if not Path("/var/run/docker.sock").exists():
        job.log("WARNING: docker.sock not mounted; cannot build OS repo inside helper container")
        return 0

    image = f"ubuntu:{settings.ubuntu_version}"
    # Pull through skopeo so the transfer uses the app's V2Ray proxy. A plain
    # `docker pull` would use the Docker daemon's own network and bypass it.
    job.log(f"Pull helper image through V2Ray: docker.io/library/{image}")
    run_cmd(
        [
            "skopeo",
            "copy",
            "--insecure-policy",
            "--retry-times",
            "3",
            f"docker://docker.io/library/{image}",
            f"docker-daemon:{image}",
        ]
    )

    data_host = os.environ.get("HOST_DATA_DIR", "")  # optional
    # Bind mount the compose data dir: we mounted ./data at /data in app
    # docker run from inside app needs the host path for volume mounts.
    # Fall back to named volume style by reusing the same file contents via /data.
    script = "/opt/os-repo/create-repo-ubuntu.sh"
    pkglist = "/config/pkglist"

    # Copy script+pkglist into /data so the ubuntu container can mount only /data
    helper = settings.data_dir / "_os_repo_helper"
    helper.mkdir(parents=True, exist_ok=True)
    shutil.copy2(script, helper / "create-repo-ubuntu.sh")
    if Path(pkglist).exists():
        dest_pkg = helper / "pkglist"
        if dest_pkg.exists():
            shutil.rmtree(dest_pkg)
        shutil.copytree(pkglist, dest_pkg)

    # Discover host path of /data via docker inspect of this container if possible
    host_data = _host_path_for_data()
    if not host_data:
        job.log("WARNING: could not resolve host path for /data; OS repo skipped")
        return 0

    run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "koa-offline",
            "-e",
            f"HTTP_PROXY={settings.http_proxy}",
            "-e",
            f"HTTPS_PROXY={settings.https_proxy}",
            "-e",
            f"http_proxy={settings.http_proxy}",
            "-e",
            f"https_proxy={settings.https_proxy}",
            "-e",
            f"VERSION_ID={settings.ubuntu_version}",
            "-e",
            "OUTPUTS_DIR=/data/outputs",
            "-e",
            "CACHE_DIR=/data/cache",
            "-e",
            "PKGLIST_DIR=/data/_os_repo_helper/pkglist",
            "-v",
            f"{host_data}:/data",
            image,
            "bash",
            "/data/_os_repo_helper/create-repo-ubuntu.sh",
        ]
    )
    debs = settings.outputs_dir / "debs"
    count = sum(1 for _ in debs.rglob("*.deb")) if debs.exists() else 0
    job.stats["debs"] = count
    return count


def _host_path_for_data() -> str | None:
    """Resolve the host path mounted at /data for nested docker run -v."""
    try:
        hostname = Path("/etc/hostname").read_text(encoding="utf-8").strip()
        proc = subprocess.run(
            ["docker", "inspect", "-f", "{{range .Mounts}}{{if eq .Destination \"/data\"}}{{.Source}}{{end}}{{end}}", hostname],
            capture_output=True,
            text=True,
            check=False,
        )
        src = (proc.stdout or "").strip()
        if src:
            return src
        # fallback: inspect by container name
        proc = subprocess.run(
            ["docker", "inspect", "-f", "{{range .Mounts}}{{if eq .Destination \"/data\"}}{{.Source}}{{end}}{{end}}", "koa-app"],
            capture_output=True,
            text=True,
            check=False,
        )
        src = (proc.stdout or "").strip()
        return src or None
    except Exception:  # noqa: BLE001
        return None


def copy_target_scripts() -> None:
    job.set_phase("scripts", "Copying target-node helper scripts")
    src = Path("/opt/target-scripts")
    dest = settings.outputs_dir / "scripts"
    if src.exists():
        for item in src.iterdir():
            target = dest / item.name
            if item.is_file():
                shutil.copy2(item, target)
                target.chmod(0o755)
    offline_src = Path("/opt/offline.yml")
    if offline_src.exists():
        shutil.copy2(offline_src, settings.outputs_dir / "offline.yml")
    write_generated_offline_yml()


def write_generated_offline_yml() -> None:
    # Nginx serves the output tree directly; images are in Docker Registry v2.
    host = settings.registry_public_host
    http = settings.http_public_url.rstrip("/")
    content = f"""# Generated by kube-offline-arad — copy into inventory/group_vars/all/offline.yml
# Files, Helm charts, PyPI and OS packages: nginx ({http})
# Container images: Docker Registry v2 ({host})
# Install as: inventory/<cluster>/group_vars/all/offline.yml

http_server: "{http}"
registry_host: "{host}"

containerd_registries_mirrors:
  - prefix: "{{{{ registry_host }}}}"
    mirrors:
      - host: "http://{{{{ registry_host }}}}"
        capabilities: ["pull", "resolve"]
        skip_verify: true

files_repo: "{{{{ http_server }}}}/files"
yum_repo: "{{{{ http_server }}}}/rpms"
ubuntu_repo: "{{{{ http_server }}}}/debs"

# PyPI served by nginx:
# pip install --index-url {http}/pypi/simple --trusted-host HOST ...

kube_image_repo: "{{{{ registry_host }}}}"
gcr_image_repo: "{{{{ registry_host }}}}"
docker_image_repo: "{{{{ registry_host }}}}"
quay_image_repo: "{{{{ registry_host }}}}"
github_image_repo: "{{{{ registry_host }}}}"

local_path_provisioner_helper_image_repo: "{{{{ registry_host }}}}/busybox"

kubeadm_download_url: "{{{{ files_repo }}}}/kubernetes/v{{{{ kube_version }}}}/kubeadm"
kubectl_download_url: "{{{{ files_repo }}}}/kubernetes/v{{{{ kube_version }}}}/kubectl"
kubelet_download_url: "{{{{ files_repo }}}}/kubernetes/v{{{{ kube_version }}}}/kubelet"
etcd_download_url: "{{{{ files_repo }}}}/kubernetes/etcd/etcd-v{{{{ etcd_version }}}}-linux-{{{{ image_arch }}}}.tar.gz"
cni_download_url: "{{{{ files_repo }}}}/kubernetes/cni/cni-plugins-linux-{{{{ image_arch }}}}-v{{{{ cni_version }}}}.tgz"
crictl_download_url: "{{{{ files_repo }}}}/kubernetes/cri-tools/crictl-v{{{{ crictl_version }}}}-{{{{ ansible_system | lower }}}}-{{{{ image_arch }}}}.tar.gz"
calicoctl_download_url: "{{{{ files_repo }}}}/kubernetes/calico/v{{{{ calico_ctl_version }}}}/calicoctl-linux-{{{{ image_arch }}}}"
calico_crds_download_url: "{{{{ files_repo }}}}/{{{{ calico_version }}}}.tar.gz"
ciliumcli_download_url: "{{{{ files_repo }}}}/cilium-cli/v{{{{ cilium_cli_version }}}}/cilium-linux-{{{{ image_arch }}}}.tar.gz"
helm_download_url: "{{{{ files_repo }}}}/helm-v{{{{ helm_version }}}}-linux-{{{{ image_arch }}}}.tar.gz"
runc_download_url: "{{{{ files_repo }}}}/runc/v{{{{ runc_version }}}}/runc.{{{{ image_arch }}}}"
nerdctl_download_url: "{{{{ files_repo }}}}/nerdctl-{{{{ nerdctl_version }}}}-{{{{ ansible_system | lower }}}}-{{{{ image_arch }}}}.tar.gz"
containerd_download_url: "{{{{ files_repo }}}}/containerd-{{{{ containerd_version }}}}-linux-{{{{ image_arch }}}}.tar.gz"
crun_download_url: "{{{{ files_repo }}}}/crun-{{{{ crun_version }}}}-linux-{{{{ image_arch }}}}"
crio_download_url: "{{{{ files_repo }}}}/cri-o.{{{{ image_arch }}}}.{{{{ crio_version }}}}.tar.gz"
"""
    output = settings.outputs_dir / "offline.yml"
    output.write_text(content, encoding="utf-8")
    job.log(f"Kubespray offline config ready: {output} → {http}/offline.yml")


def push_saved_images(
    *,
    registry_host: str | None = None,
    username: str | None = None,
    password: str | None = None,
    tls_verify: bool = False,
) -> int:
    """Push previously saved docker-archive tars to a Docker Registry v2."""
    images_dir = settings.outputs_dir / "images"
    list_path = images_dir / "images.list"
    if not list_path.exists():
        raise RuntimeError("No images.list found; run a full download first")

    refs = [
        raw.strip()
        for raw in list_path.read_text(encoding="utf-8").splitlines()
        if raw.strip() and not raw.strip().startswith("#")
    ]
    target = _normalize_registry_host(registry_host)
    job.set_phase(
        "push",
        f"Pushing {len(refs)} saved image tars to {target}",
    )
    settings.push_images = True
    settings.push_to_registry = True
    count = 0
    for index, line in enumerate(refs, start=1):
        ref = _normalize_image(line)
        tar_path = images_dir / _safe_tar_name(ref)
        job.log(f"PUSH [{index}/{len(refs)}] {ref} → {target}")
        if not tar_path.exists():
            job.log(f"Missing tar for {ref}, pulling upstream")
        pull_and_push_image(
            ref,
            registry_host=registry_host,
            username=username,
            password=password,
            tls_verify=tls_verify,
        )
        count += 1
        job.update_stats(pushed=count, push_total=len(refs))
    job.log(f"PUSH complete: {count}/{len(refs)} images → {target}")
    return count


def run_full_download(
    *,
    kubespray_version: str | None = None,
    push_images: bool = True,
    save_image_tars: bool = True,
    push_to_registry: bool | None = None,
    skip_os_repos: bool | None = None,
    skip_pypi: bool | None = None,
) -> None:
    """
    Full offline pipeline modeled on kubespray-offline download-all.sh:
      get-kubespray → files+charts+images → pypi-mirror → os repos →
      copy scripts. Outbound downloads use V2Ray; nginx serves files and
      Docker Registry v2 stores container images.
    """
    if kubespray_version:
        settings.kubespray_version = kubespray_version.lstrip("v")
    settings.push_images = push_images
    settings.save_image_tars = save_image_tars
    if push_to_registry is not None:
        settings.push_to_registry = push_to_registry
    if skip_os_repos is not None:
        settings.skip_os_repos = skip_os_repos
    if skip_pypi is not None:
        settings.skip_pypi = skip_pypi

    ensure_dirs()
    write_generated_offline_yml()
    job.set_phase("proxy", "Normalizing, restarting, and testing the V2Ray tunnel")
    validate_download_environment()
    started = time.monotonic()

    # === stages aligned with kubespray-offline/download-all.sh ===
    # Fetch source and lists first, then populate the two serving components.
    ks = fetch_kubespray()
    files_list, images_list = generate_lists(ks)
    download_files(files_list)
    if not settings.skip_charts:
        download_helm_charts()
    download_images(images_list)  # Kubespray images + config/extra-images.txt

    # Secondary package repositories follow the core files/charts/images stage.
    build_pypi_mirror(ks)
    create_os_repo()
    copy_target_scripts()

    elapsed = time.monotonic() - started
    job.set_phase(
        "done",
        f"Download complete in {elapsed / 60:.1f} min: nginx outputs + Docker Registry",
    )
