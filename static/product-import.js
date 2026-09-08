// Private upload/preview/confirm flow; never calls session, delivery or scan APIs.
(() => {
  const node = id => document.getElementById(id);
  let listing, files = [], token = null, preview = null, busy = false, committed = false;
  const headers = {'X-Product-Import': 'confirm-local'};
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
    for (const id of ['uploadProductFolder','previewSelectedZip','importZipSelection','cancelProductImport']) node(id).disabled = value;
    node('importFolder').disabled = value || !!token;
    node('importProductDir').readOnly = value || !!token || !!listing?.matched_product_dir_name;
    node('confirmProductImport').disabled = value || committed || !preview?.preview_id || !node('acceptProductImport').checked;
  }
  async function work(fn) {
    if (busy) return;
    controls(true); node('importError').textContent = '';
    try { await fn(); }
    catch (error) {
      node('importError').textContent = error.message || '连接失败，结果未确认；不要重复执行，请先检查';
      node('leaveProductImport').hidden = !token;
    }
    finally { controls(false); }
  }
  function showPreview(result) {
    preview = result;
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
  }
  document.getElementById('productList').addEventListener('click', event => {
    const button = event.target.closest('.import-product-button');
    if (!button || busy) return;
    listing = state.listings.find(item => item.item_id === button.dataset.itemId);
    if (!listing) return;
    files = []; token = null; preview = null; committed = false;
    node('productImportForm').reset();
    node('importProductDir').value = listing.matched_product_dir_name || '';
    node('importIdentity').textContent = `闲鱼商品：${listing.title} · ${listing.item_id}`;
    node('importFilesSummary').textContent = '最多1000个文件、单文件256MB、整包512MB；隐藏文件不上传。';
    node('importError').textContent = node('importProgress').textContent = '';
    node('importPreview').hidden = true;
    node('leaveProductImport').hidden = true;
    controls(false); node('productImportDialog').showModal();
  });
  node('importFolder').addEventListener('change', () => {
    const all = [...node('importFolder').files];
    files = all.filter(f => !f.webkitRelativePath.split('/').some(p => p.startsWith('.')));
    if (!listing.matched_product_dir_name && !node('importProductDir').value && files.length) node('importProductDir').value = files[0].webkitRelativePath.split('/')[0];
    node('importFilesSummary').textContent = `${files.length}个文件 · ${(files.reduce((n,f)=>n+f.size,0)/1024/1024).toFixed(2)} MB；已忽略${all.length-files.length}个隐藏文件。点击上传后才传至服务器。`;
  });
  node('productImportForm').addEventListener('submit', event => {
    event.preventDefault();
    work(async () => {
      if (token) throw new Error('本次已上传，请预览或取消后重试');
      if (!files.length || files.length > 1000 || files.some(f=>f.size>256*1024**2) || files.reduce((n,f)=>n+f.size,0)>512*1024**2) throw new Error('请检查文件数量和大小限制');
      const job = await request('', 'POST', {item_id:listing.item_id,product_dir:node('importProductDir').value.trim(),files:files.map(f=>({path:f.webkitRelativePath,size:f.size}))});
      token = job.import_id;
      for (let i=0;i<files.length;i++) {
        node('importProgress').textContent = `上传 ${i+1}/${files.length}；原商品尚未改变`;
        await request(`/${token}/files/${i}`, 'PUT', files[i], true);
      }
      node('importProgress').textContent = '文件已上传至暂存区，正在校验；请检查预览后再确认。';
      showPreview(await request(`/${token}/preview`, 'POST', {}));
    });
  });
  node('previewSelectedZip').addEventListener('click', () => work(async()=>{
    showPreview(await request(`/${token}/preview`, 'POST', {zip_index:Number(node('importZipSelection').value)}));
  }));
  node('importZipSelection').addEventListener('change',()=>{preview=null;node('acceptProductImport').checked=false;controls(false);});
  node('acceptProductImport').addEventListener('change',()=>controls(busy));
  node('confirmProductImport').addEventListener('click',()=>work(async()=>{
    if (!preview?.preview_id || !node('acceptProductImport').checked || committed) return;
    const result=await request(`/${token}/confirm`,'POST',{preview_id:preview.preview_id,accept_replace_and_unverify:true});
    committed=result.committed; preview=null;
    node('importProgress').textContent='导入成功。旧版本与原包已归档；请配置分享并人工核验。自动发货允许范围未改变。';
    await loadProducts();
  }));
  async function closeImport() {
    await work(async()=>{
      if(token && !committed) await request(`/${token}`,'DELETE');
      node('productImportDialog').close();
    });
  }
  node('cancelProductImport').addEventListener('click',closeImport);
  node('leaveProductImport').addEventListener('click',()=>{if(!busy)node('productImportDialog').close();});
  node('productImportDialog').addEventListener('cancel',event=>{event.preventDefault();if(!busy)closeImport();});
})();
