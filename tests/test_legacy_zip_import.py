import asyncio
import pytest
from test_product_import import intake, uploaded, zipped


def legacy(intake, paths):
    imp, a = intake
    data = zipped()
    job = imp.start(a, '123456789', '45-demo', [{'path':'45-demo/'+p,'size':len(data)} for p in paths])
    async def send():
        for i in range(len(paths)):
            async def chunks(): yield data
            await imp.upload(a, job['import_id'], i, chunks())
    asyncio.run(send())
    return job['import_id']


@pytest.mark.parametrize('path',['delivery.zip','客户交付/delivery.zip','DELIVERY.ZIP'])
def test_zip_only_import_without_invented_knowledge(intake,path):
    imp,a=intake;t=legacy(intake,[path]);p=imp.preview(a,t,None)
    assert p['can_confirm'] and p['delivery_kind']=='zip' and p['knowledge_chars']==0
    assert '暂无自动回复知识' in p['knowledge_warnings'][0]
    imp.confirm(a,t,p['preview_id'],True)
    product=imp.db.get_product('45-demo')
    assert not product['knowledge_text'] and not product['verified_fingerprint']
    assert product['delivery_safety_fingerprint']
    assert imp.renew_committed_package_safety(a,t)['safety_checked']


def test_zip_only_preserves_existing_cached_knowledge(intake):
    imp,a=intake;t=uploaded(intake);p=imp.preview(a,t,None);imp.confirm(a,t,p['preview_id'],True)
    old=imp.db.get_product('45-demo')
    t=legacy(intake,['new.zip']);p=imp.preview(a,t,None)
    assert p['knowledge_preview']==old['knowledge_text']
    imp.confirm(a,t,p['preview_id'],True)
    current=imp.db.get_product('45-demo')
    for k in ('knowledge_text','knowledge_hash','knowledge_chars','knowledge_updated_at'):
        assert current[k]==old[k]
    assert not current['verified_fingerprint']


def test_multiple_zip_requires_selection_and_ignores_source_zip(intake):
    imp,a=intake;t=legacy(intake,['one.zip','客户交付/two.zip','制作源文件/private.zip'])
    p=imp.preview(a,t,None)
    assert p['requires_zip_selection'] and len(p['packages'])==2
    with pytest.raises(ValueError): imp.preview(a,t,2)
    assert imp.preview(a,t,0)['zip_name']=='one.zip'
