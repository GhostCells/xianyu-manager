from __future__ import annotations

from xianyu_manager.config import load_settings
from xianyu_manager.scanner import delivery_blocking_errors, scan_library, scan_product


def test_current_product_library_passes():
    settings = load_settings()
    products = scan_library(settings.product_library, settings.validator_path)
    assert len(products) >= 19
    assert all(product.quality_status == "passed" for product in products)
    assert all(product.knowledge_chars > 0 for product in products)
    assert all(product.knowledge_hash for product in products)
    assert products[0].number == 1


def test_catalogue_sequence_problem_does_not_block_individual_delivery(tmp_path):
    product = tmp_path / "22-测试商品"
    product.mkdir()

    class RecordingValidator:
        received_number = None

        @classmethod
        def validate_product(cls, _path, expected_number, _errors):
            cls.received_number = expected_number

    scanned = scan_product(product, 23, RecordingValidator)

    assert RecordingValidator.received_number == 22
    assert scanned.number == 22
    assert scanned.quality_status == "passed"


def test_seller_side_publishing_warnings_do_not_block_delivery():
    errors = [
        "商品: 编号不连续，应为 21",
        "商品: 含不必要文件：验收报告.txt",
        "发布文案.txt: 第一行标题超过 20 个字符（28）",
        "交付包.zip: 压缩包成员损坏：教程.pdf",
    ]

    assert delivery_blocking_errors(errors) == [
        "交付包.zip: 压缩包成员损坏：教程.pdf"
    ]
