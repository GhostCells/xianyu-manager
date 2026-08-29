from __future__ import annotations

import os
import secrets
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
    auto_reply_secret_path: Path
    selection_bridge_token_path: Path


def find_browser_executable() -> Path | None:
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ]
    return next((path for path in candidates if path.is_file()), None)


def load_settings() -> Settings:
    manager_root = Path(__file__).resolve().parents[2]
    project_root = manager_root.parent
    data_dir_override = os.environ.get("XIANYU_MANAGER_DATA_DIR", "").strip()
    data_dir = (
        Path(data_dir_override).expanduser().resolve()
        if data_dir_override
        else manager_root / "data"
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    selection_bridge_token_path = data_dir / "selection-bridge-token.txt"
    if not selection_bridge_token_path.is_file():
        selection_bridge_token_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return Settings(
        project_root=project_root,
        product_library=project_root / "商品库",
        validator_path=project_root / "scripts" / "validate_product_library.py",
        data_dir=data_dir,
        database_path=data_dir / "manager.db",
        static_dir=manager_root / "static",
        browser_profiles_dir=data_dir / "browser-profiles",
        browser_executable=find_browser_executable(),
        auto_reply_secret_path=data_dir / "siliconflow-api-key.dpapi",
        selection_bridge_token_path=selection_bridge_token_path,
    )
