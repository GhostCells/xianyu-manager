import subprocess
from pathlib import Path


def test_verification_feedback_loading_errors_and_identity():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('static/app.js','utf8'), elements={};
const el=id=>elements[id]||(elements[id]={value:'',style:{},dataset:{},checked:false,open:true,reportValidity(){return true},showModal(){throw Error('must remain open')}});
const p={dir_name:'20-Seedance2.5',name:'local name',title:'registered title',listing_url:'exact',
share_url:'url',share_code:'code',zip_hash:'a'.repeat(64),fulfillment_fingerprint:'b'.repeat(64),delivery_issues:['VERIFICATION_VERSION_UNCONFIRMED']};
const notices=[],requests=[];let resolve;
const ctx={el,state:{products:[p],listings:[{matched_product_dir_name:p.dir_name,listing_url:'exact',title:'platform title'}]},
Object,JSON,Boolean,Array,encodeURIComponent,centsToInput:v=>v==null?'':(v/100).toFixed(2),inputToCents:v=>v===''?null:Math.round(Number(v)*100),renderKnowledgeEditor:()=>{},
showActionNotice:(m,e)=>notices.push([m,e]),loadProducts:async()=>{},
fetch:()=>{requests.push(1);return new Promise(r=>resolve=r)}};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('function openEdit('),source.indexOf('async function ',source.indexOf('function shareFieldsChanged'))),ctx);
vm.runInContext(source.slice(source.indexOf('function shareErrorMessage('),source.indexOf('async function copyListing(')),ctx);
(async()=>{
ctx.openEdit(p.dir_name);assert(el('editProductIdentity').textContent.includes('platform title'));assert(el('editProductIdentity').textContent.includes(p.dir_name));
const first=ctx.confirmShare();await ctx.confirmShare();assert.equal(requests.length,1);assert.equal(el('confirmShare').textContent,'正在确认…');
p.share_verified=true;p.verified_fingerprint=p.fulfillment_fingerprint;p.share_verified_at='synthetic time';p.delivery_issues=[];
resolve({ok:true});await first;assert(el('shareVerificationStatus').textContent.startsWith('✓'));assert(el('confirmShare').disabled);assert(el('editNotice').textContent.includes('成功'));assert.equal(notices.length,0);
el('shareUrl').value='changed';ctx.shareFieldsChanged();assert(el('shareVerificationStatus').textContent.includes('已变化'));assert(el('confirmShare').disabled);assert.equal(el('editNotice').textContent,'');
el('shareUrl').value='url';ctx.shareFieldsChanged();assert(el('shareVerificationStatus').textContent.startsWith('✓'));
p.zip_hash='c'.repeat(64);p.fulfillment_fingerprint='d'.repeat(64);ctx.openEdit(p.dir_name);assert(el('shareVerificationStatus').textContent.startsWith('⚠'));
const fail=ctx.confirmShare();resolve({ok:false,json:async()=>({detail:'synthetic failure'})});await fail;
assert.equal(el('formError').textContent,'synthetic failure');assert.equal(notices.length,0);assert(!ctx.state.shareConfirmPending);
const blocked=ctx.confirmShare();resolve({ok:false,json:async()=>({detail:'DELIVERY_PACKAGE_UNCONFIRMED,QUALITY_BLOCKED'})});await blocked;
assert(el('formError').textContent.includes('交付资料尚未登记'));assert(el('formError').textContent.includes('质量检查尚未通过'));
el('shareUrl').value='new-url';ctx.shareFieldsChanged();
const saving=ctx.saveEdit({preventDefault(){}});const count=requests.length;
await ctx.saveEdit({preventDefault(){}});await ctx.confirmShare();assert.equal(requests.length,count);
assert.equal(el('saveEditButton').textContent,'正在保存…');
p.share_url='new-url';resolve({ok:true});await saving;
assert(el('editDialog').open);assert(el('editNotice').textContent.includes('已保存'));
assert.equal(Object.keys(ctx.editedFields()).length,0);assert(!el('confirmShare').disabled);
const saveFail=ctx.saveEdit({preventDefault(){}});resolve({ok:false,json:async()=>({detail:'QUALITY_BLOCKED'})});await saveFail;
assert(el('editDialog').open);assert(el('formError').textContent.includes('质量检查'));
assert(!ctx.state.editSavePending);assert(!el('saveEditButton').disabled);
ctx.fetch=async()=>{throw Error('network unavailable')};await ctx.saveEdit({preventDefault(){}});
assert(el('formError').textContent.includes('network unavailable'));assert(!ctx.state.editSavePending);
// One explicit share save must not patch or discard maintenance drafts.
ctx.openEdit(p.dir_name);el('confirmedPrice').value='19';el('shareUrl').value='saved-link';
let body;ctx.fetch=async(url,options)=>{body=JSON.parse(options.body);p.share_url=body.share_url;return {ok:true}};
await ctx.saveEdit({preventDefault(){}},true);
assert.deepEqual(body,{share_url:'saved-link'});assert.equal(el('confirmedPrice').value,'19.00');
assert(el('editNotice').textContent.includes('维护设置仍有未保存'));
ctx.renderShareVerification({...p,delivery_issues:['DELIVERY_PACKAGE_UNCONFIRMED','QUALITY_BLOCKED','SHARE_UNVERIFIED']});
assert(el('shareVerificationStatus').textContent.includes('第1步'));assert(!el('shareVerificationStatus').textContent.includes('质量'));
assert(el('confirmShare').disabled);assert(el('verificationDetailText').textContent.includes('质量'));
// An imported safe ZIP may be verified even when publishing quality is unknown/failed.
const imported={...p,share_verified:false,verified_fingerprint:'',share_needs_review:true,quality_status:'failed',
zip_name:'customer.zip',delivery_safety_fingerprint:'c'.repeat(64),delivery_issues:['SHARE_UNVERIFIED','SHARE_NEEDS_REVIEW','VERIFICATION_VERSION_UNCONFIRMED']};
ctx.renderShareVerification(imported);assert(!el('confirmShare').disabled);
assert(el('deliveryPackageStatus').textContent.includes('ZIP安全检查通过'));
assert(!el('shareVerificationStatus').textContent.includes('质量'));
ctx.renderShareVerification({...imported,delivery_issues:['DELIVERY_PACKAGE_SAFETY_UNCONFIRMED']});
assert(el('confirmShare').disabled);assert(el('shareVerificationStatus').textContent.includes('版本未确认或已变化'));
ctx.renderShareVerification({...imported,delivery_issues:['SHARE_SYNTAX_INVALID']});assert(el('confirmShare').disabled);
// Cloud tutorials have an explicit version and never require a fake ZIP.
const cloud={...imported,zip_name:'',zip_hash:'',zip_size:0,delivery_kind:'cloud',delivery_revision:'e'.repeat(64),delivery_safety_fingerprint:''};
ctx.renderShareVerification(cloud);assert(!el('confirmShare').disabled);
assert(el('deliveryPackageStatus').textContent.includes('网盘资料交付（无ZIP）'));
ctx.renderShareVerification({...cloud,delivery_issues:['DELIVERY_CLOUD_VERSION_UNCONFIRMED']});assert(el('confirmShare').disabled);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(['node', '-e', script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
