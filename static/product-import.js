// Private upload/preview/confirm flow; never calls session, delivery or scan APIs.
(() => {
  const node = id => document.getElementById(id);
  let listing, files = [], token = null, preview = null, busy = false, committed = false, returnDir = null, uncertain = false;
  const headers = {'X-Product-Import': 'confirm-local'};
  function importFilesAllowed(files) {
    return files.length > 0 && files.length <= 20000
      && files.every(f => Number.isSafeInteger(f.size) && f.size >= 0 && f.size <= 1024**3)
      && files.reduce((n,f) => n+f.size, 0) <= 1024**3;
  }
  async function request(path, method, body, raw = false) {
    const response = await fetch('/api/product-imports' + path, {
      method, headers: {...headers, ...(raw ? {} : {'Content-Type':'application/json'})},
      body: body === undefined ? undefined : raw ? body : JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '导入未完成');
    return data;
  }
  function controls(value) {
    busy = value;
    for (const id of ['previewSelectedZip','importZipSelection','acceptProductImport']) node(id).disabled = value || committed || uncertain;
    node('cancelProductImport').disabled = value || uncertain;
    node('continueImportSetup').disabled = value;
    node('uploadProductFolder').disabled = value || !!token || !importFilesAllowed(files);
    node('uploadProductFolder').textContent = committed ? '商品包已导入' : token ? '本次已开始上传' : value ? '正在准备上传…' : '上传并生成预览';
    node('importFolder').disabled = value || !!token;
    node('importProductDir').readOnly = value || !!token || !!listing?.matched_product_dir_name;
    node('confirmProductImport').disabled = value || committed || uncertain || !preview?.preview_id || !node('acceptProductImport').checked;
    node('productImportForm').setAttribute('aria-busy', String(value));
  }
  async function work(fn) {
    if (busy) return;
    controls(true); node('importError').textContent = '';
    try { await fn(); }
    catch (error) {
      node('importError').textContent = error.message || '连接失败，结果未确认；不要重复执行，请先检查';
      node('leaveProductImport').hidden = !token;
      node('importProgress').textContent = committed ? '导入已成功，但最新商品状态读取失败。请关闭后重新打开资料配置查看；不要重复导入。' : uncertain ? '确认结果未知，请保留现场检查，不要重复提交或重新导入。' : '本步骤未完成，原商品尚未确认替换。请查看下方错误。';
    }
    finally { controls(false); }
  }
  function showPreview(result) {
    preview = result;
    node('importUploadFields').hidden = true;
    node('importPreview').hidden = false;
    node('acceptProductImport').checked = false;
    node('importZipSelection').replaceChildren(...result.packages.map(p => new Option(`${p.path} (${(p.size/1024/1024).toFixed(2)} MB)`, p.index)));
    if (result.zip_name) {
      const match = result.packages.find(p => p.path.split('/').pop() === result.zip_name);
      if (match) node('importZipSelection').value = match.index;
    }
    node('importPreviewText').textContent = result.requires_zip_selection ? result.message :
      `闲鱼：${result.title}\nID：${result.item_id}\n本地：${result.product}\n${result.existing ? '更新已有Product，旧版本会备份' : '建立新的独立Product'}\n交付：${result.zip_name}\nhash：${result.zip_hash.slice(0,12)}\n知识：${result.knowledge_chars}字\n来源：${result.knowledge_sources.join('、')}\n质量：${result.quality_status}\n${result.quality_errors.join('\n')}\n${result.knowledge_warnings.join('\n')}\n${result.notice}`;
    node('importKnowledge').textContent = (result.knowledge_preview || '') + (result.knowledge_truncated ? '\n（预览已截断，后台保存完整提取结果）' : '');
    node('importProgress').textContent = result.requires_zip_selection
      ? '上传完成，尚未导入。请选择要交付的ZIP，再生成预览。'
      : '预览已生成，尚未正式导入。检查下方资料，勾选确认后点击“确认导入”。';
  }
  document.addEventListener('click', event => {
    const button = event.target.closest('.import-product-button');
    if (!button || busy || state.editSavePending || state.shareConfirmPending) return;
    if (button.id === 'editImportProduct' && Object.keys(editedFields()).length) {
      node('formError').textContent = '有未保存的修改，请先保存网盘信息或维护设置，再导入商品包。';
      return;
    }
    listing = state.listings.find(item => item.item_id === button.dataset.itemId);
    if (!listing) return;
    returnDir = button.id === 'editImportProduct' ? node('editDirName').value : null;
    if (returnDir) node('editDialog').close();
    files = []; token = null; preview = null; committed = false; uncertain = false;
    node('productImportForm').reset();
    node('importProductDir').value = listing.matched_product_dir_name || '';
    node('importIdentity').textContent = `闲鱼商品：${listing.title} · ${listing.item_id}`;
    node('importFilesSummary').textContent = '最多20,000个文件，整包不超过1GiB（1024MiB）；隐藏文件不上传。';
    node('importError').textContent = node('importProgress').textContent = '';
    node('importPreview').hidden = true;
    node('importUploadFields').hidden = false;
    node('leaveProductImport').hidden = true;
    node('continueImportSetup').hidden = true;
    node('importUploadProgress').hidden = true;
    node('importUploadProgress').value = 0;
    node('cancelProductImport').textContent = returnDir ? '返回交付准备' : '取消 / 关闭';
    node('confirmProductImport').textContent = '确认导入（不核验、不开放发货）';
    controls(false); node('productImportDialog').showModal();
  });
  node('importFolder').addEventListener('change', () => {
    const all = [...node('importFolder').files];
    files = all.filter(f => !f.webkitRelativePath.split('/').some(p => p.startsWith('.')));
    if (!listing.matched_product_dir_name && !node('importProductDir').value && files.length) node('importProductDir').value = files[0].webkitRelativePath.split('/')[0];
    node('importFilesSummary').textContent = `${files[0]?.webkitRelativePath.split('/')[0] || '未选择文件夹'} · ${files.length}个文件 · ${(files.reduce((n,f)=>n+f.size,0)/1024/1024).toFixed(2)} MB；已忽略${all.length-files.length}个隐藏文件。尚未上传。`;
    node('importError').textContent = all.length && !importFilesAllowed(files) ? '请选择包含有效文件的文件夹：最多20,000个文件，整包不超过1GiB。' : '';
    controls(false);
  });
  node('productImportForm').addEventListener('submit', event => {
    event.preventDefault();
    work(async () => {
      if (token) throw new Error('本次已上传，请预览或取消后重试');
      if (!importFilesAllowed(files)) throw new Error('最多20,000个文件，整包不超过1GiB（1024MiB）');
      const job = await request('', 'POST', {item_id:listing.item_id,product_dir:node('importProductDir').value.trim(),files:files.map(f=>({path:f.webkitRelativePath,size:f.size}))});
      token = job.import_id;
      node('importUploadProgress').hidden = false;
      node('importUploadProgress').max = files.length;
      for (let i=0;i<files.length;i++) {
        node('importProgress').textContent = `上传 ${i+1}/${files.length}；原商品尚未改变`;
        await request(`/${token}/files/${i}`, 'PUT', files[i], true);
        node('importUploadProgress').value = i+1;
      }
      node('importProgress').textContent = '文件已上传至暂存区，正在校验；请检查预览后再确认。';
      node('importFilesSummary').textContent = `${files[0].webkitRelativePath.split('/')[0]} · ${files.length}个文件 · ${(files.reduce((n,f)=>n+f.size,0)/1024/1024).toFixed(2)} MB；文件已上传到暂存区。`;
      showPreview(await request(`/${token}/preview`, 'POST', {}));
    });
  });
  node('previewSelectedZip').addEventListener('click', () => work(async()=>{
    showPreview(await request(`/${token}/preview`, 'POST', {zip_index:Number(node('importZipSelection').value)}));
  }));
  node('importZipSelection').addEventListener('change',()=>{preview=null;node('acceptProductImport').checked=false;node('importProgress').textContent='ZIP选择已变化，请点击“预览所选ZIP”，旧预览不能用于确认。';controls(false);});
  node('acceptProductImport').addEventListener('change',()=>controls(busy));
  node('confirmProductImport').addEventListener('click',()=>work(async()=>{
    if (!preview?.preview_id || !node('acceptProductImport').checked || committed) return;
    node('confirmProductImport').textContent='正在确认导入…';
    node('importProgress').textContent='正在保存商品资料，请勿关闭或重复提交…';
    uncertain = true;
    const result=await request(`/${token}/confirm`,'POST',{preview_id:preview.preview_id,accept_replace_and_unverify:true});
    if (result.committed !== true) throw new Error('服务器未确认导入成功，请保留现场检查');
    committed=true; uncertain=false;
    const summary=preview; preview=null;
    node('importProgress').textContent=`✓ 正式导入成功 · 完成于 ${new Date().toLocaleString()}\nZIP：${summary.zip_name} · 版本：${summary.zip_hash.slice(0,12)} · 知识：${summary.knowledge_chars}字\n旧版本与原包已归档。尚未人工核验，自动发货允许范围未改变。`;
    node('importPreview').hidden=true;
    node('importUploadProgress').hidden=true;
    node('cancelProductImport').textContent='关闭';
    await loadProducts();
    node('continueImportSetup').hidden=false;
  }));
  node('continueImportSetup').addEventListener('click',()=>{
    if(busy || !committed) return;
    const current=state.listings.find(item=>item.item_id===listing.item_id);
    if (!current?.matched_product_dir_name) {node('importError').textContent='最新精确映射暂不可用，请刷新商品列表后再打开。';return;}
    node('productImportDialog').close();openEdit(current.matched_product_dir_name);
    node('editNotice').textContent='商品包已导入成功。请继续第2步保存网盘信息，再由本人完成第3步核验。';
    node('shareUrl').focus();
  });
  async function closeImport() {
    await work(async()=>{
      if(token && !committed) await request(`/${token}`,'DELETE');
      node('productImportDialog').close();
      if(returnDir) openEdit(returnDir);
    });
  }
  node('cancelProductImport').addEventListener('click',closeImport);
  node('leaveProductImport').addEventListener('click',()=>{if(!busy)node('productImportDialog').close();});
  node('productImportDialog').addEventListener('cancel',event=>{event.preventDefault();if(!busy && !uncertain)closeImport();});
})();
