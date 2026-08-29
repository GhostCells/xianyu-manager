from __future__ import annotations

import hashlib
import importlib.util
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

from .knowledge import knowledge_hash, sanitize_product_knowledge


PRODUCT_RE = re.compile(r"^(?P<number>\d{2})-(?P<name>.+)$")
PUBLISHING_ONLY_ERROR_MARKERS = (
    "编号不连续",
    "含不必要文件",
    "第一行标题超过 20 个字符",
)


@dataclass(frozen=True)
class ScannedProduct:
    dir_name: str
    number: int
    name: str
    title: str
    zip_name: str
    zip_hash: str
    zip_size: int
    image_count: int
    quality_status: str
    quality_errors: list[str]
    scanned_at: str
    knowledge_text: str = ""
    knowledge_hash: str = ""
    knowledge_chars: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_validator(path: Path) -> ModuleType:
    if not path.is_file():
        raise FileNotFoundError(f"找不到工作区校验器：{path}")
    spec = importlib.util.spec_from_file_location("xianyu_workspace_validator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载工作区校验器：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_title(copy_path: Path) -> str:
    if not copy_path.is_file():
        return ""
    text = copy_path.read_text(encoding="utf-8-sig").strip()
    return text.splitlines()[0].strip() if text else ""


def read_knowledge(copy_path: Path) -> str:
    if not copy_path.is_file():
        return ""
    return sanitize_product_knowledge(copy_path.read_text(encoding="utf-8-sig"))


def delivery_blocking_errors(errors: list[str]) -> list[str]:
    """Return issues that make the customer delivery itself unsafe or incomplete.

    Catalogue numbering, seller-side notes and an overlong listing title affect
    publishing quality, but they do not change an already verified customer ZIP
    or its Baidu share link.  Keeping those issues out of delivery readiness
    prevents valid paid orders from being stranded.
    """
    return [
        item
        for item in errors
        if not any(marker in item for marker in PUBLISHING_ONLY_ERROR_MARKERS)
    ]


def scan_product(path: Path, expected_number: int, validator: ModuleType) -> ScannedProduct:
    match = PRODUCT_RE.match(path.name)
    number = int(match.group("number")) if match else 0
    name = match.group("name") if match else path.name
    zip_files = sorted(item for item in path.iterdir() if item.is_file() and item.suffix.lower() == ".zip")
    zip_path = zip_files[0] if len(zip_files) == 1 else None
    image_dir = path / "图片"
    image_count = len([item for item in image_dir.iterdir() if item.is_file()]) if image_dir.is_dir() else 0
    knowledge_text = read_knowledge(path / "发布文案.txt")
    errors: list[str] = []
    # Delivery readiness belongs to the individual product.  A duplicate or
    # missing catalogue number is a library-maintenance problem and must not
    # make every later, otherwise valid, product impossible to deliver.
    # The standalone library validator still receives the positional number
    # and continues to report catalogue ordering problems.
    validator.validate_product(path, number or expected_number, errors)
    blocking_errors = delivery_blocking_errors(errors)
    return ScannedProduct(
        dir_name=path.name,
        number=number,
        name=name,
        title=read_title(path / "发布文案.txt"),
        zip_name=zip_path.name if zip_path else "",
        zip_hash=sha256_file(zip_path) if zip_path else "",
        zip_size=zip_path.stat().st_size if zip_path else 0,
        image_count=image_count,
        quality_status="passed" if not blocking_errors else "failed",
        quality_errors=errors,
        scanned_at=datetime.now(timezone.utc).isoformat(),
        knowledge_text=knowledge_text,
        knowledge_hash=knowledge_hash(knowledge_text),
        knowledge_chars=len(knowledge_text),
    )


def scan_library(root: Path, validator_path: Path) -> list[ScannedProduct]:
    if not root.is_dir():
        raise FileNotFoundError(f"商品库不存在：{root}")
    validator = load_validator(validator_path)
    products = sorted(item for item in root.iterdir() if item.is_dir())
    return [scan_product(path, index, validator) for index, path in enumerate(products, start=1)]
