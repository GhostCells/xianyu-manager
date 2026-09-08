"""Bounded folder intake. No external IO, ZIP extraction, or delivery calls.

Uploads are private numbered blobs, never executable paths. Confirmation is a
separate operation; the existing trusted scanner decides quality. Old assets
and raw uploads remain outside the scanned product library.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import time
import uuid
import zipfile

from .fulfillment_rules import parse_listing_id
from .knowledge import load_knowledge_folder, TEXT_EXTENSIONS
from .scanner import load_validator, scan_product, sha256_file

MAX_FILES = 1000
MAX_FILE = 256 * 1024**2
MAX_TOTAL = 512 * 1024**2
MAX_STORAGE = 2 * 1024**3
TTL = 24 * 3600


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def safe_path(value):
    if not isinstance(value, str) or len(value) > 400 or '\\' in value or ':' in value:
        raise ValueError('导入路径不合法')
    parts = value.split('/')
    if any(not p or p in {'.', '..'} or p.startswith('.') or any(ord(c) < 32 for c in p) for p in parts):
        raise ValueError('不接受隐藏文件、上级路径或控制字符')
    return PurePosixPath(value)


def tree_hash(path):
    if path.is_symlink():
        raise ValueError('商品目录不能是符号链接')
    if not path.exists():
        return digest([])
    if not path.is_dir():
        raise ValueError('商品路径不是目录，不能覆盖')
    entries = []
    for item in sorted(path.rglob('*')):
        if item.is_symlink():
            raise ValueError('商品目录存在符号链接，不能覆盖')
        if item.is_file():
            entries.append((str(item.relative_to(path)), sha256_file(item)))
    return digest(entries)


class ProductImport:
    def __init__(self, database, settings):
        self.db, self.settings = database, settings
        self.root = settings.data_dir / 'product-imports'
        self.lock = threading.Lock()

    def _save(self, directory, job):
        temp = directory / 'job.tmp'
        temp.write_text(json.dumps(job, ensure_ascii=False))
        temp.chmod(0o600)
        os.replace(temp, directory / 'job.json')

    def _job(self, account_id, token, *, allow_expired=False):
        if not re.fullmatch('[0-9a-f]{32}', token):
            raise ValueError('导入编号不合法')
        directory = self.root / token
        try:
            job = json.loads((directory / 'job.json').read_text())
        except (OSError, ValueError):
            raise ValueError('导入记录不存在') from None
        if job['account_id'] != account_id:
            raise ValueError('导入账号不匹配')
        if not allow_expired and time.time() - job['created'] > TTL and job['phase'] != 'committed':
            raise ValueError('导入已过期，请取消后重新上传')
        return directory, job

    def _target(self, connection, account_id, item_id, name):
        listing = connection.execute('SELECT * FROM account_listings WHERE account_id=? AND item_id=? AND is_active=1', (account_id, item_id)).fetchone()
        if not listing:
            raise ValueError('请选择当前账号正常在售商品')
        if parse_listing_id(listing['listing_url']) != item_id:
            raise ValueError('在售商品链接与完整ID不一致')
        if listing['source_kind'] != 'platform_inventory' or json.loads(listing['source_text'] or '{}').get('itemStatus') != 0:
            raise ValueError('请先刷新在售列表，仅接受正常状态商品')
        matched = listing['matched_product_dir_name'] or ''
        if matched and matched != name:
            raise ValueError('已有精确映射必须保持，不可通过导入重新绑定')
        configured = connection.execute('SELECT * FROM account_products WHERE account_id=? AND product_dir_name=?', (account_id, name)).fetchone()
        if configured and parse_listing_id(configured['listing_url']) not in {'', item_id}:
            raise ValueError('本地商品已登记其他闲鱼商品')
        if connection.execute('SELECT 1 FROM account_products WHERE product_dir_name=? AND account_id<>?', (name, account_id)).fetchone():
            raise ValueError('商品被其他账号引用，不允许覆盖')
        if connection.execute('SELECT 1 FROM account_listings WHERE matched_product_dir_name=? AND (account_id<>? OR item_id<>?)', (name, account_id, item_id)).fetchone():
            raise ValueError('商品存在其他映射，请先人工处理')
        product = connection.execute('SELECT * FROM products WHERE dir_name=?', (name,)).fetchone()
        if product and product['catalog_status'] in {'legacy', 'listing_only'}:
            raise ValueError('不允许覆盖历史或独立交付商品')
        return dict(listing), dict(product) if product else None, dict(configured) if configured else None

    def start(self, account_id, item_id, name, files):
        if not re.fullmatch(r'\d{8,}', item_id) or not re.fullmatch(r'\d{2}-[^/\\:]+', name) or len(name) > 120:
            raise ValueError('请提供完整商品ID和“编号-商品名”目录名称')
        safe_path(name)
        if not 1 <= len(files) <= MAX_FILES:
            raise ValueError('单次最多1000个文件')
        seen, total, normalized = set(), 0, []
        folder = None
        for f in files:
            path = safe_path(f['path'])
            if len(path.parts) < 2:
                raise ValueError('请选择整个商品文件夹')
            folder = folder or path.parts[0]
            if path.parts[0] != folder or str(path).casefold() in seen:
                raise ValueError('只能导入一个文件夹，路径不可重复')
            seen.add(str(path).casefold())
            size = f['size']
            if type(size) is not int or not 0 <= size <= MAX_FILE:
                raise ValueError('单文件最大256MB')
            total += size
            normalized.append({'path': str(PurePosixPath(*path.parts[1:])), 'size': size})
        if total > MAX_TOTAL:
            raise ValueError('单次商品包最大512MB')
        with self.lock:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            for record in self.root.glob('*/job.json'):
                previous = json.loads(record.read_text())
                if previous.get('name') == name and previous.get('phase') in {'applying', 'needs_manual_recovery'}:
                    raise ValueError('该商品存在未完成导入，需先人工恢复')
            if len(list(self.root.glob('*/job.json'))) >= 64:
                raise ValueError('导入记录已达上限，请先整理暂存或归档')
            used = sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file())
            reserved = sum(json.loads(p.read_text()).get('total', 0) for p in self.root.glob('*/job.json'))
            if used + reserved + total * 3 > MAX_STORAGE or shutil.disk_usage(self.root).free < total * 3 + 1024**3:
                raise ValueError('导入存储空间不足，请先整理归档；不会自动删除旧版本')
            with self.db.connect() as c:
                listing, product, configured = self._target(c, account_id, item_id, name)
            current = self.settings.product_library / name
            job = {'account_id': account_id, 'item_id': item_id, 'name': name, 'folder': folder,
                   'title': listing['title'], 'baseline': digest([listing, product, configured]),
                   'disk_baseline': tree_hash(current), 'existing': bool(product), 'files': normalized,
                   'total': total, 'created': time.time(), 'phase': 'uploading'}
            token = uuid.uuid4().hex
            directory = self.root / token
            directory.mkdir(mode=0o700)
            self._save(directory, job)
            return {'import_id': token, 'product': name, 'item_id': item_id, 'existing': bool(product), 'file_count': len(files), 'bytes': total}

    async def upload(self, account_id, token, index, stream):
        directory, job = self._job(account_id, token)
        if job['phase'] != 'uploading' or not 0 <= index < len(job['files']):
            raise ValueError('当前导入不接受此文件')
        target, temp = directory / f'{index}.blob', directory / f'{index}.part'
        if target.exists():
            raise ValueError('文件已经上传，请勿重复')
        size, h = 0, hashlib.sha256()
        with self.lock:
            _, current = self._job(account_id, token)
            if current['phase'] != 'uploading' or target.exists():
                raise ValueError('导入状态已变化')
            out = temp.open('xb')
        try:
            with out:
                temp.chmod(0o600)
                async for chunk in stream:
                    size += len(chunk)
                    if size > job['files'][index]['size']:
                        raise ValueError('上传超过声明大小')
                    out.write(chunk)
                    h.update(chunk)
            if size != job['files'][index]['size']:
                raise ValueError('文件上传不完整')
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        return {'size': size, 'sha256': h.hexdigest()}

    def preview(self, account_id, token, zip_index):
        if zip_index is not None and type(zip_index) is not int:
            raise ValueError('ZIP选择编号无效')
        with self.lock:
            directory, job = self._job(account_id, token)
            if job['phase'] not in {'uploading', 'preview'}:
                raise ValueError('导入已确认或需要人工处理')
            for i, f in enumerate(job['files']):
                p = directory / f'{i}.blob'
                if not p.is_file() or p.stat().st_size != f['size']:
                    raise ValueError('文件尚未完整上传')
            packages = [{'index': i, 'path': f['path'], 'size': f['size']} for i, f in enumerate(job['files']) if PurePosixPath(f['path']).parts[0] == '客户交付' and f['path'].lower().endswith('.zip')]
            if zip_index is None:
                if len(packages) != 1:
                    return {'requires_zip_selection': True, 'packages': packages, 'message': '请选择客户交付中的一个ZIP' if packages else '客户交付目录中没有ZIP'}
                zip_index = packages[0]['index']
            if zip_index not in {p['index'] for p in packages}:
                raise ValueError('只能选择客户交付目录中的ZIP')
            package = directory / f'{zip_index}.blob'
            try:
                with zipfile.ZipFile(package) as z:
                    entries = z.infolist()
                    if not entries or len(entries) > 10000 or sum(e.file_size for e in entries) > 1024**3:
                        raise ValueError('ZIP内容为空或超过安全检查上限')
                    for e in entries:
                        # No extraction; still reject unsafe, encrypted or explosive archives.
                        path = PurePosixPath(e.filename)
                        if path.is_absolute() or '..' in path.parts or '\\' in e.filename or ':' in e.filename or e.flag_bits & 1 or (e.external_attr >> 16) & 0o170000 == 0o120000:
                            raise ValueError('ZIP含不安全路径、符号链接或加密文件')
                        if e.file_size > max(e.compress_size, 1) * 200:
                            raise ValueError('ZIP压缩比例超过安全上限')
                    if z.testzip() is not None:
                        raise ValueError('ZIP完整性检查失败')
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError):
                raise ValueError('ZIP无法安全读取') from None
            # Private candidates never appear under the scanned production library.
            candidate = directory / 'candidate' / job['name']
            if candidate.exists():
                shutil.rmtree(candidate)
            candidate.mkdir(parents=True, mode=0o700)
            knowledge_dir = candidate / '商品资料'
            knowledge_dir.mkdir()
            source_names = []
            for i, f in enumerate(job['files']):
                path = PurePosixPath(f['path'])
                if path.parts[0] == '商品资料' and path.suffix.lower() in TEXT_EXTENSIONS | {'.pdf'}:
                    dest = candidate.joinpath(*path.parts)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(directory / f'{i}.blob', dest)
                    source_names.append(f['path'])
                if len(path.parts) == 3 and path.parts[:2] in {('商品资料','商品图片'), ('商品资料','图片')} and path.suffix.lower() in {'.png','.jpg','.jpeg','.webp'}:
                    image_dir = candidate / '图片'
                    image_dir.mkdir(exist_ok=True)
                    dest = image_dir / path.name
                    if dest.exists():
                        raise ValueError('商品图片文件名冲突，请整理后重试')
                    shutil.copyfile(directory / f'{i}.blob', dest)
            knowledge = load_knowledge_folder(knowledge_dir)
            if not knowledge.text.strip():
                raise ValueError('商品资料未提取到有效知识，原生产资料未变更')
            zip_name = PurePosixPath(job['files'][zip_index]['path']).name
            shutil.copyfile(package, candidate / zip_name)
            (candidate / '发布文案.txt').write_text(knowledge.text, encoding='utf-8')
            # Never load an uploaded validator or trust 验收报告.txt as a pass.
            scanned = scan_product(candidate, int(job['name'][:2]), load_validator(self.settings.validator_path))
            job.update(phase='preview', zip_index=zip_index, candidate_hash=tree_hash(candidate), preview_id=uuid.uuid4().hex)
            self._save(directory, job)
            return {'preview_id': job['preview_id'], 'item_id': job['item_id'], 'title': job['title'], 'product': job['name'], 'existing': job['existing'], 'packages': packages,
                    'zip_name': zip_name, 'zip_hash': scanned.zip_hash, 'zip_size': scanned.zip_size,
                    'knowledge_sources': source_names, 'knowledge_chars': knowledge.chars,
                    'knowledge_preview': knowledge.text[:1600], 'knowledge_truncated': knowledge.chars > 1600,
                    'knowledge_warnings': list(knowledge.warnings), 'quality_status': scanned.quality_status,
                    'quality_errors': scanned.quality_errors, 'can_confirm': True,
                    'notice': '确认会替换运营资料、保留旧版本并撤销当前核验；不会修改分享、开启发货或修改允许范围'}

    def confirm(self, account_id, token, preview_id, accepted):
        if accepted is not True:
            raise ValueError('请明确确认导入及当前核验将失效')
        with self.lock:
            directory, job = self._job(account_id, token)
            if job['phase'] != 'preview' or preview_id != job.get('preview_id'):
                raise ValueError('预览已失效或已经执行，请勿重复确认')
            candidate = directory / 'candidate' / job['name']
            target = self.settings.product_library / job['name']
            if tree_hash(candidate) != job['candidate_hash'] or tree_hash(target) != job['disk_baseline']:
                raise ValueError('文件版本已变化，请重新导入预览')
            if self.root.stat().st_dev != self.settings.product_library.stat().st_dev:
                raise ValueError('归档和商品库需位于同一文件系统，暂不支持跨盘替换')
            scanned = scan_product(candidate, int(job['name'][:2]), load_validator(self.settings.validator_path))
            knowledge = load_knowledge_folder(candidate / '商品资料')
            with self.db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                listing, product, configured = self._target(c, account_id, job['item_id'], job['name'])
                if digest([listing, product, configured]) != job['baseline']:
                    raise ValueError('商品、映射或分享已变化，请重新导入预览')
                self.db._require_no_pending_delivery(c, job['name'])
                if c.execute("SELECT 1 FROM orders WHERE product_dir_name=? AND delivery_status IN ('sending','manual_review','confirm_pending')", (job['name'],)).fetchone():
                    raise ValueError('该商品有交付未决订单，先人工核对后再导入')
                before_file = directory / 'before.json'
                before_file.write_text(json.dumps({'product':product,'listing':listing,'account_product':configured},ensure_ascii=False))
                before_file.chmod(0o600)
                # Persist a fail-closed marker BEFORE filesystem replacement.
                if product:
                    c.execute("UPDATE products SET share_verified=0,share_needs_review=1,verified_fingerprint='',quality_status='unknown' WHERE dir_name=?", (job['name'],))
                expected = digest(self._target(c, account_id, job['item_id'], job['name']))
            job['phase'] = 'applying'
            self._save(directory, job)
            try:
                with self.db.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    if digest(self._target(c, account_id, job['item_id'], job['name'])) != expected:
                        raise ValueError('资料在确认过程中发生变化')
                    self.db._require_no_pending_delivery(c, job['name'])
                    if target.exists():
                        os.replace(target, directory / 'previous-product')
                    os.replace(candidate, target)
                    c.execute('''INSERT INTO products (dir_name,number,name,title,scanned_at) VALUES (?,?,?,?,?) ON CONFLICT(dir_name) DO NOTHING''',
                              (job['name'], scanned.number, scanned.name, job['title'], scanned.scanned_at))
                    c.execute('''UPDATE products SET zip_name=?,zip_hash=?,zip_size=?,image_count=?,quality_status=?,quality_errors_json=?,scanned_at=?,
                        knowledge_text=?,knowledge_hash=?,knowledge_chars=?,knowledge_source_path=?,knowledge_file_count=?,knowledge_updated_at=CURRENT_TIMESTAMP,
                        share_verified=0,share_needs_review=1,verified_fingerprint='',updated_at=CURRENT_TIMESTAMP WHERE dir_name=?''',
                              (scanned.zip_name,scanned.zip_hash,scanned.zip_size,scanned.image_count,scanned.quality_status,json.dumps(scanned.quality_errors,ensure_ascii=False),scanned.scanned_at,
                               knowledge.text,knowledge.content_hash,knowledge.chars,str(target/'商品资料'),knowledge.file_count,job['name']))
                    # Explicitly selected listing only; never fuzzy-match or touch other mappings.
                    if not configured:
                        c.execute('''INSERT INTO account_products(account_id,product_dir_name,enabled,listing_url,listing_status) VALUES(?,?,1,?,'published')''',
                                  (account_id,job['name'],listing['listing_url']))
                    c.execute('UPDATE account_listings SET matched_product_dir_name=? WHERE account_id=? AND item_id=?', (job['name'],account_id,job['item_id']))
                    self.db._log(c, 'product_import_confirmed', job['name'], {'import_id':token,'account_id':account_id,'item_id':job['item_id'],'zip_hash':scanned.zip_hash})
                job['phase'] = 'committed'
                self._save(directory, job)
            except Exception:
                job['phase'] = 'needs_manual_recovery'
                self._save(directory, job)
                raise ValueError('导入未完成，已保留原包和旧版本；该商品需人工检查，禁止自动核验') from None
            return {'product': job['name'], 'quality_status': scanned.quality_status, 'verified': False, 'import_id': token, 'committed': True}

    def cancel(self, account_id, token):
        with self.lock:
            directory, job = self._job(account_id, token, allow_expired=True)
            if job['phase'] not in {'uploading', 'preview'}:
                raise ValueError('已执行导入不能通过取消删除归档')
            if list(directory.glob('*.part')):
                raise ValueError('上传尚未停止')
            shutil.rmtree(directory)
            return {'cancelled': True}
