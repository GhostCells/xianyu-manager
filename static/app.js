const state = {
  products: [], listings: [], account: null, session: null, sessionTimer: null,
  delivery: null, deliveryTimer: null, autoReply: null, autoReplyTimer: null,
  autoReplyFormInitialized: false, safety: null, safetyTimer: null,
  safetyFormInitialized: false
};

const el = (id) => document.getElementById(id);
const money = (cents) => cents == null ? "—" : `¥${(cents / 100).toFixed(2)}`;
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

function productStage(product) {
  if (product.quality_status !== "passed") return "failed";
  if (!product.enabled_for_account) return "library";
  if (!Array.isArray(product.delivery_issues) || product.delivery_issues.length) return "needs_link";
  if (product.listing_status === "published") return "published";
  return "ready";
}

function renderSummary() {
  const live = state.listings;
  const mapped = live.filter((item) => item.matched_product_dir_name);
  const deliverable = live.filter((item) => item.delivery_ready);
  const needsWork = live.filter((item) => !item.delivery_ready);
  el("summary").innerHTML = [
    ["管理商品", live.length, "账号快照 + 已确认发布记录"],
    ["完成映射", mapped.length, "已找到本地交付商品"],
    ["可以发货", deliverable.length, "商品与网盘均就绪"],
    ["需要处理", needsWork.length, "缺映射或网盘地址"],
  ].map(([label, value, hint]) => `<article class="metric"><span>${label}</span><strong>${value}</strong><small>${hint}</small></article>`).join("");
}

function badges(product) {
  const result = [];
  const knowledgeBadge = Number(product.knowledge_chars || 0) > 0
    ? `<span class="badge blue">AI资料 ${Number(product.knowledge_chars).toLocaleString()}字</span>`
    : '<span class="badge warn">AI资料缺失</span>';
  if (!product.enabled_for_account) return `<span class="badge neutral">仅在商品库</span>${knowledgeBadge}`;
  result.push(product.quality_status === "passed" ? '<span class="badge ok">质检通过</span>' : '<span class="badge bad">质检失败</span>');
  result.push(knowledgeBadge);
  if (Array.isArray(product.delivery_issues) && !product.delivery_issues.length) result.push('<span class="badge ok">交付资料已人工核验</span>');
  else result.push('<span class="badge warn">交付资料待复核（非在线失效判定）</span>');
  if (product.listing_status === "published") result.push('<span class="badge blue">已上架</span>');
  else if (product.listing_status === "paused") result.push('<span class="badge neutral">已暂停</span>');
  else result.push('<span class="badge neutral">待上架</span>');
  return result.join("");
}

function matchesQuery(item, query) {
  const text = `${item.dir_name || ""} ${item.name || ""} ${item.title || ""} ${item.product_name || ""}`.toLowerCase();
  return !query || text.includes(query);
}

function renderLibrary(products) {
  return products.map((product) => `
    <article class="product-row">
      <img class="product-cover" src="/api/products/${encodeURIComponent(product.dir_name)}/image" alt="" loading="lazy" onerror="this.classList.add('missing')" />
      <div class="product-main">
        <div class="product-title-line"><span class="product-number">${String(product.number).padStart(2, "0")}</span><div><h3>${escapeHtml(product.name)}</h3><p>${escapeHtml(product.title || "尚未读取发布标题")}</p></div></div>
        <div class="badges">${badges(product)}</div>
      </div>
      <div class="product-facts">
        <span>售价<strong>${money(product.confirmed_price_cents)}</strong></span>
        <span>发布图<strong>${product.image_count} / 5</strong></span>
        <span>提取码<strong>${escapeHtml(product.share_code || "—")}</strong></span>
      </div>
      <div class="row-actions">
        <button class="secondary copy-button" data-dir="${escapeHtml(product.dir_name)}">复制文案</button>
        <button class="primary edit-button" data-dir="${escapeHtml(product.dir_name)}">配置</button>
      </div>
    </article>`).join("");
}

function renderLiveListings(listings) {
  return listings.map((listing) => {
    const product = state.products.find((item) => item.dir_name === listing.matched_product_dir_name);
    const mapped = Boolean(listing.matched_product_dir_name);
    const sourceLabel = listing.source_kind === "configured" ? "已发布" : "在售";
    const mappingBadge = product
      ? `<span class="badge ok">已映射 ${String(product.number).padStart(2, "0")}</span>`
      : mapped
        ? '<span class="badge ok">已配置独立交付</span>'
        : '<span class="badge bad">缺少本地商品映射</span>';
    const deliveryBadge = listing.delivery_ready ? '<span class="badge blue">可以自动发货</span>' : mapped && !listing.share_url ? '<span class="badge warn">待配置网盘</span>' : '<span class="badge neutral">暂不可发货</span>';
    const knowledgeBadge = Number(listing.knowledge_chars || 0) > 0 ? '<span class="badge blue">AI商品资料已载入</span>' : '<span class="badge warn">AI商品资料缺失</span>';
    const usedProductDirs = new Set(state.listings.map((item) => item.matched_product_dir_name).filter(Boolean));
    const mappingCandidates = state.products
      .filter((item) => !["legacy", "listing_only"].includes(item.catalog_status))
      .filter((item) => !usedProductDirs.has(item.dir_name));
    const mappingOptions = mappingCandidates
      .map((item) => {
        const readiness = item.quality_status === "passed" ? "" : " · 资料待完善";
        const searchText = `${item.number} ${item.dir_name || ""} ${item.name || ""} ${item.title || ""}`.toLowerCase();
        return `<option value="${escapeHtml(item.dir_name)}" data-search="${escapeHtml(searchText)}">${String(item.number).padStart(2, "0")} · ${escapeHtml(item.name)}${readiness}</option>`;
      })
      .join("");
    const mappingControls = !mapped && mappingCandidates.length ? `
      <div class="mapping-controls">
        <input class="mapping-search" data-item-id="${escapeHtml(listing.item_id)}" type="search" placeholder="搜索编号或商品名" aria-label="搜索本地商品" autocomplete="off" />
        <select class="mapping-select" data-item-id="${escapeHtml(listing.item_id)}" aria-label="选择要关联的本地商品">
          <option value="">手动选择本地商品…</option>${mappingOptions}
        </select>
        <button class="primary map-button" data-item-id="${escapeHtml(listing.item_id)}" type="button" disabled>确认关联</button>
        <small class="mapping-hint">可选择全部未被其他在售商品占用的本地商品；“资料待完善”可先建立映射，但完善交付资料前不会自动发货。</small>
      </div>` : !mapped ? '<small class="mapping-hint">没有可关联的本地商品，请先同步商品库或检查是否已被其他在售商品占用。</small>' : "";
    return `<article class="product-row">
      <img class="product-cover" src="${escapeHtml(listing.image_url || "")}" alt="" loading="lazy" onerror="this.classList.add('missing')" />
      <div class="product-main">
        <div class="product-title-line"><span class="product-number">${sourceLabel}</span><div><h3>${escapeHtml(listing.title)}</h3><p>闲鱼商品号 ${escapeHtml(listing.item_id)}</p></div></div>
        <div class="badges">${mappingBadge}${deliveryBadge}${knowledgeBadge}</div>
      </div>
      <div class="product-facts">
        <span>售价<strong>${money(listing.price_cents)}</strong></span>
        <span>本地交付<strong>${escapeHtml(listing.product_name || "未匹配")}</strong></span>
        <span>提取码<strong>${escapeHtml(listing.share_code || "—")}</strong></span>
      </div>
      <div class="row-actions">
        <a class="secondary link-button" href="${escapeHtml(listing.listing_url)}" target="_blank" rel="noreferrer">打开闲鱼</a>
        ${product ? `<button class="primary edit-button" data-dir="${escapeHtml(product.dir_name)}">配置交付</button>` : ""}
        ${mappingControls}
      </div>
    </article>`;
  }).join("");
}

function filteredItems() {
  const query = el("searchInput").value.trim().toLowerCase();
  const filter = el("statusFilter").value;
  if (filter === "library") return {kind:"library", items:state.products.filter((item) => matchesQuery(item, query))};
  const listings = state.listings.filter((item) => {
    if (!matchesQuery(item, query)) return false;
    if (filter === "delivery_ready") return item.delivery_ready;
    if (filter === "needs_mapping") return !item.matched_product_dir_name;
    if (filter === "needs_link") return item.matched_product_dir_name && !item.delivery_ready;
    return true;
  });
  return {kind:"listings", items:listings};
}

function renderProducts() {
  const filtered = filteredItems();
  if (!filtered.items.length) {
    el("productList").innerHTML = '<div class="empty-state"><strong>这里暂时没有商品</strong><span>切换筛选条件，或同步商品数据后再看。</span></div>';
    return;
  }
  el("productList").innerHTML = filtered.kind === "library" ? renderLibrary(filtered.items) : renderLiveListings(filtered.items);
}

function render() { renderSummary(); renderProducts(); }

const pendingSessionStatuses = new Set(["starting", "waiting_scan", "detected"]);
function stopSessionPolling() { if (state.sessionTimer) clearInterval(state.sessionTimer); state.sessionTimer = null; }
function startSessionPolling() { stopSessionPolling(); state.sessionTimer = setInterval(loadSession, 2000); }

function renderSession() {
  const session = state.session;
  if (!session) return;
  const browserLabel = String(session.browser_name || "").toLowerCase().includes("chrome") ? "Chrome" : "Edge";
  const labels = {
    unbound: ["尚未登录七月账号", `点击登录，在打开的专用 ${browserLabel} 窗口中完成扫码。`],
    starting: ["正在打开登录窗口", `请稍候，不要关闭正在打开的专用 ${browserLabel}。`],
    waiting_scan: ["等待完整登录验证", `请在专用 ${browserLabel} 的闲鱼消息页完成登录和必要的滑块验证。`],
    detected: ["已检测到完整登录", `请确认专用 ${browserLabel} 中显示的是七月账号，然后点击“绑定并保持窗口”。`],
    bound: [session.browser_open ? `已绑定专用 ${browserLabel}` : `专用 ${browserLabel} 尚未打开`, session.browser_open ? "系统正在复用这个可见窗口；监听状态请看订单监听面板。" : `点击下方按钮重新打开专用 ${browserLabel}，系统会继续使用已保存的登录状态。`],
    expired: ["七月账号登录已失效", "请重新登录后再开启自动发货。"],
    error: ["账号连接需要处理", session.last_error || "请重新启动登录流程。"],
  };
  const [title, message] = labels[session.status] || labels.unbound;
  el("sessionTitle").textContent = title;
  el("sessionMessage").textContent = message;
  const meta = [];
  if (session.browser_name) meta.push(`浏览器：${session.browser_name}`);
  if (session.browser_mode === "visible_persistent") meta.push("运行方式：可见窗口常驻");
  if (session.binding_confirmed_at) meta.push(`确认时间：${session.binding_confirmed_at}`);
  if (session.last_checked_at) meta.push(`最近检查：${session.last_checked_at}`);
  el("sessionMeta").textContent = meta.join(" · ");
  el("sessionPanel").dataset.status = session.status;
  el("startBinding").hidden = !["unbound", "bound", "expired", "error"].includes(session.status);
  el("startBinding").textContent = session.browser_open ? `显示专用 ${browserLabel}` : (session.status === "bound" ? `打开专用 ${browserLabel}` : "登录七月账号");
  el("confirmBinding").hidden = session.status !== "detected";
  el("syncBinding").hidden = !session.session_sync_available;
  el("cancelBinding").hidden = !pendingSessionStatuses.has(session.status);
  if (pendingSessionStatuses.has(session.status)) startSessionPolling(); else stopSessionPolling();
}

async function loadSession() {
  const response = await fetch("/api/session");
  if (!response.ok) throw new Error("读取账号连接状态失败");
  state.session = await response.json();
  renderSession();
}

const activeDeliveryStatuses = new Set(["starting", "cooldown", "authenticating", "connecting", "listening", "reconnecting"]);
function stopDeliveryPolling() { if (state.deliveryTimer) clearInterval(state.deliveryTimer); state.deliveryTimer = null; }
function startDeliveryPolling() { if (!state.deliveryTimer) state.deliveryTimer = setInterval(loadDelivery, 3000); }
function maskOrderId(value) { const text = String(value || ""); return text ? `…${text.slice(-6)}` : "未知订单"; }

function renderOrders(orders) {
  const labels = {pending:"待处理",sending:"发送中",message_sent:"地址已发送",delivered:"发货完成",failed:"发送失败",confirm_pending:"待确认发货",manual_review:"需人工核对",waiting_group:"待免拼"};
  if (!orders.length) { el("orderList").innerHTML = '<p class="empty">暂无订单记录。</p>'; return; }
  el("orderList").innerHTML = orders.slice(0, 10).map((order) => {
    const groupLabel = order.group_status === "exempted" ? "已免拼，等待付款完成信号" : "";
    const statusLabel = groupLabel || labels[order.delivery_status] || order.delivery_status;
    return `<div class="order-row"><div><strong>${escapeHtml(order.product_title || order.product_name)}</strong><small>${maskOrderId(order.xianyu_order_id)}</small></div><span class="order-status ${escapeHtml(order.delivery_status)}">${escapeHtml(statusLabel)}</span><small>${escapeHtml(order.delivered_at || order.message_sent_at || order.detected_at || order.created_at || "")}</small></div>`;
  }).join("");
}

function renderDelivery() {
  const delivery = state.delivery;
  if (!delivery) return;
  const labels = {
    disabled: ["自动发货尚未开启", "登录完成后开启，只处理已付款订单。"],
    starting: ["正在启动自动发货", "正在检查商品映射和登录会话。"],
    cooldown: ["闲鱼风控冷却中", delivery.last_error || "冷却结束后系统会自动重试。"],
    authenticating: ["正在读取登录会话", "登录凭据只在内存中使用。"],
    connecting: ["正在连接订单消息", "正在建立实时监听连接。"],
    listening: ["自动发货监听中", "商品、订单和网盘地址唯一匹配后才会发送。"],
    reconnecting: ["监听正在恢复", "网络波动，系统正在自动重连。"],
    verification_required: ["需要重新验证闲鱼登录", delivery.last_error || "请点击上方登录七月账号，并在 Edge 中完成验证。"],
    error: ["自动发货需要处理", delivery.last_error || "请检查登录状态。"],
  };
  let [title, message] = labels[delivery.status] || labels.error;
  if (!delivery.enabled && delivery.status === "listening" && delivery.auto_reply_enabled) {
    title = "自动发货未开启";
    message = "实时连接正供 AI 自动回复使用；付款事件不会触发自动发货。";
  }
  el("deliveryTitle").textContent = title;
  el("deliveryMessage").textContent = message;
  const meta = [];
  if (delivery.last_event_at) meta.push(`最近付款：${delivery.last_event_at}`);
  if (delivery.last_delivery_at) meta.push(`最近发货：${delivery.last_delivery_at}`);
  if (delivery.last_recovery_at) meta.push(`漏单检查：${delivery.last_recovery_at}（补偿 ${delivery.recovered_order_count || 0} 单）`);
  if (delivery.ready_product_count != null) meta.push(`可发货商品：${delivery.ready_product_count}`);
  el("deliveryMeta").textContent = meta.join(" · ");
  el("deliveryPanel").dataset.status = delivery.status;
  el("startDelivery").hidden = Boolean(delivery.enabled) && activeDeliveryStatuses.has(delivery.status);
  el("probeDelivery").hidden = activeDeliveryStatuses.has(delivery.status);
  el("stopDelivery").hidden = !delivery.enabled || !activeDeliveryStatuses.has(delivery.status);
  el("autoConfirmPlatform").checked = Boolean(delivery.auto_confirm_platform);
  el("autoFreeGroup").checked = Boolean(delivery.auto_free_group);
  el("autoConfirmPlatform").disabled = Boolean(delivery.enabled) && activeDeliveryStatuses.has(delivery.status);
  el("autoFreeGroup").disabled = Boolean(delivery.enabled) && activeDeliveryStatuses.has(delivery.status);
  renderOrders(delivery.orders || []);
  startDeliveryPolling();
}

async function loadDelivery() {
  const response = await fetch("/api/delivery");
  if (!response.ok) throw new Error("读取自动发货状态失败");
  state.delivery = await response.json();
  renderDelivery();
}

const replyStatusLabels = {
  received: "等待回复", processing: "生成中", replied: "已回复", manual: "未自动答",
  skipped: "已合并", failed: "失败"
};

function autoReplyRecordStatus(record) {
  const reason = String(record.reason || "");
  if (record.status === "replied" && reason.includes("自动澄清追问")) return "已追问";
  if (record.status === "manual" && record.reply_source === "manual") return "人工回复";
  if (record.status === "manual" && (record.manual_takeover || reason.includes("人工接管") || reason.includes("人工回复"))) return "人工接管";
  if (record.status === "manual" && reason.includes("资料")) return "需补充资料";
  return replyStatusLabels[record.status] || record.status;
}

function maskChatId(value) {
  const text = String(value || "");
  return text ? `会话 …${text.slice(-6)}` : "未知会话";
}

function fillAutoReplyForm() {
  const settings = state.autoReply?.settings;
  if (!settings) return;
  el("autoReplyEnabled").checked = Boolean(settings.enabled);
  el("autoReplyModel").value = settings.model || "deepseek-ai/DeepSeek-V4-Flash";
  el("autoReplyBaseUrl").value = settings.base_url || "https://api.siliconflow.cn/v1";
  el("autoReplyPrompt").value = settings.system_prompt || "";
  el("autoReplyMinDelay").value = settings.min_delay_seconds ?? 5;
  el("autoReplyMaxDelay").value = settings.max_delay_seconds ?? 12;
  el("autoReplyMaxChars").value = settings.max_reply_chars ?? 180;
  el("autoReplyManualHours").value = settings.manual_takeover_hours ?? 12;
  el("autoReplyApiKey").value = "";
  el("autoReplyApiKey").placeholder = state.autoReply.has_api_key ? "已用 Windows 加密保存；留空不修改" : "sk-...";
  state.autoReplyFormInitialized = true;
}

function renderAutoReplyTestProducts() {
  const select = el("autoReplyTestProduct");
  const ready = state.listings.filter((item) =>
    item.matched_product_dir_name && Number(item.knowledge_chars || 0) > 0
  );
  const previous = select.value;
  const recentItemId = (state.autoReply?.records || []).find((record) =>
    ready.some((item) => String(item.item_id) === String(record.listing_item_id || ""))
  )?.listing_item_id;
  select.innerHTML = ready.length
    ? ready.map((item) => `<option value="${escapeHtml(item.item_id)}">${escapeHtml(item.title)}</option>`).join("")
    : '<option value="">暂无已载入资料的在售商品</option>';
  const userSelected = select.dataset.userSelected === "true";
  if (userSelected && ready.some((item) => String(item.item_id) === previous)) select.value = previous;
  else if (recentItemId) select.value = String(recentItemId);
  else if (ready.some((item) => String(item.item_id) === previous)) select.value = previous;
  select.disabled = !ready.length;
}

function renderAutoReplyRecords(records) {
  if (!records.length) {
    el("autoReplyRecords").innerHTML = '<p class="empty">暂无回复记录。</p>';
    return;
  }
  el("autoReplyRecords").innerHTML = records.slice(0, 24).map((record) => {
    const direction = record.direction === "inbound" ? "买家" : (record.reply_source === "manual" ? "卖家人工" : "AI 回复");
    const status = autoReplyRecordStatus(record);
    const statusClass = record.status === "failed" || (record.status === "manual" && record.manual_takeover) ? "bad" : record.status === "replied" ? "ok" : "warn";
    const resume = record.manual_takeover ? `<button class="secondary resume-chat" type="button" data-chat="${escapeHtml(record.chat_id)}">恢复自动</button>` : "";
    return `<div class="reply-record">
      <div><strong>${escapeHtml(record.listing_title || `闲鱼商品 ${record.listing_item_id || "未匹配"}`)}</strong><small>${direction} · ${maskChatId(record.chat_id)} · ${escapeHtml(record.created_at || "")}</small></div>
      <div class="reply-content">${escapeHtml(record.content)}${record.reason ? `<small>${escapeHtml(record.reason)}</small>` : ""}</div>
      <div class="reply-status"><span class="badge ${statusClass}">${escapeHtml(status)}</span>${resume}</div>
    </div>`;
  }).join("");
}

function renderAutoReply() {
  const data = state.autoReply;
  if (!data) return;
  if (!state.autoReplyFormInitialized) fillAutoReplyForm();
  const enabled = Boolean(data.settings?.enabled);
  const connected = enabled && data.connected;
  el("autoReplyPanel").dataset.status = connected ? "connected" : enabled ? "waiting" : "disabled";
  el("autoReplyTitle").textContent = connected ? "AI 自动回复监听中" : enabled ? "已启用，等待消息连接" : "AI 自动回复尚未启用";
  el("autoReplyMessage").textContent = connected
    ? "连续消息会合并理解；问题含糊时先追问，资料不足只跳过当前问题，高风险或卖家回复才接管会话。"
    : enabled ? (data.runtime_error || "请确认闲鱼账号已绑定，实时消息连接会自动恢复。") : "保存硅基流动 API Key 并启用后，DeepSeek V4 Flash 才会参与回复。";
  const meta = [
    `模型：${data.settings?.model || "deepseek-ai/DeepSeek-V4-Flash"}`,
    data.has_api_key ? "API Key：已加密保存" : "API Key：未配置",
  ];
  if (data.knowledge) meta.push(`已上架商品知识库：${data.knowledge.ready}/${data.knowledge.total} 已就绪`);
  if (data.last_reply_at) meta.push(`最近回复：${data.last_reply_at}`);
  if (data.last_error) meta.push(`最近错误：${data.last_error}`);
  el("autoReplyMeta").textContent = meta.join(" · ");
  renderAutoReplyTestProducts();
  renderAutoReplyRecords(data.records || []);
  if (!state.autoReplyTimer) {
    state.autoReplyTimer = setInterval(() => loadAutoReply(false).catch(() => {}), 5000);
  }
}

async function loadAutoReply(fillForm = false) {
  const response = await fetch("/api/auto-reply");
  if (!response.ok) throw new Error("读取自动回复状态失败");
  state.autoReply = await response.json();
  if (fillForm) state.autoReplyFormInitialized = false;
  renderAutoReply();
}

function autoReplyFormPayload() {
  return {
    enabled: el("autoReplyEnabled").checked,
    base_url: el("autoReplyBaseUrl").value.trim(),
    model: el("autoReplyModel").value,
    api_key: el("autoReplyApiKey").value.trim() || null,
    system_prompt: el("autoReplyPrompt").value.trim(),
    min_delay_seconds: Number(el("autoReplyMinDelay").value),
    max_delay_seconds: Number(el("autoReplyMaxDelay").value),
    max_reply_chars: Number(el("autoReplyMaxChars").value),
    manual_takeover_hours: Number(el("autoReplyManualHours").value),
  };
}

function showAutoReplyNotice(message, isError = false) {
  const notice = el("autoReplyNotice");
  notice.textContent = message;
  notice.classList.toggle("error", isError);
  notice.hidden = false;
}

async function saveAutoReply(event) {
  event.preventDefault();
  const submit = el("autoReplyForm").querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const response = await fetch("/api/auto-reply/settings", {
      method: "PUT", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(autoReplyFormPayload()),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "自动回复设置保存失败");
    state.autoReply = result;
    state.autoReplyFormInitialized = false;
    renderAutoReply();
    showAutoReplyNotice(result.runtime_error ? `设置已保存；${result.runtime_error}` : "自动回复设置已保存。", Boolean(result.runtime_error));
    await loadDelivery();
  } catch (error) { showAutoReplyNotice(error.message, true); }
  finally { submit.disabled = false; }
}

async function testAutoReply() {
  const button = el("testAutoReply");
  button.disabled = true; button.textContent = "生成中…";
  const form = autoReplyFormPayload();
  try {
    const response = await fetch("/api/auto-reply/test", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        api_key: form.api_key, base_url: form.base_url, model: form.model,
        system_prompt: form.system_prompt, max_reply_chars: form.max_reply_chars,
        listing_item_id: el("autoReplyTestProduct").value || null,
        message: el("autoReplyTestMessage").value.trim(),
      }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "硅基流动测试失败");
    const product = result.product ? `（${result.product}）` : "";
    const message = result.action === "reply"
      ? `测试回复${product}：${result.reply}`
      : result.action === "clarify"
        ? `测试追问${product}：${result.reply}`
        : `测试结果${product}：需人工确认（${result.reason || "模型判断"}）`;
    showAutoReplyNotice(message, false);
  } catch (error) { showAutoReplyNotice(error.message, true); }
  finally { button.disabled = false; button.textContent = "测试生成"; }
}

async function resumeAutoReplyChat(chatId) {
  const response = await fetch(`/api/auto-reply/chats/${encodeURIComponent(chatId)}/resume`, {method:"POST"});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) { showAutoReplyNotice(result.detail || "恢复会话失败", true); return; }
  state.autoReply = result;
  renderAutoReply();
  showAutoReplyNotice("该会话已恢复自动回复。", false);
}

function fillSafetyForm() {
  const settings = state.safety?.settings;
  if (!settings) return;
  el("maxRepliesHour").value = settings.max_replies_per_hour ?? 20;
  el("maxRepliesDay").value = settings.max_replies_per_day ?? 100;
  el("maxDeliveriesHour").value = settings.max_deliveries_per_hour ?? 15;
  el("minOutboundInterval").value = settings.min_outbound_interval_seconds ?? 5;
  el("riskCooldownMinutes").value = settings.risk_cooldown_minutes ?? 30;
  el("notificationsEnabled").checked = Boolean(settings.notifications_enabled);
  state.safetyFormInitialized = true;
}

function renderSafety() {
  const data = state.safety;
  if (!data) return;
  if (!state.safetyFormInitialized) fillSafetyForm();
  const open = Boolean(data.circuit?.is_open);
  el("safetyPanel").dataset.status = open ? "open" : "normal";
  el("safetyTitle").textContent = open ? "自动化已安全熔断" : "发送保护正常";
  el("safetyMessage").textContent = open
    ? (data.circuit.reason || "检测到平台异常，正在冷却。")
    : "自动回复与自动发货均受总量、间隔和重复发送保护。";
  const usage = data.usage || {};
  const settings = data.settings || {};
  const meta = [
    `回复：本小时 ${usage.replies_hour || 0}/${settings.max_replies_per_hour || 20}`,
    `近 24 小时 ${usage.replies_day || 0}/${settings.max_replies_per_day || 100}`,
    `发货：本小时 ${usage.deliveries_hour || 0}/${settings.max_deliveries_per_hour || 15}`,
  ];
  if (open && data.circuit.open_until) meta.push(`冷却至：${data.circuit.open_until}`);
  el("safetyMeta").textContent = meta.join(" · ");
  const startup = data.startup || {};
  const report = startup.report || {};
  el("startupReport").textContent = startup.at
    ? `最近启动检查 ${startup.at}：${report.message || report.status || "已完成"}`
    : "尚无启动检查记录；下次启动系统时会自动生成。";
  el("resetCircuit").hidden = !open;
  if (!state.safetyTimer) {
    state.safetyTimer = setInterval(() => loadSafety(false).catch(() => {}), 5000);
  }
}

async function loadSafety(fillForm = false) {
  const response = await fetch("/api/automation-safety");
  if (!response.ok) throw new Error("读取无人值守保护状态失败");
  state.safety = await response.json();
  if (fillForm) state.safetyFormInitialized = false;
  renderSafety();
}

function safetyPayload() {
  return {
    max_replies_per_hour: Number(el("maxRepliesHour").value),
    max_replies_per_day: Number(el("maxRepliesDay").value),
    max_deliveries_per_hour: Number(el("maxDeliveriesHour").value),
    min_outbound_interval_seconds: Number(el("minOutboundInterval").value),
    risk_cooldown_minutes: Number(el("riskCooldownMinutes").value),
    notifications_enabled: el("notificationsEnabled").checked,
  };
}

function showSafetyNotice(message, isError = false) {
  const notice = el("safetyNotice");
  notice.textContent = message;
  notice.classList.toggle("error", isError);
  notice.hidden = false;
}

async function saveSafety(event) {
  event.preventDefault();
  const button = el("safetyForm").querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const response = await fetch("/api/automation-safety", {
      method: "PUT", headers: {"Content-Type":"application/json"},
      body: JSON.stringify(safetyPayload()),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "安全设置保存失败");
    state.safety = result;
    state.safetyFormInitialized = false;
    renderSafety();
    showSafetyNotice("无人值守安全设置已保存。", false);
  } catch (error) { showSafetyNotice(error.message, true); }
  finally { button.disabled = false; }
}

async function resetCircuit() {
  if (!window.confirm("仅在确认账号可以正常操作后解除熔断。现在继续吗？")) return;
  const button = el("resetCircuit");
  button.disabled = true;
  try {
    const response = await fetch("/api/automation-safety/circuit/reset", {method:"POST"});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "解除熔断失败");
    state.safety = result;
    renderSafety();
    showSafetyNotice("熔断已解除，系统会按当前登录状态恢复监听。", false);
    await loadDelivery();
  } catch (error) { showSafetyNotice(error.message, true); }
  finally { button.disabled = false; }
}

function showActionNotice(message, isError = false) {
  const notice = el("actionNotice");
  notice.textContent = message;
  notice.classList.toggle("error", isError);
  notice.hidden = false;
}

async function deliveryAction(action) {
  const buttons = [el("probeDelivery"), el("startDelivery"), el("stopDelivery")];
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const options = {method:"POST", headers:{"Content-Type":"application/json"}};
    if (action === "start") options.body = JSON.stringify({auto_confirm_platform:el("autoConfirmPlatform").checked, auto_free_group:el("autoFreeGroup").checked});
    const response = await fetch(`/api/delivery/${action}`, options);
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "自动发货操作失败");
    state.delivery = {...result, enabled:action === "start", auto_confirm_platform:el("autoConfirmPlatform").checked, auto_free_group:el("autoFreeGroup").checked};
    if (action === "probe" && result.probe_ok) showActionNotice("连接自检通过。", false);
    renderDelivery();
  } catch (error) { showActionNotice(error.message, true); await loadDelivery(); }
  finally { buttons.forEach((button) => { button.disabled = false; }); }
}

async function sessionAction(action) {
  const buttons = [el("startBinding"), el("confirmBinding"), el("syncBinding"), el("cancelBinding")];
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const response = await fetch(`/api/session/${action}`, {method:"POST"});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "操作失败");
    state.session = result;
    renderSession();
    if (action === "confirm" || action === "sync") {
      await Promise.all([loadDelivery(), loadSession()]);
    }
  } catch (error) { el("sessionTitle").textContent = "账号登录未完成"; el("sessionMessage").textContent = error.message; }
  finally { buttons.forEach((button) => { button.disabled = false; }); }
}

async function loadProducts() {
  const [accountsResponse, productsResponse, listingsResponse] = await Promise.all([fetch("/api/accounts"), fetch("/api/products"), state.safeMode ? Promise.resolve(new Response("[]")) : fetch("/api/listings")]);
  if (!accountsResponse.ok || !productsResponse.ok || !listingsResponse.ok) throw new Error("读取商品失败");
  const accounts = await accountsResponse.json();
  state.account = accounts.find((account) => account.is_active) || (state.safeMode ? null : accounts[0]) || null;
  state.products = await productsResponse.json();
  state.listings = await listingsResponse.json();
  renderAutoReplyTestProducts();
  el("accountName").textContent = state.account?.name || (state.safeMode ? "安全演练：未选定账号" : "七月账号");
  render();
}

function centsToInput(value) { return value == null ? "" : (value / 100).toFixed(2); }
function inputToCents(value) { return value === "" ? null : Math.round(Number(value) * 100); }

function renderKnowledgeEditor(product) {
  el("knowledgeFolderPath").value = product.knowledge_source_path || "";
  const chars = Number(product.knowledge_chars || 0);
  const files = Number(product.knowledge_file_count || 0);
  if (product.knowledge_source_path && chars > 0) {
    el("knowledgeStatus").textContent = `已载入 ${files} 个文件 · ${chars.toLocaleString()} 字${product.knowledge_updated_at ? ` · ${product.knowledge_updated_at}` : ""}`;
  } else if (chars > 0) {
    el("knowledgeStatus").textContent = `已从商品资料自动提取 ${chars.toLocaleString()} 字；也可改用自选文件夹`;
  } else {
    el("knowledgeStatus").textContent = "商品资料缺失，请选择一个本地文件夹";
  }
}

function openEdit(dirName) {
  const product = state.products.find((item) => item.dir_name === dirName);
  if (!product) return;
  el("editDirName").value = product.dir_name;
  el("dialogTitle").textContent = product.name;
  el("enabledForAccount").checked = Boolean(product.enabled_for_account);
  const legacyBlocked = product.catalog_status === "legacy" && !product.enabled_for_account;
  el("enabledForAccount").disabled = legacyBlocked;
  el("legacyNotice").hidden = !legacyBlocked;
  el("shareUrl").value = product.share_url || "";
  el("shareCode").value = product.share_code || "";
  el("shareVerificationStatus").textContent = product.delivery_issues?.length ? "待核验/修复：" + product.delivery_issues.join(", ") : "当前交付资料已人工核验（非在线检查）";
  state.editFingerprint = product.fulfillment_fingerprint;
  el("suggestedPrice").value = centsToInput(product.suggested_price_cents);
  el("confirmedPrice").value = centsToInput(product.confirmed_price_cents);
  el("listingUrl").value = product.listing_url || "";
  el("listingStatus").value = product.listing_status;
  state.editBaseline = readEditFields();
  renderKnowledgeEditor(product);
  el("formError").textContent = "";
  el("editDialog").showModal();
}

async function updateKnowledgeFolder(mode, button) {
  const dirName = el("editDirName").value;
  const old = button.textContent;
  button.disabled = true;
  button.textContent = mode === "pick" ? "等待选择…" : mode === "clear" ? "清除中…" : "载入中…";
  el("formError").textContent = "";
  try {
    const options = mode === "pick"
      ? {method:"POST"}
      : mode === "clear"
        ? {method:"DELETE"}
        : {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({path:el("knowledgeFolderPath").value.trim()})};
    const suffix = mode === "pick" ? "/pick" : "";
    const response = await fetch(`/api/products/${encodeURIComponent(dirName)}/knowledge-folder${suffix}`, options);
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "商品资料载入失败");
    if (result.cancelled) return;
    await loadProducts();
    const refreshedProduct = state.products.find((item) => item.dir_name === dirName) || result.product;
    renderKnowledgeEditor(refreshedProduct);
    const skipped = Number(result.skipped_files || 0);
    showActionNotice(mode === "clear"
      ? "已清除自选资料库，恢复使用商品目录自动提取的资料。"
      : `资料库已更新：读取 ${result.file_count} 个文件、${Number(result.chars || 0).toLocaleString()} 字${skipped ? `，另跳过 ${skipped} 个文件` : ""}。`);
  } catch (error) {
    el("formError").textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = old;
  }
}

function readEditFields() {
  return {enabled_for_account:el("enabledForAccount").checked, share_url:el("shareUrl").value || null, share_code:el("shareCode").value.trim(), suggested_price_cents:inputToCents(el("suggestedPrice").value), confirmed_price_cents:inputToCents(el("confirmedPrice").value), listing_url:el("listingUrl").value || null, listing_status:el("listingStatus").value};
}

function editedFields() {
  return Object.fromEntries(Object.entries(readEditFields()).filter(([key, value]) => value !== state.editBaseline[key]));
}

async function confirmShare(revoke = false) {
  if (Object.keys(editedFields()).length) { el("formError").textContent = "请先保存修改并重新打开，再核验当前资料"; return; }
  const dirName = el("editDirName").value;
  const response = await fetch(`/api/products/${encodeURIComponent(dirName)}${revoke ? "" : "/verify-share"}`, {
    method: revoke ? "PATCH" : "POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(revoke ? {share_verified:false} : {fingerprint:state.editFingerprint}),
  });
  if (!response.ok) { const result = await response.json(); el("formError").textContent = result.detail || "核验未完成"; return; }
  await loadProducts();
  openEdit(dirName);
}

async function saveEdit(event) {
  event.preventDefault();
  const dirName = el("editDirName").value;
  const payload = editedFields();
  const response = await fetch(`/api/products/${encodeURIComponent(dirName)}`, {method:"PATCH", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
  if (!response.ok) { const error = await response.json().catch(() => ({})); el("formError").textContent = error.detail || "保存失败"; return; }
  el("editDialog").close();
  await loadProducts();
}

async function copyListing(dirName, button) {
  const response = await fetch(`/api/products/${encodeURIComponent(dirName)}/copy`);
  if (!response.ok) return;
  await navigator.clipboard.writeText(await response.text());
  const old = button.textContent; button.textContent = "已复制"; setTimeout(() => { button.textContent = old; }, 1200);
}

async function detectListing(dirName, button) {
  const old = button.textContent; button.disabled = true; button.textContent = "检测中…";
  try {
    const response = await fetch(`/api/products/${encodeURIComponent(dirName)}/detect-listing`, {method:"POST"});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "检测失败");
    if (result.matched) { showActionNotice(`已绑定《${result.product.title || result.product.name}》的闲鱼地址。`); await loadProducts(); }
    else showActionNotice("暂未检测到标题匹配的已发布商品。", true);
  } catch (error) { showActionNotice(error.message, true); }
  finally { button.disabled = false; button.textContent = old; }
}

async function mapListingProduct(itemId, select, button) {
  const dirName = select.value;
  if (!dirName) return;
  const old = button.textContent;
  button.disabled = true;
  button.textContent = "关联中…";
  try {
    const response = await fetch(`/api/listings/${encodeURIComponent(itemId)}/product`, {
      method:"PUT",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({product_dir_name:dirName}),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "商品关联失败");
    await loadProducts();
    showActionNotice(`已按闲鱼商品号 ${itemId} 关联本地商品，今后修改标题也不会丢失匹配。`);
  } catch (error) {
    showActionNotice(error.message, true);
    button.disabled = false;
    button.textContent = old;
  }
}

el("scanButton").addEventListener("click", async () => {
  el("scanButton").disabled = true; el("scanButton").textContent = "同步中…";
  try {
    const response = await fetch("/api/scan", {method:"POST"});
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || "同步失败");
    }
    const result = await response.json();
    state.products = result.products;
    if (Array.isArray(result.listings)) {
      state.listings = result.listings;
      render();
    } else {
      // Compatible with a manager process that has not restarted yet.
      await loadProducts();
    }
    const remote = result.listing_sync;
    if (remote?.ok) {
      showActionNotice(`同步完成：读取闲鱼在售 ${remote.remote_count} 件，新增 ${remote.new_count} 件，自动匹配 ${remote.matched_count} 件。`);
    } else if (remote) {
      showActionNotice(`本地商品库已更新，但闲鱼在售同步失败：${remote.error || "未知原因"}`, true);
    } else {
      showActionNotice("本地商品库同步完成；请重启管理系统后启用闲鱼在售同步。", true);
    }
  }
  catch (error) { showActionNotice(error.message, true); }
  finally { el("scanButton").disabled = false; el("scanButton").textContent = "同步本地与闲鱼"; }
});
el("searchInput").addEventListener("input", renderProducts);
el("statusFilter").addEventListener("change", renderProducts);
el("productList").addEventListener("click", (event) => {
  const editButton = event.target.closest(".edit-button");
  const copyButton = event.target.closest(".copy-button");
  const detectButton = event.target.closest(".detect-button");
  const mapButton = event.target.closest(".map-button");
  if (editButton) openEdit(editButton.dataset.dir);
  if (copyButton) copyListing(copyButton.dataset.dir, copyButton);
  if (detectButton) detectListing(detectButton.dataset.dir, detectButton);
  if (mapButton) {
    const select = mapButton.closest(".mapping-controls")?.querySelector(".mapping-select");
    if (select) mapListingProduct(mapButton.dataset.itemId, select, mapButton);
  }
});
el("productList").addEventListener("change", (event) => {
  const select = event.target.closest(".mapping-select");
  if (!select) return;
  const button = select.closest(".mapping-controls")?.querySelector(".map-button");
  if (button) button.disabled = !select.value;
});
el("productList").addEventListener("input", (event) => {
  const search = event.target.closest(".mapping-search");
  if (!search) return;
  const controls = search.closest(".mapping-controls");
  const select = controls?.querySelector(".mapping-select");
  const button = controls?.querySelector(".map-button");
  if (!select) return;
  const query = search.value.trim().toLowerCase();
  let visibleCount = 0;
  Array.from(select.options).forEach((option, index) => {
    if (index === 0) return;
    const visible = !query || String(option.dataset.search || "").includes(query);
    option.hidden = !visible;
    if (visible) visibleCount += 1;
  });
  const selected = select.selectedOptions[0];
  if (selected?.hidden) select.value = "";
  select.options[0].textContent = visibleCount ? `手动选择本地商品…（${visibleCount}）` : "没有匹配的本地商品";
  if (button) button.disabled = !select.value;
});
el("editForm").addEventListener("submit", saveEdit);
el("pickKnowledgeFolder").addEventListener("click", (event) => updateKnowledgeFolder("pick", event.currentTarget));
el("loadKnowledgeFolder").addEventListener("click", (event) => updateKnowledgeFolder("load", event.currentTarget));
el("clearKnowledgeFolder").addEventListener("click", (event) => updateKnowledgeFolder("clear", event.currentTarget));
el("closeDialog").addEventListener("click", () => el("editDialog").close());
el("cancelEdit").addEventListener("click", () => el("editDialog").close());
el("startBinding").addEventListener("click", () => sessionAction("start"));
el("confirmBinding").addEventListener("click", () => sessionAction("confirm"));
el("syncBinding").addEventListener("click", () => sessionAction("sync"));
el("cancelBinding").addEventListener("click", () => sessionAction("cancel"));
el("startDelivery").addEventListener("click", () => deliveryAction("start"));
el("confirmShare").addEventListener("click", () => confirmShare(false));
el("revokeShare").addEventListener("click", () => confirmShare(true));
el("probeDelivery").addEventListener("click", () => deliveryAction("probe"));
el("stopDelivery").addEventListener("click", () => deliveryAction("stop"));
el("autoReplyForm").addEventListener("submit", saveAutoReply);
el("testAutoReply").addEventListener("click", testAutoReply);
el("autoReplyTestProduct").addEventListener("change", (event) => { event.currentTarget.dataset.userSelected = "true"; });
el("safetyForm").addEventListener("submit", saveSafety);
el("resetCircuit").addEventListener("click", resetCircuit);
el("autoReplyRecords").addEventListener("click", (event) => {
  const button = event.target.closest(".resume-chat");
  if (button) resumeAutoReplyChat(button.dataset.chat);
});

async function initializePage() {
  const response = await fetch("/api/health");
  if (!response.ok) throw new Error("读取 API 状态失败");
  state.safeMode = (await response.json()).safe_mode === true;
  if (state.safeMode) {
    await loadProducts();
    el("sessionTitle").textContent = "安全演练模式";
    el("sessionMessage").textContent = "仅开放本地查询，业务操作被禁止；切换模式需要重启服务。";
    document.querySelectorAll("button, input, select, textarea").forEach((node) => { node.disabled = true; });
    return;
  }
  await Promise.all([loadProducts(), loadSession(), loadDelivery(), loadAutoReply(true), loadSafety(true)]);
}

initializePage().catch((error) => {
  el("productList").innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`;
});
