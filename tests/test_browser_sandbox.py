import ast
from pathlib import Path
import pytest
from xianyu_manager import browser_launch


@pytest.mark.parametrize('platform,expected', [('linux', {'chromium_sandbox': True}), ('win32', {}), ('darwin', {})])
def test_explicit_linux_sandbox(monkeypatch, platform, expected):
    monkeypatch.setattr(browser_launch.sys, 'platform', platform)
    assert browser_launch.sandbox_options() == expected


def test_all_runtime_persistent_launches_use_policy():
    root = Path(__file__).resolve().parents[1] / 'src/xianyu_manager'
    count = 0
    for name in ['session.py', 'delivery.py']:
        for node in ast.walk(ast.parse((root/name).read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'launch_persistent_context':
                assert any(k.arg is None and isinstance(k.value, ast.Call) and isinstance(k.value.func, ast.Name) and k.value.func.id == 'sandbox_options' for k in node.keywords)
                count += 1
    assert count == 4
