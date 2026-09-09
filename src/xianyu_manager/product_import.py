"""Bounded folder intake. No external IO, ZIP extraction, or delivery calls.

Uploads are private numbered blobs, never executable paths. Confirmation is a
separate operation; intake records metadata, not product quality. Old assets
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
from datetime import datetime, timezone

from .fulfillment_rules import parse_listing_id, package_safety_fingerprint
from .delivery_package import check_zip, check_registered_package, tree_hash
from .product_ownership import has_foreign_product_use
from .knowledge import load_knowledge_folder, TEXT_EXTENSIONS, KnowledgeFolderResult
from .scanner import ScannedProduct, sha256_file

MAX_FILES = 20000
MAX_TOTAL = 1024**3
MAX_FILE = MAX_TOTAL
# Enough room for a full-size intake plus its private candidate; still bounded.
MAX_STORAGE = 4 * 1024**3
MAX_MANIFEST_BYTES = 32 * 1024**2
TTL = 24 * 3600


def delivery_zip_path(value):
    path = PurePosixPath(value)
    return path.suffix.lower() == '.zip' and (len(path.parts) == 1 or path.parts[0] == '客户交付')


def import_knowledge(folder):
    # ZIP-only imports must not manufacture knowledge or read ZIP contents.
    if not any(p.suffix.lower() in TEXT_EXTENSIONS | {'.pdf'} for p in folder.rglob('*') if p.is_file()):
        return KnowledgeFolderResult(str(folder), '', '', 0, 0, 0, ())
    return load_knowledge_folder(folder)


class ImportStorageNotReady(ValueError):
    """Raised only before an import can mutate product files/rows."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def safe_path(value):
    if not isinstance(value, str) or len(value) > 400 or '\\' in value or ':' in value:
        raise ValueError('导入路径不合法')
    parts = value.split('/')
    if any(not p or p in {'.', '..'} or p.startswith('.') or any(ord(c) < 32 for c in p) for p in parts):
        raise ValueError('不接受隐藏文件、上级路径或控制字符')
    return PurePosixPath(value)


def import_metadata(candidate, zip_name):
    """Register uploaded assets without invoking the publishing/quality validator.

    Archive safety is checked during preview and its bytes are bound by tree_hash.
    Unknown is deliberate: upload is neither quality approval nor share verification.
    """
    package = candidate / zip_name
    images = candidate / '图片'
    return ScannedProduct(
        dir_name=candidate.name, number=int(candidate.name[:2]), name=candidate.name[3:],
        title='', zip_name=zip_name, zip_hash=sha256_file(package) if zip_name else '', zip_size=package.stat().st_size if zip_name else 0,
        image_count=sum(p.is_file() for p in images.iterdir()) if images.is_dir() else 0,
        quality_status='unknown', quality_errors=[], scanned_at=datetime.now(timezone.utc).isoformat(),
    )


class ProductImport:
    def __init__(self, database, settings):
        self.db, self.settings = database, settings
        self.root = settings.data_dir / 'product-imports'
        self.lock = threading.Lock()

    def _prepare_destination(self, name):
        """No privilege escalation. Repair only our own directory's owner bits."""
        library = self.settings.product_library
        target = library / name
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise ImportStorageNotReady('商品目录状态异常，尚未上传或替换；可关闭窗口，原资料保留')
        if target.exists() and not os.access(target, os.W_OK | os.X_OK):
            # Legacy root-owned directories are provisioned once by deployment,
            # not by a privileged web endpoint. Never chmod another owner's files.
            if hasattr(os, 'getuid') and target.stat().st_uid == os.getuid():
                try:
                    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        st = os.fstat(fd)
                        if st.st_uid == os.getuid():
                            os.fchmod(fd, (st.st_mode & 0o777) | 0o700)
                    finally:
                        os.close(fd)
                except OSError:
                    pass
            if not os.access(target, os.W_OK | os.X_OK):
                raise ImportStorageNotReady('商品存储尚未就绪，尚未替换资料；可关闭窗口，原资料保留')
        if not os.access(library, os.W_OK | os.X_OK) or not os.access(self.root, os.W_OK | os.X_OK):
            raise ImportStorageNotReady('商品存储不可写，尚未替换资料；可关闭窗口，原资料保留')
        if self.root.stat().st_dev != library.stat().st_dev:
            raise ImportStorageNotReady('归档和商品库需位于同一文件系统，尚未替换资料')

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
        if has_foreign_product_use(connection, name, account_id):
            raise ValueError('商品仍有其他账号的有效关联或历史业务记录，不允许覆盖；已归档的空草稿不会阻断')
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
            raise ValueError('单次最多20000个文件')
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
                raise ValueError('单文件最大1GiB（1024MiB）')
            total += size
            normalized.append({'path': str(PurePosixPath(*path.parts[1:])), 'size': size})
        if total > MAX_TOTAL:
            raise ValueError('单次商品包最大1GiB（1024MiB）')
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
            self._prepare_destination(name)
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

    def preview(self, account_id, token, zip_index, delivery_kind='zip'):
        if delivery_kind not in ('zip', 'cloud') or (delivery_kind == 'cloud' and zip_index is not None):
            raise ValueError('请选择明确的交付方式')
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
            packages = [{'index': i, 'path': f['path'], 'size': f['size']} for i, f in enumerate(job['files']) if delivery_zip_path(f['path'])]
            if zip_index is None and delivery_kind == 'zip':
                if len(packages) != 1:
                    return {'requires_zip_selection': True, 'packages': packages, 'message': '请选择客户交付中的一个ZIP，或选择网盘资料交付' if packages else '没有交付ZIP；教程等商品可选择“网盘资料交付（无ZIP）”，再生成预览'}
                zip_index = packages[0]['index']
            if delivery_kind == 'zip' and zip_index not in {p['index'] for p in packages}:
                raise ValueError('只能选择商品根目录或客户交付目录中的ZIP')
            if delivery_kind == 'zip':
                package = directory / f'{zip_index}.blob'
                check_zip(package)
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
            knowledge = import_knowledge(knowledge_dir)
            if not knowledge.text:
                with self.db.connect() as c:
                    _, existing, _ = self._target(c, account_id, job['item_id'], job['name'])
                text = (existing or {}).get('knowledge_text') or ''
                knowledge = KnowledgeFolderResult('', text, '', len(text), 0, 0,
                    ('未提供商品知识：保留该商品已有知识。' if text else '暂无自动回复知识；交付资料可以导入，需另补知识才能用于商品问答。',))
            zip_name = PurePosixPath(job['files'][zip_index]['path']).name if delivery_kind == 'zip' else ''
            if zip_name:
                shutil.copyfile(package, candidate / zip_name)
            (candidate / '发布文案.txt').write_text(knowledge.text, encoding='utf-8')
            # Neither uploaded reports nor the old publishing validator approve quality.
            scanned = import_metadata(candidate, zip_name)
            job.update(phase='preview', zip_index=zip_index, delivery_kind=delivery_kind, candidate_hash=tree_hash(candidate), preview_id=uuid.uuid4().hex, validation_scope='upload_safety_only')
            self._save(directory, job)
            return {'preview_id': job['preview_id'], 'item_id': job['item_id'], 'title': job['title'], 'product': job['name'], 'existing': job['existing'], 'packages': packages,
                    'zip_name': zip_name, 'zip_hash': scanned.zip_hash, 'zip_size': scanned.zip_size,
                    'delivery_kind': delivery_kind, 'delivery_revision': job['candidate_hash'] if delivery_kind == 'cloud' else '',
                    'knowledge_sources': source_names, 'knowledge_chars': knowledge.chars,
                    'knowledge_preview': knowledge.text[:1600], 'knowledge_truncated': knowledge.chars > 1600,
                    'knowledge_warnings': list(knowledge.warnings), 'quality_status': scanned.quality_status, 'validation_scope':'upload_safety_only',
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
            self._prepare_destination(job['name'])
            kind = job.get('delivery_kind', 'zip')
            zip_name = PurePosixPath(job['files'][job['zip_index']]['path']).name if kind == 'zip' else ''
            scanned = import_metadata(candidate, zip_name)
            knowledge = import_knowledge(candidate / '商品资料')
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
                    c.execute("UPDATE products SET share_verified=0,share_needs_review=1,verified_fingerprint='',delivery_safety_fingerprint='',quality_status='unknown' WHERE dir_name=?", (job['name'],))
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
                        share_verified=0,share_needs_review=1,verified_fingerprint='',delivery_safety_fingerprint=?,delivery_kind=?,delivery_revision=?,updated_at=CURRENT_TIMESTAMP WHERE dir_name=?''',
                              (scanned.zip_name,scanned.zip_hash,scanned.zip_size,scanned.image_count,scanned.quality_status,json.dumps(scanned.quality_errors,ensure_ascii=False),scanned.scanned_at,
                               knowledge.text,knowledge.content_hash,knowledge.chars,str(target/'商品资料'),knowledge.file_count,package_safety_fingerprint(scanned.to_dict()) if kind == 'zip' else '',kind,job['candidate_hash'] if kind == 'cloud' else '',job['name']))
                    if not knowledge.text and product:
                        # Retain the cached corpus without pointing at replaced files.
                        c.execute('''UPDATE products SET knowledge_text=?,knowledge_hash=?,knowledge_chars=?,knowledge_file_count=?,knowledge_updated_at=?,knowledge_source_path='' WHERE dir_name=?''',
                                  (product.get('knowledge_text') or '',product.get('knowledge_hash') or '',product.get('knowledge_chars') or 0,product.get('knowledge_file_count') or 0,product.get('knowledge_updated_at'),job['name']))
                    # Explicitly selected listing only; never fuzzy-match or touch other mappings.
                    if not configured:
                        c.execute('''INSERT INTO account_products(account_id,product_dir_name,enabled,listing_url,listing_status) VALUES(?,?,1,?,'published')''',
                                  (account_id,job['name'],listing['listing_url']))
                    c.execute('UPDATE account_listings SET matched_product_dir_name=? WHERE account_id=? AND item_id=?', (job['name'],account_id,job['item_id']))
                    self.db._log(c, 'product_import_confirmed', job['name'], {'import_id':token,'account_id':account_id,'item_id':job['item_id'],'zip_hash':scanned.zip_hash})
                job['phase'] = 'committed'
                self._save(directory, job)
            except Exception as exc:
                job['phase'] = 'needs_manual_recovery'
                # Safe diagnostics only: no paths, SQL values, share links or exception text.
                job['failure'] = {'type':type(exc).__name__, 'errno':getattr(exc,'errno',None)}
                self._save(directory, job)
                raise ValueError('导入未完成，已保留原包和旧版本；该商品需人工检查，禁止自动核验') from None
            return {'product': job['name'], 'quality_status': scanned.quality_status, 'verified': False, 'import_id': token, 'committed': True}

    def renew_committed_package_safety(self, account_id, token):
        """Explicit maintenance for pre-receipt imports. No share verification.

        Only a fully committed, unchanged import can acquire the safety receipt.
        No scan, mapping update, import replay, order processing or auto approval.
        """
        if self.db.safe_mode or self.db.prepare_mode:
            raise ValueError('SAFE_MODE_OPERATION_BLOCKED')
        if self.db.runtime_account_id is not None and self.db.runtime_account_id != account_id:
            raise ValueError('IMPORT_ACCOUNT_MISMATCH')
        with self.lock:
            directory, job = self._job(account_id, token, allow_expired=True)
            if job['phase'] != 'committed':
                raise ValueError('IMPORT_NOT_COMMITTED')
            if job.get('delivery_kind', 'zip') != 'zip':
                raise ValueError('IMPORT_NOT_ZIP_DELIVERY')
            target = self.settings.product_library / job['name']
            if tree_hash(target) != job['candidate_hash']:
                raise ValueError('IMPORTED_ASSETS_CHANGED')
            with self.db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                _, product, _ = self._target(c, account_id, job['item_id'], job['name'])
                if not product:
                    raise ValueError('PRODUCT_NOT_FOUND')
                self.db._require_no_pending_delivery(c, job['name'])
                selected = job['files'][job['zip_index']]
                blob = directory / f"{job['zip_index']}.blob"
                if (not delivery_zip_path(selected['path'])
                        or PurePosixPath(selected['path']).name != product['zip_name']
                        or selected['size'] != product['zip_size']
                        or sha256_file(blob) != product['zip_hash']):
                    raise ValueError('IMPORTED_PACKAGE_MISMATCH')
                receipt = check_registered_package(self.settings.product_library, product)
                c.execute('UPDATE products SET delivery_safety_fingerprint=? WHERE dir_name=?', (receipt, job['name']))
                self.db._log(c, 'import_package_safety_checked', job['name'], {'import_id':token, 'account_id':account_id, 'zip_hash':product['zip_hash']})
            return {'product':job['name'], 'safety_checked':True, 'share_verification_changed':False}

    def cancel(self, account_id, token):
        with self.lock:
            directory, job = self._job(account_id, token, allow_expired=True)
            if job['phase'] not in {'uploading', 'preview'}:
                raise ValueError('已执行导入不能通过取消删除归档')
            if list(directory.glob('*.part')):
                raise ValueError('上传尚未停止')
            shutil.rmtree(directory)
            return {'cancelled': True}
