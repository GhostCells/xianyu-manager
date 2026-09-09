import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from xianyu_manager.database import Database
from xianyu_manager.product_import import ProductImport, safe_path, MAX_FILE, MAX_TOTAL, MAX_FILES


def zipped(name='customer/readme.txt'):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr(name, 'synthetic delivery only')
    return data.getvalue()


@pytest.fixture
def intake(tmp_path):
    db = Database(tmp_path / 'db.sqlite')
    db.ensure_default_accounts()
    a = db.get_active_account()['id']
    with db.connect() as c:
        c.execute('''INSERT INTO account_listings(account_id,item_id,title,listing_url,source_kind,source_text)
            VALUES(?, '123456789', 'synthetic', 'https://www.goofish.com/item?id=123456789', 'platform_inventory', '{"itemStatus":0}')''', (a,))
    library = tmp_path / 'library'
    library.mkdir()
    validator = tmp_path / 'validator.py'
    validator.write_text('def validate_product(path, number, errors):\n    pass\n')
    importer = ProductImport(db, SimpleNamespace(data_dir=tmp_path/'data',product_library=library,validator_path=validator))
    return importer, a


def uploaded(intake, extra=None):
    imp, a = intake
    data = {'45-demo/商品资料/说明.txt': '仅支持Mac。token=secret_value 隐私\nhttps://example.invalid'.encode(), '45-demo/客户交付/delivery.zip': zipped(), '45-demo/制作源文件/private.py': b'raise RuntimeError("never execute")'}
    data.update(extra or {})
    job = imp.start(a,'123456789','45-demo',[{'path':k,'size':len(v)} for k,v in data.items()])
    async def upload():
        for i, value in enumerate(data.values()):
            async def chunks():
                yield value
            await imp.upload(a,job['import_id'],i,chunks())
    asyncio.run(upload())
    return job['import_id']


def test_preview_then_confirm_no_auto_verification(intake):
    imp, a = intake
    token = uploaded(intake)
    r = imp.preview(a,token,None)
    assert 'secret_value' not in r['knowledge_preview']
    assert 'example.invalid' not in r['knowledge_preview']
    assert imp.db.get_product('45-demo') is None
    assert not (imp.settings.product_library/'45-demo').exists()
    with imp.db.connect() as c:
        before = {t:[tuple(row) for row in c.execute('SELECT * FROM '+t)] for t in ('accounts','orders','auto_reply_settings','automation_outbound_events')}
    result = imp.confirm(a,token,r['preview_id'],True)
    assert result['committed'] and not result['verified']
    p = imp.db.get_product('45-demo')
    assert p['zip_name']=='delivery.zip' and p['quality_status']=='unknown'
    from xianyu_manager.fulfillment_rules import delivery_issues
    assert 'QUALITY_BLOCKED' not in delivery_issues(p)
    assert p['delivery_safety_fingerprint']
    assert not p['share_verified'] and p['share_needs_review'] and not p['verified_fingerprint']
    assert imp.db.get_product_by_listing_item_id('123456789',a)['dir_name']=='45-demo'
    assert len(list((imp.settings.product_library/'45-demo').rglob('*.zip')))==1
    assert not list((imp.settings.product_library/'45-demo').rglob('*.py'))
    with imp.db.connect() as c:
        for t, rows in before.items():assert [tuple(row) for row in c.execute('SELECT * FROM '+t)]==rows
    with pytest.raises(ValueError):imp.confirm(a,token,r['preview_id'],True)


@pytest.mark.parametrize('problem', ['file_target', 'listing_url', 'unfinished_import'])
def test_unsafe_existing_state_blocks_import(intake, problem):
    imp, a = intake
    if problem == 'file_target':
        (imp.settings.product_library / '45-demo').write_text('do not replace')
    elif problem == 'listing_url':
        with imp.db.connect() as c:
            c.execute("UPDATE account_listings SET listing_url='https://www.goofish.com/item?id=999999999'")
    else:
        token = uploaded(intake)
        directory, job = imp._job(a, token)
        job['phase'] = 'needs_manual_recovery'
        imp._save(directory, job)
    with pytest.raises(ValueError):
        uploaded(intake)


def test_update_preserves_share_mapping_and_old_files(intake):
    imp,a=intake
    t=uploaded(intake);p=imp.preview(a,t,None);imp.confirm(a,t,p['preview_id'],True)
    imp.db.update_product('45-demo',{'share_url':'https://pan.baidu.com/s/synthetic','share_code':'test'})
    # Safety approval is separate from quality, and still requires a human share confirmation.
    p=imp.db.get_product('45-demo');imp.db.confirm_product_share('45-demo',p['fulfillment_fingerprint'])
    with imp.db.connect() as c:before=[tuple(r) for r in c.execute('SELECT * FROM account_products')]
    t=uploaded(intake);p=imp.preview(a,t,None);imp.confirm(a,t,p['preview_id'],True)
    p=imp.db.get_product('45-demo')
    assert p['share_url']=='https://pan.baidu.com/s/synthetic' and p['share_code']=='test'
    assert not p['share_verified'] and not p['verified_fingerprint']
    assert (imp.root/t/'previous-product'/'delivery.zip').is_file()
    with imp.db.connect() as c:assert [tuple(r) for r in c.execute('SELECT * FROM account_products')]==before


@pytest.mark.parametrize('path',['/etc/passwd','x/../passwd','x/./a','x//a','x/\\a','x/.env','x/a\n','C:/a','x/.git/config'])
def test_paths_rejected(path):
    with pytest.raises(ValueError):safe_path(path)


@pytest.mark.parametrize('files',[
    [], [{'path':'x/a','size':MAX_FILE+1}], [{'path':'x/a','size':-1}],
    [{'path':'x/a','size':0},{'path':'x/A','size':0}],
    [{'path':'x/a','size':0},{'path':'y/b','size':0}],
    [{'path':'x/a','size':True}],
])
def test_manifest_rejected(intake,files):
    imp,a=intake
    with pytest.raises(ValueError):imp.start(a,'123456789','45-demo',files)


@pytest.mark.parametrize('count', [20000, 20001])
def test_file_count_boundary(intake,count):
    imp,a=intake
    files=[{'path':f'45-demo/资料/{i}.txt','size':0} for i in range(count)]
    if count == MAX_FILES:
        assert imp.start(a,'123456789','45-demo',files)['file_count']==count
    else:
        with pytest.raises(ValueError,match='20000'):imp.start(a,'123456789','45-demo',files)


@pytest.mark.parametrize('extra', [0, 1])
def test_total_gib_boundary_without_large_files(intake,monkeypatch,extra):
    import xianyu_manager.product_import as module
    monkeypatch.setattr(module.shutil,'disk_usage',lambda _:SimpleNamespace(free=10*1024**3))
    imp,a=intake
    files=[{'path':'45-demo/客户交付/a.zip','size':MAX_TOTAL}, {'path':'45-demo/说明.txt','size':extra}]
    if extra:
        with pytest.raises(ValueError,match='1GiB'):imp.start(a,'123456789','45-demo',files)
    else:
        assert imp.start(a,'123456789','45-demo',files)['bytes']==1024**3


def test_large_import_still_requires_free_disk(intake,monkeypatch):
    import xianyu_manager.product_import as module
    monkeypatch.setattr(module.shutil,'disk_usage',lambda _:SimpleNamespace(free=3*1024**3))
    imp,a=intake
    with pytest.raises(ValueError,match='存储空间不足'):
        imp.start(a,'123456789','45-demo',[{'path':'45-demo/a','size':MAX_TOTAL}])


def test_multiple_zip_requires_choice(intake):
    imp,a=intake;t=uploaded(intake,{'45-demo/客户交付/second.zip':zipped()})
    r=imp.preview(a,t,None)
    assert r['requires_zip_selection'] and len(r['packages'])==2
    with pytest.raises(ValueError):imp.confirm(a,t,'fake',True)
    r=imp.preview(a,t,r['packages'][1]['index']);assert r['zip_name']=='second.zip'


@pytest.mark.parametrize('filename',['../escape','/absolute','x\\evil','customer/../escape'])
def test_zip_unsafe(intake,filename):
    imp,a=intake;t=uploaded(intake,{'45-demo/客户交付/delivery.zip':zipped(filename)})
    with pytest.raises(ValueError):imp.preview(a,t,None)
    assert imp.db.get_product('45-demo') is None


def test_wrong_account_and_missing_upload(intake):
    imp,a=intake
    t=imp.start(a,'123456789','45-demo',[{'path':'45-demo/客户交付/a.zip','size':1}])['import_id']
    with pytest.raises(ValueError):imp.preview(a+1,t,None)
    with pytest.raises(ValueError):imp.preview(a,t,None)
    async def short():
        if False:yield b''
    with pytest.raises(ValueError):asyncio.run(imp.upload(a,t,0,short()))
    assert not list((imp.root/t).glob('*.blob'))


@pytest.mark.parametrize('change',['share','mapping','disk','candidate'])
def test_stale_preview_blocks(intake,change):
    imp,a=intake;t=uploaded(intake);p=imp.preview(a,t,None)
    if change=='mapping':
        with imp.db.connect() as c:c.execute("UPDATE account_listings SET is_active=0")
    elif change=='share':
        with imp.db.connect() as c:c.execute("UPDATE account_listings SET title='changed'")
    elif change=='disk':
        dest=imp.settings.product_library/'45-demo';dest.mkdir();(dest/'extra').write_text('changed')
    else:(imp.root/t/'candidate'/'45-demo'/'delivery.zip').write_bytes(b'changed')
    with pytest.raises(ValueError):imp.confirm(a,t,p['preview_id'],True)


def test_quality_not_forged_and_cancel(intake):
    imp,a=intake
    imp.settings.validator_path.write_text("def validate_product(path, number, errors):\n    errors.append('synthetic quality failure')\n")
    t=uploaded(intake,{'45-demo/验收报告.txt':'全部通过'.encode()});p=imp.preview(a,t,None)
    assert p['quality_status']=='unknown'
    assert p['quality_errors']==[]
    assert p['validation_scope']=='upload_safety_only'
    assert imp.db.get_product('45-demo') is None
    imp.cancel(a,t);assert not (imp.root/t).exists()


@pytest.mark.parametrize('image_count', [0, 4, 7])
def test_upload_ignores_publishing_rules_and_never_loads_validator(intake,image_count):
    imp,a=intake
    imp.settings.validator_path.write_text("raise AssertionError('validator must not execute during intake')")
    extra={f'45-demo/商品资料/商品图片/arbitrary-{i}.png':b'opaque upload' for i in range(image_count)}
    t=uploaded(intake,extra);p=imp.preview(a,t,None)
    assert p['quality_status']=='unknown' and p['quality_errors']==[]
    result=imp.confirm(a,t,p['preview_id'],True)
    assert result['committed'] and result['quality_status']=='unknown'
    product=imp.db.get_product('45-demo')
    assert product['knowledge_chars']>0 and product['image_count']==image_count
    assert not product['share_verified'] and not product['verified_fingerprint']


def test_existing_unwritable_directory_rejected_before_mutation(intake,monkeypatch):
    imp,a=intake;t=uploaded(intake);p=imp.preview(a,t,None);imp.confirm(a,t,p['preview_id'],True)
    t=uploaded(intake);p=imp.preview(a,t,None)
    before=imp.db.get_product('45-demo')
    monkeypatch.setattr('xianyu_manager.product_import.os.access',lambda *args:False)
    with pytest.raises(ValueError,match='存储尚未就绪'):imp.confirm(a,t,p['preview_id'],True)
    assert imp.db.get_product('45-demo')==before
    assert json.loads((imp.root/t/'job.json').read_text())['phase']=='preview'


def test_storage_failure_before_upload_creates_no_job(intake,monkeypatch):
    imp,a=intake
    monkeypatch.setattr('xianyu_manager.product_import.os.access',lambda *args:False)
    with pytest.raises(ValueError,match='存储不可写'):uploaded(intake)
    assert not list(imp.root.glob('*/job.json'))
    assert not list(imp.root.rglob('*.blob'))


def test_own_directory_permission_repaired_without_touching_contents(intake):
    imp,a=intake;t=uploaded(intake);p=imp.preview(a,t,None);imp.confirm(a,t,p['preview_id'],True)
    target=imp.settings.product_library/'45-demo'
    before=(target/'delivery.zip').read_bytes();old_mode=(target/'delivery.zip').stat().st_mode
    target.chmod(0o500)
    try:
        t=uploaded(intake);p=imp.preview(a,t,None);imp.confirm(a,t,p['preview_id'],True)
        assert (imp.root/t/'previous-product'/'delivery.zip').read_bytes()==before
        assert (imp.root/t/'previous-product'/'delivery.zip').stat().st_mode==old_mode
    finally:
        if target.exists():target.chmod(0o700)


def test_foreign_owner_is_not_chmodded_by_web_process(intake,monkeypatch):
    imp,a=intake;t=uploaded(intake);p=imp.preview(a,t,None);imp.confirm(a,t,p['preview_id'],True)
    import os
    own_uid=os.getuid()
    monkeypatch.setattr(os,'getuid',lambda:own_uid+100)
    monkeypatch.setattr(os,'access',lambda *args:False)
    monkeypatch.setattr(os,'fchmod',lambda *args:pytest.fail('cannot chmod another owner'))
    with pytest.raises(ValueError,match='存储尚未就绪'):uploaded(intake)


def test_explicit_confirmation_required(intake):
    imp,a=intake;t=uploaded(intake);p=imp.preview(a,t,None)
    with pytest.raises(ValueError):imp.confirm(a,t,p['preview_id'],False)


def test_replace_failure_stays_unverified(intake,monkeypatch):
    imp,a=intake;t=uploaded(intake);p=imp.preview(a,t,None);imp.confirm(a,t,p['preview_id'],True)
    t=uploaded(intake);p=imp.preview(a,t,None)
    import os
    original=os.replace
    def fail(src,dest):
        if Path(dest)==imp.settings.product_library/'45-demo':raise OSError('synthetic')
        return original(src,dest)
    monkeypatch.setattr(os,'replace',fail)
    with pytest.raises(ValueError,match='人工检查'):imp.confirm(a,t,p['preview_id'],True)
    product=imp.db.get_product('45-demo')
    assert not product['share_verified'] and product['quality_status']=='unknown'
    assert json.loads((imp.root/t/'job.json').read_text())['phase']=='needs_manual_recovery'
    assert json.loads((imp.root/t/'job.json').read_text())['failure']=={'type':'OSError','errno':None}
    assert (imp.root/t/'previous-product').is_dir()
