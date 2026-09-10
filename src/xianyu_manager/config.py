from __future__ import annotations

import os
import secrets
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    project_root: Path
    product_library: Path
    validator_path: Path
    data_dir: Path
    database_path: Path
    static_dir: Path
    browser_profiles_dir: Path
    browser_executable: Path | None
    browser_headless: bool
    auto_reply_secret_path: Path
    selection_bridge_token_path: Path
    safe_mode: bool = False
    prepare_mode: bool = False
    account_id: int | None = None
    login_authorized: bool = False
    egress_status_path: Path | None = None
    reply_only: bool = False
    order_cutoff_at: str = ''
    resident_reply: bool = False
    mvp_fulfillment: bool = False
    catalog_delivery: bool = False
    fulfillment_items: tuple[str, ...] = ()
    browser_cdp_url: str = ''


def _parse_bool(value: str, *, name: str, default: bool) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true 或 false")


def _browser_candidates(platform_name: str) -> list[Path]:
    if platform_name == "win32":
        return [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ]
    if platform_name == "darwin":
        return [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    return [Path(path) for name in ("google-chrome", "chromium", "chromium-browser") if (path := shutil.which(name))]


def find_browser_executable() -> Path | None:
    explicit = os.environ.get("XIANYU_BROWSER_EXECUTABLE", "").strip()
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"XIANYU_BROWSER_EXECUTABLE 指向的浏览器不存在：{path}")
        return path
    candidates = _browser_candidates(sys.platform)
    return next((path for path in candidates if path.is_file()), None)


def _configured_path(environment_name: str, fallback: Path, *, kind: str) -> Path:
    explicit = os.environ.get(environment_name, "").strip()
    if not explicit:
        return fallback
    path = Path(explicit).expanduser().resolve()
    exists = path.is_dir() if kind == "目录" else path.is_file()
    if not exists:
        raise FileNotFoundError(f"{environment_name} 指向的{kind}不存在：{path}")
    return path


def load_settings() -> Settings:
    safe_mode = read_safe_mode()
    policy_options = read_runtime_options()
    manager_root = Path(__file__).resolve().parents[2]
    project_root = manager_root.parent
    data_dir_override = os.environ.get("XIANYU_MANAGER_DATA_DIR", "").strip()
    data_dir = (
        Path(data_dir_override).expanduser().resolve()
        if data_dir_override
        else manager_root / "data"
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    product_library = _configured_path(
        "XIANYU_PRODUCT_LIBRARY_DIR", project_root / "商品库", kind="目录"
    )
    validator_path = _configured_path(
        "XIANYU_PRODUCT_VALIDATOR_PATH",
        project_root / "scripts" / "validate_product_library.py",
        kind="文件",
    )
    selection_bridge_token_path = data_dir / "selection-bridge-token.txt"
    if not selection_bridge_token_path.is_file():
        selection_bridge_token_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return Settings(
        project_root=project_root,
        product_library=product_library,
        validator_path=validator_path,
        data_dir=data_dir,
        database_path=data_dir / "manager.db",
        static_dir=manager_root / "static",
        browser_profiles_dir=data_dir / "browser-profiles",
        browser_executable=find_browser_executable(),
        browser_cdp_url=read_browser_cdp_url(),
        browser_headless=_parse_bool(
            os.environ.get("XIANYU_BROWSER_HEADLESS", ""),
            name="XIANYU_BROWSER_HEADLESS",
            default=False,
        ),
        auto_reply_secret_path=data_dir / "siliconflow-api-key.dpapi",
        selection_bridge_token_path=selection_bridge_token_path,
        safe_mode=safe_mode,
        **policy_options,
    )


def read_browser_cdp_url() -> str:
    from .external_browser import validate_cdp_url
    value = validate_cdp_url(os.environ.get('XIANYU_MANAGER_BROWSER_CDP_URL', '').strip())
    if value and (sys.platform != 'linux' or os.environ.get('XIANYU_MANAGER_ACCOUNT_ID') != '2'):
        raise ValueError('EXTERNAL_BROWSER_REQUIRES_LINUX_ACCOUNT_2')
    return value


def read_safe_mode() -> bool:
    return _parse_bool(
        os.environ.get("XIANYU_MANAGER_SAFE_MODE", ""),
        name="XIANYU_MANAGER_SAFE_MODE", default=False,
    )


def read_catalog_delivery() -> bool:
    return _parse_bool(os.environ.get('XIANYU_MANAGER_CATALOG_DELIVERY', ''), name='XIANYU_MANAGER_CATALOG_DELIVERY', default=False)


def read_runtime_options() -> dict:
    prepare = _parse_bool(os.environ.get('XIANYU_MANAGER_PREPARE_MODE', ''), name='XIANYU_MANAGER_PREPARE_MODE', default=False)
    login = _parse_bool(os.environ.get('XIANYU_MANAGER_LOGIN_AUTHORIZED', ''), name='XIANYU_MANAGER_LOGIN_AUTHORIZED', default=False)
    raw = os.environ.get('XIANYU_MANAGER_ACCOUNT_ID', '').strip()
    if raw and (not raw.isascii() or not raw.isdigit() or int(raw) <= 0):
        raise ValueError('XIANYU_MANAGER_ACCOUNT_ID must be a positive integer')
    account = int(raw) if raw else None
    if login and account is None:
        raise ValueError('LOGIN_AUTHORIZED requires ACCOUNT_ID')
    path = os.environ.get('XIANYU_MANAGER_EGRESS_STATUS_PATH', '').strip()
    if path and not Path(path).is_absolute():
        raise ValueError('EGRESS_STATUS_PATH must be absolute')
    reply_only = _parse_bool(os.environ.get('XIANYU_MANAGER_REPLY_ONLY', ''), name='XIANYU_MANAGER_REPLY_ONLY', default=False)
    if reply_only and account is None:
        raise ValueError('REPLY_ONLY requires ACCOUNT_ID')
    resident = _parse_bool(os.environ.get('XIANYU_MANAGER_RESIDENT_REPLY', ''), name='XIANYU_MANAGER_RESIDENT_REPLY', default=False)
    mvp = _parse_bool(os.environ.get('XIANYU_MANAGER_MVP_FULFILLMENT', ''), name='XIANYU_MANAGER_MVP_FULFILLMENT', default=False)
    catalog = read_catalog_delivery()
    if catalog and not mvp:
        raise ValueError('CATALOG_DELIVERY requires recovery-disabled MVP mode')
    items = tuple(x.strip() for x in os.environ.get('XIANYU_MANAGER_FULFILLMENT_ITEMS', '').split(',') if x.strip())
    if any(not x.isascii() or not x.isdigit() for x in items) or len(set(items)) != len(items):
        raise ValueError('FULFILLMENT_ITEMS must be unique complete numeric IDs')
    if resident and (not (reply_only or mvp) or prepare or account is None or not login):
        raise ValueError('RESIDENT_REPLY requires reply-only, explicit account and login authorization')
    cutoff = os.environ.get('XIANYU_MANAGER_ORDER_CUTOFF_AT', '').strip()
    if mvp:
        from .order_cutoff import utc_time
        if reply_only or prepare or account != 2 or not resident or (not items and not catalog):
            raise ValueError('MVP_FULFILLMENT requires resident account2, nonempty allowlist and normal mode')
        utc_time(cutoff, field='ORDER_CUTOFF')
    return dict(prepare_mode=prepare, account_id=account, login_authorized=login,
                reply_only=reply_only, resident_reply=resident, order_cutoff_at=cutoff,
                mvp_fulfillment=mvp, fulfillment_items=items, catalog_delivery=catalog,
                egress_status_path=Path(path) if path else None)
