"""Synthetic DOM/network: never imports assets or changes production data."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_steps_and_maintenance_are_separate():
    html = (ROOT / 'static/index.html').read_text()
    assert html.index('1. 导入商品包') < html.index('2. 保存网盘信息') < html.index('3. 人工核验')
    maintenance = html.split('<details id="deliveryMaintenance"')[1]
    for name in ['revokeShare', 'enabledForAccount', 'knowledgeAdvanced', 'suggestedPrice', 'listingStatus']:
        assert f'id="{name}"' in maintenance
    assert html.count('id="saveEditButton"') == 1
    assert html.count('id="editNotice"') == 1
    assert 'id="importUploadProgress"' in html


def test_upload_preview_commit_feedback_and_uncertain_result():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
async function scenario(failure){
const elements={},listeners={},calls=[];
const el=id=>elements[id]||(elements[id]={id,dataset:{},value:'',files:[],checked:false,
  disabled:false,textContent:'',hidden:false,addEventListener(type,fn){this[type]=fn},
  setAttribute(){},replaceChildren(){},reset(){},showModal(){this.open=true},close(){this.open=false},focus(){}});
const listing={item_id:'123',matched_product_dir_name:'35-demo',title:'synthetic'};
let release,opened;
const ctx={document:{getElementById:el,addEventListener:(t,f)=>listeners[t]=f},
 state:{listings:[listing]},editedFields:()=>({}),openEdit:dir=>opened=dir,
 loadProducts:async()=>{},Option:function(t,v){this.value=v},Date,
 fetch:async(url,opts)=>{
  calls.push([url,opts.method]);
  if(url==='/api/product-imports')return {ok:true,json:async()=>({import_id:'safe'})};
  if(url.endsWith('/preview'))return {ok:true,json:async()=>({preview_id:'v1',title:'synthetic',item_id:'123',product:'35-demo',existing:true,
  packages:[{path:'客户交付/demo.zip',index:0,size:1}],zip_name:'demo.zip',zip_hash:'a'.repeat(64),knowledge_chars:8,knowledge_sources:[],quality_status:'passed',quality_errors:[],knowledge_warnings:[],notice:'synthetic'})};
  if(url.endsWith('/confirm')){await new Promise(r=>release=r);if(failure)throw Error('synthetic timeout');return {ok:true,json:async()=>({committed:true})};}
  return {ok:true,json:async()=>({})};
 }};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('static/product-import.js','utf8'),ctx);
const button=el('editImportProduct');button.dataset.itemId='123';el('editDirName').value='35-demo';
listeners.click({target:{closest:()=>button}});
assert(el('uploadProductFolder').disabled);
el('importFolder').files=[{size:1,webkitRelativePath:'35-demo/客户交付/demo.zip'}];el('importFolder').change();
assert(!el('uploadProductFolder').disabled);assert(el('importFilesSummary').textContent.includes('35-demo'));
el('productImportForm').submit({preventDefault(){}});
await new Promise(r=>setImmediate(r));
assert(el('importProgress').textContent.includes('尚未正式导入'));assert(el('confirmProductImport').disabled);
assert.equal(el('importUploadProgress').value,1);
el('acceptProductImport').checked=true;el('acceptProductImport').change();
el('confirmProductImport').click();el('confirmProductImport').click();
await new Promise(r=>setImmediate(r));
assert.equal(calls.filter(c=>c[0].endsWith('/confirm')).length,1);
assert(el('confirmProductImport').disabled);assert(el('importProgress').textContent.includes('正在保存'));
release();await new Promise(r=>setImmediate(r));
if(failure){
 assert(el('importProgress').textContent.includes('结果未知'));assert(el('confirmProductImport').disabled);
 assert(el('cancelProductImport').disabled);assert(!el('leaveProductImport').hidden);
 assert(el('continueImportSetup').hidden);
}else{
 assert(el('importProgress').textContent.includes('正式导入成功'));assert(el('importProgress').textContent.includes('8字'));
 assert(!el('continueImportSetup').hidden);assert(el('confirmProductImport').disabled);
 el('continueImportSetup').click();assert.equal(opened,'35-demo');assert(el('editNotice').textContent.includes('第2步'));
}
assert(calls.every(c=>c[0].startsWith('/api/product-imports')));
}
(async()=>{await scenario(false);await scenario(true)})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
