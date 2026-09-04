from __future__ import annotations

from pathlib import Path

import pytest

from xianyu_manager import config


def test_explicit_browser_executable_has_priority(tmp_path, monkeypatch):
    browser = tmp_path / "custom-browser"
    browser.touch()
    monkeypatch.setenv("XIANYU_BROWSER_EXECUTABLE", str(browser))
    monkeypatch.setattr(config, "_browser_candidates", lambda _platform: [tmp_path / "fallback"])

    assert config.find_browser_executable() == browser.resolve()


def test_explicit_linux_browser_path_is_accepted(tmp_path, monkeypatch):
    browser = tmp_path / "chromium"
    browser.touch()
    monkeypatch.setenv("XIANYU_BROWSER_EXECUTABLE", str(browser))

    assert config.find_browser_executable() == browser.resolve()


def test_missing_explicit_browser_has_clear_error(tmp_path, monkeypatch):
    missing = tmp_path / "missing-chrome"
    monkeypatch.setenv("XIANYU_BROWSER_EXECUTABLE", str(missing))

    with pytest.raises(FileNotFoundError, match="XIANYU_BROWSER_EXECUTABLE"):
        config.find_browser_executable()


def test_windows_browser_fallback_candidates_are_preserved():
    candidates = config._browser_candidates("win32")

    assert Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe") in candidates
    assert Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") in candidates


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("1", True), ("false", False), ("0", False), ("", False)])
def test_browser_headless_boolean_parsing(value, expected):
    assert config._parse_bool(value, name="XIANYU_BROWSER_HEADLESS", default=False) is expected


def test_invalid_browser_headless_value_is_rejected():
    with pytest.raises(ValueError, match="XIANYU_BROWSER_HEADLESS"):
        config._parse_bool("sometimes", name="XIANYU_BROWSER_HEADLESS", default=False)


def test_explicit_product_and_validator_paths(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    validator = tmp_path / "validator.py"
    validator.touch()
    data = tmp_path / "data"
    monkeypatch.setenv("XIANYU_MANAGER_DATA_DIR", str(data))
    monkeypatch.setenv("XIANYU_PRODUCT_LIBRARY_DIR", str(library))
    monkeypatch.setenv("XIANYU_PRODUCT_VALIDATOR_PATH", str(validator))

    settings = config.load_settings()

    assert settings.product_library == library.resolve()
    assert settings.validator_path == validator.resolve()
    assert settings.browser_headless is False


def test_default_product_paths_remain_workspace_relative(tmp_path, monkeypatch):
    monkeypatch.delenv("XIANYU_PRODUCT_LIBRARY_DIR", raising=False)
    monkeypatch.delenv("XIANYU_PRODUCT_VALIDATOR_PATH", raising=False)
    monkeypatch.setenv("XIANYU_MANAGER_DATA_DIR", str(tmp_path / "data"))

    settings = config.load_settings()

    assert settings.product_library == settings.project_root / "商品库"
    assert settings.validator_path == settings.project_root / "scripts" / "validate_product_library.py"


@pytest.mark.parametrize(
    ("name", "kind"),
    [("XIANYU_PRODUCT_LIBRARY_DIR", "目录"), ("XIANYU_PRODUCT_VALIDATOR_PATH", "文件")],
)
def test_missing_explicit_product_path_is_rejected(tmp_path, monkeypatch, name, kind):
    monkeypatch.setenv(name, str(tmp_path / "missing"))

    with pytest.raises(FileNotFoundError, match=name):
        config._configured_path(name, tmp_path / "fallback", kind=kind)
