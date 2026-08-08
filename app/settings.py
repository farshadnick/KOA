from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="/config/app.env", extra="ignore")

    data_dir: Path = Path("/data")
    outputs_dir: Path = Path("/data/outputs")
    cache_dir: Path = Path("/data/cache")
    config_dir: Path = Path("/config")

    kubespray_version: str = "2.27.0"
    image_arch: str = "amd64"
    ubuntu_version: str = "22.04"

    registry_url: str = "http://registry:5000"
    registry_public_host: str = "localhost:35000"
    http_public_url: str = "http://localhost:8080"

    http_proxy: str = "http://v2ray:10809"
    https_proxy: str = "http://v2ray:10809"
    all_proxy: str = "socks5://v2ray:10808"
    no_proxy: str = "localhost,127.0.0.1,registry,nginx,koa-registry,koa-nginx"
    v2ray_container: str = "koa-v2ray"
    proxy_test_url: str = "https://www.gstatic.com/generate_204"
    proxy_ready_attempts: int = 12
    proxy_ready_delay_seconds: float = 2.0

    # Pipeline toggles (like download-all.sh stages)
    skip_os_repos: bool = False
    skip_pypi: bool = False
    skip_charts: bool = False
    image_workers: int = 2
    push_images: bool = True
    save_image_tars: bool = True
    push_to_registry: bool = True


settings = Settings()
