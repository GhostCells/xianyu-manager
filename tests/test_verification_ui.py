import subprocess
from pathlib import Path


def test_verification_feedback_loading_errors_and_identity():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('static/app.js','utf8'), elements={};
const el=id=>elements[id]||(elements[id]={value:'',style:{},checked:false,open:true,showModal(){throw Error('must remain open')}});
const p={dir_name:'20-Seedance2.5',name:'local name',title:'registered title',listing_url:'exact',
share_url:'url',share_code:'code',zip_hash:'a'.repeat(64),fulfillment_fingerprint:'b'.repeat(64),delivery_issues:['VERIFICATION_VERSION_UNCONFIRMED']};
const notices=[],requests=[];let resolve;
const ctx={el,state:{products:[p],listings:[{matched_product_dir_name:p.dir_name,listing_url:'exact',title:'platform title'}]},
Object,JSON,Boolean,Array,encodeURIComponent,centsToInput:()=>'',inputToCents:()=>null,renderKnowledgeEditor:()=>{},
showActionNotice:(m,e)=>notices.push([m,e]),loadProducts:async()=>{},
fetch:()=>{requests.push(1);return new Promise(r=>resolve=r)}};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('function openEdit('),source.indexOf('async function ',source.indexOf('function shareFieldsChanged'))),ctx);
vm.runInContext(source.slice(source.indexOf('function readEditFields()'),source.indexOf('async function saveEdit(')),ctx);
(async()=>{
ctx.openEdit(p.dir_name);assert(el('editProductIdentity').textContent.includes('platform title'));assert(el('editProductIdentity').textContent.includes(p.dir_name));
const first=ctx.confirmShare();await ctx.confirmShare();assert.equal(requests.length,1);assert.equal(el('confirmShare').textContent,'正在确认…');
p.share_verified=true;p.verified_fingerprint=p.fulfillment_fingerprint;p.share_verified_at='synthetic time';p.delivery_issues=[];
resolve({ok:true});await first;assert(el('shareVerificationStatus').textContent.startsWith('✓'));assert(el('confirmShare').disabled);assert(notices.some(n=>n[0].includes('成功')));
el('shareUrl').value='changed';ctx.shareFieldsChanged();assert(el('shareVerificationStatus').textContent.includes('已变化'));assert(el('confirmShare').disabled);
el('shareUrl').value='url';ctx.shareFieldsChanged();assert(el('shareVerificationStatus').textContent.startsWith('✓'));
p.zip_hash='c'.repeat(64);p.fulfillment_fingerprint='d'.repeat(64);ctx.openEdit(p.dir_name);assert(el('shareVerificationStatus').textContent.startsWith('⚠'));
const fail=ctx.confirmShare();resolve({ok:false,json:async()=>({detail:'synthetic failure'})});await fail;
assert.equal(el('formError').textContent,'synthetic failure');assert(notices.at(-1)[1]);assert(!ctx.state.shareConfirmPending);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(['node', '-e', script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
