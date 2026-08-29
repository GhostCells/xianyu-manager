from __future__ import annotations

from xianyu_manager.knowledge import (
    MAX_PRODUCT_KNOWLEDGE_CHARS,
    evidence_is_supported,
    load_knowledge_folder,
    sanitize_product_knowledge,
)


def test_product_knowledge_removes_delivery_secrets_and_contacts():
    cleaned = sanitize_product_knowledge(
        """AI漫剧分镜表生成器
支持 Windows 10/11 64 位。
链接：https://pan.baidu.com/s/example
提取码：9r1v
API token=sk-super-secret-value
微信：seller123
不包含第三方 API 额度。
"""
    )

    assert "Windows 10/11" in cleaned
    assert "不包含第三方 API 额度" in cleaned
    assert "pan.baidu.com" not in cleaned
    assert "9r1v" not in cleaned
    assert "sk-super-secret-value" not in cleaned
    assert "seller123" not in cleaned


def test_product_knowledge_is_bounded_and_evidence_must_be_verbatim():
    cleaned = sanitize_product_knowledge("功能说明\n" + "可编辑分镜表。" * 2000)
    assert len(cleaned) <= MAX_PRODUCT_KNOWLEDGE_CHARS + 20
    assert evidence_is_supported("可编辑分镜表", cleaned)
    assert not evidence_is_supported("可以直接生成视频", cleaned)


def test_load_knowledge_folder_recurses_and_removes_secrets(tmp_path):
    (tmp_path / "介绍.md").write_text("支持 SEO 五步流程。\n提取码：9r1v", encoding="utf-8")
    detail = tmp_path / "教程"
    detail.mkdir()
    (detail / "FAQ.txt").write_text(
        "适合新手。\nhttps://pan.baidu.com/s/example\nAPI token=sk-super-secret-value",
        encoding="utf-8",
    )
    (detail / "image.png").write_bytes(b"not an image")

    result = load_knowledge_folder(tmp_path)

    assert result.file_count == 2
    assert result.skipped_files == 1
    assert "SEO 五步流程" in result.text
    assert "适合新手" in result.text
    assert "9r1v" not in result.text
    assert "pan.baidu.com" not in result.text
    assert "sk-super-secret-value" not in result.text
    assert result.chars == len(result.text)
