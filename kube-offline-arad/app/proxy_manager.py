from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


proxy_operation_lock = threading.Lock()


HTTP_INBOUND = {
    "tag": "koa-http",
    "listen": "0.0.0.0",
    "port": 10809,
    "protocol": "http",
    "settings": {},
}

SOCKS_INBOUND = {
    "tag": "koa-socks",
    "listen": "0.0.0.0",
    "port": 10808,
    "protocol": "socks",
    "settings": {"auth": "noauth", "udp": True},
}


def normalize_v2ray_config(content: bytes | str) -> dict[str, Any]:
    try:
        config = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid V2Ray JSON: {exc}") from exc

    if not isinstance(config, dict):
        raise ValueError("V2Ray config must be a JSON object")
    outbounds = config.get("outbounds")
    if not isinstance(outbounds, list) or not outbounds:
        raise ValueError("V2Ray config must contain at least one outbound")
    proxy_outbound = next(
        (
            outbound
            for outbound in outbounds
            if isinstance(outbound, dict) and outbound.get("tag") == "proxy"
        ),
        None,
    )
    if proxy_outbound is None:
        proxy_outbound = next(
            (
                outbound
                for outbound in outbounds
                if isinstance(outbound, dict)
                and outbound.get("protocol")
                not in {"freedom", "blackhole", "dns", "loopback"}
            ),
            None,
        )
    if proxy_outbound is None:
        raise ValueError(
            "V2Ray config has no tunnel outbound; add a non-'freedom' outbound "
            "tagged 'proxy'"
        )
    proxy_tag = proxy_outbound.get("tag")
    if not isinstance(proxy_tag, str) or not proxy_tag:
        proxy_tag = "koa-proxy"
        proxy_outbound["tag"] = proxy_tag

    inbounds = config.get("inbounds", [])
    if not isinstance(inbounds, list):
        raise ValueError("V2Ray 'inbounds' must be an array")

    # The app always connects to these fixed ports over the Compose network.
    # Replace conflicting listeners while preserving every unrelated inbound.
    config["inbounds"] = [
        inbound
        for inbound in inbounds
        if not (
            isinstance(inbound, dict)
            and (
                inbound.get("tag") in {"koa-http", "koa-socks"}
                or inbound.get("port") in {10808, 10809}
            )
        )
    ] + [HTTP_INBOUND, SOCKS_INBOUND]

    routing = config.setdefault("routing", {})
    if not isinstance(routing, dict):
        raise ValueError("V2Ray 'routing' must be an object")
    rules = routing.setdefault("rules", [])
    if not isinstance(rules, list):
        raise ValueError("V2Ray routing 'rules' must be an array")
    routing["rules"] = [
        rule
        for rule in rules
        if not (
            isinstance(rule, dict)
            and set(rule.get("inboundTag", [])) & {"koa-http", "koa-socks"}
        )
    ]
    routing["rules"].insert(
        0,
        {
            "type": "field",
            "inboundTag": ["koa-http", "koa-socks"],
            "outboundTag": proxy_tag,
        },
    )
    return config


def write_normalized_config(path: Path, content: bytes | str) -> None:
    config = normalize_v2ray_config(content)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def restart_v2ray(container: str) -> None:
    proc = subprocess.run(
        ["docker", "restart", container],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown Docker error").strip()
        raise RuntimeError(f"Could not restart {container}: {detail}")


def wait_for_proxy(
    *,
    proxy_url: str,
    test_url: str,
    attempts: int,
    delay_seconds: float,
    log: Callable[[str], None] | None = None,
) -> None:
    parsed_proxy = urlparse(proxy_url)
    host = parsed_proxy.hostname
    port = parsed_proxy.port
    if not host or not port:
        raise RuntimeError(f"Invalid proxy URL: {proxy_url}")

    listener_error = ""
    for listener_attempt in range(1, 41):
        try:
            with socket.create_connection((host, port), timeout=0.5):
                listener_error = ""
                break
        except OSError as exc:
            listener_error = str(exc)
            if listener_attempt < 40:
                time.sleep(0.25)
    if listener_error:
        raise RuntimeError(
            f"V2Ray listener {host}:{port} did not start within 10 seconds: "
            f"{listener_error}"
        )
    if log:
        log(f"V2Ray listener ready at {host}:{port}; testing tunneled HTTPS")

    last_error = ""
    for attempt in range(1, attempts + 1):
        proc = subprocess.run(
            [
                "curl",
                "--silent",
                "--show-error",
                "--output",
                "/dev/null",
                "--connect-timeout",
                "5",
                "--max-time",
                "15",
                "--proxy",
                proxy_url,
                "--noproxy",
                "",
                test_url,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            if log:
                log(
                    f"V2Ray ready: tunneled HTTPS check passed "
                    f"({proxy_url} → {test_url})"
                )
            return
        last_error = (proc.stderr or proc.stdout or f"curl rc={proc.returncode}").strip()
        if log:
            log(
                f"V2Ray readiness [{attempt}/{attempts}] failed: {last_error}"
            )
        if attempt < attempts:
            time.sleep(delay_seconds)

    raise RuntimeError(
        f"V2Ray proxy did not become ready after {attempts} attempts: {last_error}"
    )


def recent_v2ray_logs(container: str, lines: int = 30) -> str:
    proc = subprocess.run(
        ["docker", "logs", "--tail", str(lines), container],
        text=True,
        capture_output=True,
        check=False,
    )
    return (proc.stdout + proc.stderr).strip()
