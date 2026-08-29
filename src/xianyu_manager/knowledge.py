from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path


MAX_PRODUCT_KNOWLEDGE_CHARS = 16000
MAX_KNOWLEDGE_FILES = 80
MAX_KNOWLEDGE_FILE_BYTES = 2 * 1024 * 1024
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".jsonl",
    ".yaml", ".yml", ".xml", ".html", ".htm",
}
IGNORED_DIRECTORY_NAMES = {
    ".git", ".svn", "node_modules", "__pycache__", ".venv", "venv",
}

URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
SECRET_RE = re.compile(
    r"\b(?:sk|ak)-[A-Za-z0-9_-]{10,}\b|"
    r"\b(?:cookie|authorization|token)\s*[:=]\s*\S+",
    re.I,
)
CONTACT_RE = re.compile(
    r"(?:微信|微\s*信|vx|v信|qq|电话|手机号|邮箱)\s*[:：=]\s*\S+",
    re.I,
)
SHARE_CODE_LINE_RE = re.compile(
    r"(?:提取码|访问码|解压密码|网盘密码)\s*[:：=]?\s*[A-Za-z0-9]{2,12}",
    re.I,
)


@dataclass(frozen=True)
class KnowledgeFolderResult:
    source_path: str
    text: str
    content_hash: str
    chars: int
    file_count: int
    skipped_files: int
    warnings: tuple[str, ...]


def _read_text_file(path: Path) -> str:
    payload = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _read_pdf_file(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on optional runtime package
        raise RuntimeError("当前环境缺少 PDF 读取组件 pypdf") from exc
    reader = PdfReader(str(path))
    return "\n".join(str(page.extract_text() or "") for page in reader.pages)


def load_knowledge_folder(folder: Path | str) -> KnowledgeFolderResult:
    """Build a bounded, sanitized product knowledge corpus from a local folder."""
    source = Path(folder).expanduser()
    if not source.is_absolute():
        raise ValueError("请输入完整的本地文件夹地址")
    try:
        source = source.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ValueError("所选资料文件夹不存在或无法访问") from exc
    if not source.is_dir():
        raise ValueError("所选地址不是文件夹")

    candidates: list[Path] = []
    skipped_files = 0
    warnings: list[str] = []
    for current_root, directory_names, file_names in os.walk(source, followlinks=False):
        directory_names[:] = sorted(
            name for name in directory_names
            if name not in IGNORED_DIRECTORY_NAMES and not name.startswith(".")
        )
        root_path = Path(current_root)
        for file_name in sorted(file_names):
            path = root_path / file_name
            if file_name.startswith(".") or path.is_symlink():
                skipped_files += 1
                continue
            if path.suffix.lower() not in TEXT_EXTENSIONS | {".pdf"}:
                skipped_files += 1
                continue
            try:
                if path.stat().st_size > MAX_KNOWLEDGE_FILE_BYTES:
                    skipped_files += 1
                    warnings.append(f"已跳过过大的文件：{path.relative_to(source)}")
                    continue
            except OSError:
                skipped_files += 1
                continue
            candidates.append(path)

    if len(candidates) > MAX_KNOWLEDGE_FILES:
        skipped_files += len(candidates) - MAX_KNOWLEDGE_FILES
        warnings.append(f"最多读取 {MAX_KNOWLEDGE_FILES} 个文件，其余已跳过")
        candidates = candidates[:MAX_KNOWLEDGE_FILES]

    sections: list[str] = []
    loaded_files = 0
    for path in candidates:
        try:
            raw_text = _read_pdf_file(path) if path.suffix.lower() == ".pdf" else _read_text_file(path)
        except Exception as exc:  # a single corrupt file must not block the whole folder
            skipped_files += 1
            warnings.append(f"无法读取：{path.relative_to(source)}（{str(exc)[:80]}）")
            continue
        cleaned = sanitize_product_knowledge(raw_text, limit=MAX_PRODUCT_KNOWLEDGE_CHARS)
        if not cleaned:
            skipped_files += 1
            continue
        sections.append(f"【资料文件：{path.relative_to(source)}】\n{cleaned}")
        loaded_files += 1

    corpus = sanitize_product_knowledge("\n\n".join(sections))
    if not corpus:
        supported = "、".join(sorted(TEXT_EXTENSIONS | {".pdf"}))
        raise ValueError(f"文件夹内没有可用资料；支持的格式：{supported}")
    return KnowledgeFolderResult(
        source_path=str(source),
        text=corpus,
        content_hash=knowledge_hash(corpus),
        chars=len(corpus),
        file_count=loaded_files,
        skipped_files=skipped_files,
        warnings=tuple(warnings[:8]),
    )


def sanitize_product_knowledge(text: str, *, limit: int = MAX_PRODUCT_KNOWLEDGE_CHARS) -> str:
    """Return a compact product fact sheet with delivery secrets removed."""
    cleaned_lines: list[str] = []
    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"[\t\u00a0]+", " ", raw_line).strip()
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue
        if "pan.baidu.com" in line.lower() or SHARE_CODE_LINE_RE.search(line):
            continue
        line = URL_RE.sub("[链接已移除]", line)
        line = SECRET_RE.sub("[敏感信息已移除]", line)
        line = CONTACT_RE.sub("[联系方式已移除]", line)
        if line not in cleaned_lines[-2:]:
            cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "\n[商品资料已截断]"


def knowledge_hash(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evidence_is_supported(evidence: str, corpus: str) -> bool:
    normalized_evidence = re.sub(r"\s+", "", str(evidence or ""))
    normalized_corpus = re.sub(r"\s+", "", str(corpus or ""))
    return bool(
        normalized_evidence
        and len(normalized_evidence) <= 240
        and normalized_evidence in normalized_corpus
    )
