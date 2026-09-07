"""All local tests use synthetic assets and prohibit real network/browser IO."""
import os
import socket
import tempfile
from pathlib import Path

import pytest


_assets = tempfile.TemporaryDirectory(prefix="xianyu-tests-")
_root = Path(_assets.name)
_library = _root / "library"
_library.mkdir()
for number in range(1, 20):
    product = _library / f"{number:02d}-synthetic"
    product.mkdir()
    (product / "发布文案.txt").write_text("合成测试标题\n仅用于离线单元测试的资料", encoding="utf-8")
_validator = _root / "validator.py"
_validator.write_text("def validate_product(path, number, errors):\n    pass\n", encoding="utf-8")
os.environ["XIANYU_MANAGER_DATA_DIR"] = str(_root / "data")
os.environ["XIANYU_PRODUCT_LIBRARY_DIR"] = str(_library)
os.environ["XIANYU_PRODUCT_VALIDATOR_PATH"] = str(_validator)
os.environ.pop("SILICONFLOW_API_KEY", None)
os.environ.pop("XIANYU_BROWSER_EXECUTABLE", None)
os.environ.pop("XIANYU_MANAGER_SAFE_MODE", None)
for option in ("PREPARE_MODE", "ACCOUNT_ID", "LOGIN_AUTHORIZED", "EGRESS_STATUS_PATH", "API_UDS", "REPLY_ONLY", "ORDER_CUTOFF_AT"):
    os.environ.pop("XIANYU_MANAGER_" + option, None)


@pytest.fixture(autouse=True)
def forbid_live_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected live network/browser IO in local test")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    from playwright.async_api import BrowserType
    monkeypatch.setattr(BrowserType, "launch", forbidden)
    monkeypatch.setattr(BrowserType, "launch_persistent_context", forbidden)


def pytest_collection_modifyitems(items):
    for item in items:
        if item.name == "test_current_product_library_passes":
            item.add_marker(pytest.mark.skip(
                reason="External real product library integrity test; run separately on authorized host"
            ))
