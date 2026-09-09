"""Local ZIP safety, independent of seller publishing/asset quality rules."""
from pathlib import Path, PurePosixPath
import zipfile

from .scanner import sha256_file
from .fulfillment_rules import package_safety_fingerprint


def check_zip(package: Path) -> None:
    """Bounded, no extraction/execution. Same checks as folder intake."""
    try:
        with zipfile.ZipFile(package) as z:
            entries = z.infolist()
            if not entries or len(entries) > 10000 or sum(e.file_size for e in entries) > 1024**3:
                raise ValueError('ZIP内容为空或超过安全检查上限')
            for e in entries:
                path = PurePosixPath(e.filename)
                if path.is_absolute() or '..' in path.parts or '\\' in e.filename or ':' in e.filename or e.flag_bits & 1 or (e.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('ZIP含不安全路径、符号链接或加密文件')
                if e.file_size > max(e.compress_size, 1) * 200:
                    raise ValueError('ZIP压缩比例超过安全上限')
            if z.testzip() is not None:
                raise ValueError('ZIP完整性检查失败')
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError):
        raise ValueError('ZIP无法安全读取') from None


def check_registered_package(library: Path, product: dict) -> str:
    """Check the registered file, not an arbitrary upload or a trusted report."""
    name, zip_name = str(product.get('dir_name') or ''), str(product.get('zip_name') or '')
    if any(not v or v in {'.', '..'} or '/' in v or '\\' in v or ':' in v for v in (name, zip_name)):
        raise ValueError('DELIVERY_PACKAGE_SAFETY_UNCONFIRMED')
    folder = library / name
    package = folder / zip_name
    try:
        if folder.is_symlink() or package.is_symlink() or not package.is_file():
            raise ValueError('DELIVERY_PACKAGE_SAFETY_UNCONFIRMED')
        if package.stat().st_size != product.get('zip_size') or sha256_file(package) != product.get('zip_hash'):
            raise ValueError('DELIVERY_PACKAGE_VERSION_CHANGED')
        check_zip(package)
        if package.stat().st_size != product.get('zip_size') or sha256_file(package) != product.get('zip_hash'):
            raise ValueError('DELIVERY_PACKAGE_VERSION_CHANGED')
    except OSError:
        raise ValueError('DELIVERY_PACKAGE_SAFETY_UNCONFIRMED') from None
    return package_safety_fingerprint(product)
