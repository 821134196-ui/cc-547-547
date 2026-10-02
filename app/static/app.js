/* 装修工程结算前端（无框架） */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = { data: null, actorId: null };

function yuan(cents) {
  const neg = cents < 0;
  const a = Math.abs(Math.round(cents));
  const s = (a / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (neg ? "-" : "") + "¥" + s;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const STATUS_TEXT = {
  open: "待整改", submitted: "待客户签认", passed: "已签认",
  rework: "需返工", cancelled: "已取消",
};

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-User-Id": state.actorId },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 ${res.status}`);
  return data;
}
async function refresh() {
  const res = await fetch("/api/state");
  state.data = await res.json();
  render();
}
let toastTimer;
function toast(msg, isErr) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), 3200);
}
async function guard(fn) {
  try { await fn(); } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------ role/actor */

function renderActors() {
  const sel = $("#actor-select");
  if (!sel.options.length) {
    for (const u of state.data.users) {
      const o = document.createElement("option");
      o.value = u.id; o.textContent = u.name;
      sel.appendChild(o);
    }
    state.actorId = Number(sel.value);
    sel.addEventListener("change", () => { state.actorId = Number(sel.value); render(); });
  }
  const u = state.data.users.find(x => x.id === state.actorId);
  const badge = $("#actor-role");
  badge.textContent = u.role === "customer" ? "客户" : "项目经理";
  badge.className = "role-badge " + u.role;
}
function actor() { return state.data.users.find(x => x.id === state.actorId); }

/* ------------------------------------------------------------- settlement */

function renderSettlement() {
  const s = state.data.settlement;
  const cards = [
    { cls: "", label: "合同金额（含调整）", val: yuan(s.contract_cents),
      sub: "原合同 + 签证调整" },
    { cls: "paid", label: "已付款", val: yuan(s.paid.total_cents),
      sub: `进度 ${yuan(s.paid.progress_cents)} · 尾款 ${yuan(s.paid.tail_cents)} · 质保 ${yuan(s.paid.retention_cents)}` },
    { cls: "tail", label: "可付尾款（已签认）", val: yuan(s.tail.available_cents),
      sub: "照片确认并签认后可申请" },
    { cls: "retention", label: "质保金留存", val: yuan(s.retention.held_cents),
      sub: s.retention.due ? "已到期、无未结索赔，可结清" : `到期日 ${s.warranty_end || "—"}` },
  ];
  $("#settle-cards").innerHTML = cards.map(c => `
    <div class="card ${c.cls}">
      <div class="label">${c.label}</div>
      <div class="value">${c.val}</div>
      <div class="sub">${esc(c.sub)}</div>
    </div>`).join("");

  const pending = s.tail.pending_cents + s.tail.unallocated_cents;
  $("#balance-row").innerHTML =
    `已付 <b>${yuan(s.paid.total_cents)}</b>`
    + (s.paid.in_flight_cents ? ` ＋ 在途回执 <b>${yuan(s.paid.in_flight_cents)}</b>` : "")
    + ` ＋ 可付尾款 <b>${yuan(s.tail.available_cents)}</b>`
    + ` ＋ 待处理 ${yuan(pending)} ＋ 质保金留存 <b>${yuan(s.retention.held_cents)}</b>`
    + ` ＝ <b>${yuan(s.contract_cents)}</b>`;

  const rows = state.data.payments;
  $("#payments-table").innerHTML = `
    <tr><th>#</th><th>类型</th><th class="num">金额</th><th>状态</th><th>回执号</th><th>付款时间</th></tr>`
    + rows.map(p => `<tr>
        <td>${p.request_id}</td>
        <td>${{ tail: "尾款", retention: "质保金", progress: "进度款" }[p.req_kind] || p.req_kind}</td>
        <td class="num">${yuan(p.amount_cents)}</td>
        <td><span class="tag ${p.status}">${p.status === "paid" ? "已到账" : "回执延迟中"}</span></td>
        <td><code>${esc(p.receipt_no || "")}</code></td>
        <td>${esc(p.paid_at || "—")}</td>
      </tr>`).join("")
    + `<tr class="total"><td colspan="2">已到账合计</td><td class="num">${yuan(
        rows.filter(p => p.status === "paid").reduce((a, p) => a + p.amount_cents, 0))}</td><td colspan="3"></td></tr>`;
}

/* ---------------------------------------------------------- rectifications */

function rectCanPay(r) {
  return r.status === "passed" && r.signed_by && !r.requested
    && !state.data.rectifications.some(ch => ch.parent_id === r.id &&
       ["open", "submitted", "rework"].includes(ch.status));
}

function renderRects() {
  const isMgr = actor().role === "manager";
  const html = state.data.rectifications.map(r => {
    const child = state.data.rectifications.find(ch => ch.parent_id === r.id
      && ["open", "submitted", "rework"].includes(ch.status));
    const blocked = r.status === "passed" && r.signed_by && !r.requested && child;
    let tagCls = r.status;
    if (r.requested) tagCls = "paid";
    else if (blocked) tagCls = "blocked";
    const tagText = r.requested ? "尾款已申请"
      : blocked ? `新问题待处理：${child.title}`
      : STATUS_TEXT[r.status];

    const photos = r.photos.map(ph => `
      <figure>
        <img src="/api/photos/${encodeURI(ph.file_path)}" alt="${esc(ph.caption || "")}" loading="lazy">
        <figcaption>${esc(ph.caption || "")}<br><small>${esc(ph.uploaded_at)}</small></figcaption>
      </figure>`).join("");

    let actions = "";
    if (isMgr && (r.status === "open" || r.status === "rework") && !r.parent_id) {
      actions = `<button class="btn primary small" data-act="submit" data-id="${r.id}">
                 上传整改照片并提交复验</button>`;
    }
    if (!isMgr && r.status === "submitted") {
      actions = `<button class="btn primary small" data-act="sign" data-id="${r.id}">
                 确认照片并签认（客户）</button>`;
    }
    if (!isMgr && r.status === "passed" && r.signed_by && !r.requested && !child) {
      actions += `<a class="hint" href="#tail">→ 前往尾款页申请放款</a>`;
    }

    const childBanner = child ? `
      <div class="child-issue">客户复验后追加新问题：<b>${esc(child.title)}</b>
        （未验收，父项尾款暂缓）
        ${isMgr && child.status === "open"
          ? `<button class="btn small" style="margin-left:8px" data-act="submit" data-id="${child.id}">提交整改照片</button>`
          : ""}
      </div>` : "";

    return `<div class="rect" data-id="${r.id}">
      <button class="rect-head" data-act="toggle">
        <span class="chev">▶</span>
        <span class="rect-title">
          <strong>${r.parent_id ? "↳ " : ""}${esc(r.title)}</strong>
          <small>${esc(r.description || "")}</small>
        </span>
        <span class="rect-amount">${yuan(r.tail_cents)}</span>
        <span class="tag ${tagCls}">${esc(tagText)}</span>
      </button>
      <div class="rect-body">
        ${childBanner}
        <div class="photos">${photos || '<small class="hint">暂无照片</small>'}</div>
        <div class="meta">
          提交复验：${esc(r.submitted_at || "—")}<br>
          客户签认：${r.signed_at ? `${esc(r.signed_at)}（${esc(r.signer_name)}）` : "—"}<br>
          签名核验：${r.signature ? (r.signature_valid ? "✓ 通过" : "✗ 失败") : "—"}
          <code style="margin-left:6px">${esc(r.signature || "")}</code>
        </div>
        <div class="actions">${actions}</div>
      </div>
    </div>`;
  }).join("");
  $("#rect-list").innerHTML = html;

  $("#rect-list").onclick = (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const id = Number(btn.dataset.id);
    if (btn.dataset.act === "toggle") {
      btn.closest(".rect").classList.toggle("open");
      return;
    }
    if (btn.dataset.act === "submit") {
      guard(async () => {
        await api("/api/rects/submit", { rect_id: id, pattern: "grid", caption: "整改后复验照片" });
        toast("整改照片已提交，等待客户签认");
        await refresh();
      });
    }
    if (btn.dataset.act === "sign") {
      guard(async () => {
        await api("/api/rects/sign", { rect_id: id });
        toast("已签认，对应尾款可申请释放");
        await refresh();
      });
    }
  };
}

/* ------------------------------------------------------------------- tail */

function renderTail() {
  const payable = state.data.rectifications.filter(rectCanPay);
  $("#tail-payable").innerHTML = payable.length
    ? payable.map(r => `
        <div class="payable-item">
          <input type="checkbox" class="tail-check" value="${r.id}" id="tc-${r.id}" checked>
          <label for="tc-${r.id}">${esc(r.title)}
            <small>签认于 ${esc(r.signed_at)}</small></label>
          <span class="amt">${yuan(r.tail_cents)}</span>
        </div>`).join("")
    : '<p class="hint">暂无可释放尾款（未签认或已申请的整改项不计入）。</p>';

  const receipts = state.data.payments.filter(p => p.req_kind === "tail");
  $("#receipt-table").innerHTML = `
    <tr><th>申请#</th><th class="num">金额</th><th>状态</th><th>回执号</th><th>操作</th></tr>`
    + receipts.map(p => `
      <tr>
        <td>${p.request_id}</td>
        <td class="num">${yuan(p.amount_cents)}</td>
        <td><span class="tag ${p.status}">${p.status === "paid" ? "已到账" : "回执延迟中"}</span></td>
        <td><code>${esc(p.receipt_no || "")}</code></td>
        <td>
          <button class="btn small" data-receipt="${p.request_id}" data-mode="replay">回执重放</button>
          ${p.status !== "paid" ? `<button class="btn small primary" data-receipt="${p.request_id}" data-mode="confirm">延迟回执到达</button>` : ""}
        </td>
      </tr>`).join("");

  $("#receipt-table").onclick = (e) => {
    const b = e.target.closest("[data-receipt]");
    if (!b) return;
    guard(async () => {
      const r = await api("/api/payments/receipt",
        { request_id: Number(b.dataset.receipt), action: b.dataset.mode });
      toast(b.dataset.mode === "replay"
        ? `重放结果：同一回执 ${r.receipt_no}，付款指令仍为 ${r.payment_rows} 笔`
        : `回执已确认：${r.receipt_no}`);
      await refresh();
    });
  };

  $("#btn-release-tail").onclick = () => guard(async () => {
    const ids = $$(".tail-check:checked").map(c => Number(c.value));
    if (!ids.length) return toast("请至少勾选一条已签认整改项", true);
    const r = await api("/api/payments/tail",
      { rect_ids: ids, delay_receipt: $("#delay-receipt").checked });
    $("#tail-result").innerHTML =
      `<div class="result-ok">生成一笔付款指令 #${r.request_id}：${yuan(r.amount_cents)}，`
      + `回执 ${esc(r.receipt_no)}（${r.status === "paid" ? "即时到账" : "回执延迟中"}）</div>`;
    toast("放款指令已生成");
    await refresh();
  });
}

/* -------------------------------------------------------------- warranty */

function renderWarranty() {
  const s = state.data.settlement;
  const r = s.retention;
  const box = $("#retention-box");
  box.innerHTML = `
    <div class="balance" style="margin-bottom:10px">
      质保金留存 <b>${yuan(r.held_cents)}</b>
      ｜索赔扣减预留 ${yuan(r.claims_cents)}
      ｜到期日 <b>${esc(s.warranty_end || "未验收")}</b>
      ${r.due ? '｜<span class="tag paid">已到期可结清</span>'
              : '｜<span class="tag open">未到期或有未结项</span>'}
    </div>
    <p class="hint">质保金在约定期满且无未结索赔时另行结清；尾款放款不会动用质保金。</p>
    <div class="actions">
      <button class="btn primary" id="btn-retention" ${r.due ? "" : "disabled"}>申请结清质保金</button>
      <span class="hint">${r.due ? "" : "未到期、存在未结返修/索赔时按钮禁用"}</span>
    </div>`;
  $("#btn-retention")?.addEventListener("click", () => guard(async () => {
    const res = await api("/api/payments/retention", {});
    toast(`质保金付款指令 #${res.request_id}：${yuan(res.amount_cents)}`);
    await refresh();
  }));

  const txt = { open: "待修复", repaired: "已修复待确认", confirmed: "客户已确认", claim: "索赔扣减" };
  const isMgr = actor().role === "manager";
  $("#repair-list").innerHTML = state.data.warranty_repairs.map(w => `
    <div class="payable-item">
      <label><b>${esc(w.title)}</b><br><small>${esc(w.basis || "")}</small></label>
      <span class="tag ${w.status === "confirmed" ? "paid" : w.status === "claim" ? "blocked" : "submitted"}">${txt[w.status]}</span>
      ${w.cost_cents ? `<span class="amt">${yuan(w.cost_cents)}</span>` : ""}
      <span>
        ${isMgr && w.status === "open" ? `<button class="btn small" data-wr="${w.id}" data-act="repair">登记修复</button>` : ""}
        ${!isMgr && w.status === "repaired" ? `<button class="btn small primary" data-wr="${w.id}" data-act="confirm">确认</button>` : ""}
      </span>
    </div>`).join("") || '<p class="hint">质保期内暂无返修。</p>';

  $("#repair-list").onclick = (e) => {
    const b = e.target.closest("[data-wr]");
    if (!b) return;
    guard(async () => {
      const id = Number(b.dataset.wr);
      if (b.dataset.act === "repair") {
        await api("/api/warranty/repair", { id });
      } else {
        const asClaim = confirm("是否按索赔处理（从质保金扣减）？");
        await api("/api/warranty/confirm", { id, as_claim, cost: asClaim ? "500" : 0 });
      }
      await refresh();
    });
  };
  $("#btn-repair-open").onclick = () => guard(async () => {
    const title = $("#repair-title").value.trim(), basis = $("#repair-basis").value.trim();
    if (!title) return toast("请填写报修问题", true);
    await api("/api/warranty/open", { title, basis });
    $("#repair-title").value = ""; $("#repair-basis").value = "";
    await refresh();
  });
}

/* ---------------------------------------------------------------- records */

function renderRecords() {
  $("#dispute-list").innerHTML = state.data.disputes.map(d => `
    <div class="payable-item">
      <label><b>${esc(d.subject)}</b>
        <br><small>${esc(d.detail || "")}</small>
        <br><small>依据：${esc(d.basis || "")} · ${esc(d.raiser_name)} · ${esc(d.raised_at)}</small></label>
      <span class="tag ${d.status === "resolved" ? "paid" : "submitted"}">
        ${d.status === "resolved" ? "已解决" : "待处理"}</span>
      ${d.status === "open" ? `<button class="btn small" data-dp="${d.id}">标记解决</button>` : ""}
    </div>`).join("") || '<p class="hint">暂无争议。</p>';
  $("#dispute-list").onclick = (e) => {
    const b = e.target.closest("[data-dp]");
    if (!b) return;
    guard(async () => { await api("/api/disputes/resolve", { id: Number(b.dataset.dp) }); await refresh(); });
  };
  $("#btn-dispute-add").onclick = () => guard(async () => {
    const subject = $("#dispute-subject").value.trim(), basis = $("#dispute-basis").value.trim();
    if (!subject || !basis) return toast("争议必须填写主题与依据", true);
    await api("/api/disputes", { subject, basis });
    $("#dispute-subject").value = ""; $("#dispute-basis").value = "";
    await refresh();
  });

  $("#adjust-table").innerHTML = `
    <tr><th>#</th><th class="num">调整额</th><th>原因</th><th>依据</th><th>登记人</th></tr>`
    + state.data.adjustments.map(a => `<tr>
      <td>${a.id}</td><td class="num" style="color:${a.delta_cents >= 0 ? "var(--green)" : "var(--red)"}">
        ${a.delta_cents >= 0 ? "+" : ""}${yuan(a.delta_cents)}</td>
      <td>${esc(a.reason)}</td><td>${esc(a.basis)}</td><td>${esc(a.creator_name)}</td></tr>`).join("");
  $("#btn-adj-add").onclick = () => guard(async () => {
    const delta = $("#adj-delta").value.trim(), reason = $("#adj-reason").value.trim(),
          basis = $("#adj-basis").value.trim();
    if (!delta || !reason || !basis) return toast("调整金额、原因、依据均必填", true);
    await api("/api/contract/adjust", { delta, reason, basis });
    ["#adj-delta", "#adj-reason", "#adj-basis"].forEach(id => ($(id).value = ""));
    toast("合同价已调整，结算页金额已联动");
    await refresh();
  });

  $("#mgr-history").innerHTML = state.data.manager_history.map(h => `
    <div class="payable-item">
      <label><b>${esc(h.old_name || "—")}</b> → <b>${esc(h.new_name)}</b>
        <br><small>原因：${esc(h.reason)}<br>依据：${esc(h.basis)}</small></label>
      <span class="tag paid">${esc(h.changed_at)}</span>
    </div>`).join("");

  $("#completion-table").innerHTML = `
    <tr><th>项目</th><th class="num">数量</th><th>单位</th><th>录入人</th><th>时间</th></tr>`
    + state.data.completions.map(w => `<tr>
      <td>${esc(w.item_name)}</td><td class="num">${w.quantity}</td><td>${esc(w.unit)}</td>
      <td>${esc(w.enterer_name)}</td><td>${esc(w.entered_at)}</td></tr>`).join("");
  $("#btn-wc-add").onclick = () => guard(async () => {
    const item_name = $("#wc-item").value.trim(), qty = $("#wc-qty").value.trim(),
          unit = $("#wc-unit").value.trim();
    if (!item_name || !qty || !unit) return toast("请填写完整", true);
    try {
      await api("/api/completions", { item_name, quantity: qty, unit });
      ["#wc-item", "#wc-qty", "#wc-unit"].forEach(id => ($(id).value = ""));
      await refresh();
    } catch (e) { toast("录入被拒绝：" + e.message, true); }
  });

  $("#event-log").textContent = state.data.events.map(e =>
    `${e.ts}  ${e.actor_name || "系统"}  ${e.entity}#${e.entity_id ?? ""}  ${e.action}  ${e.payload}`
  ).join("\n");
}

/* ----------------------------------------------------------------- render */

function render() {
  if (!state.data) return;
  renderActors();
  const s = state.data.settlement;
  $("#project-name").textContent = s.project_name;
  $("#project-meta").textContent =
    `项目经理：${s.manager} · 竣工验收 ${s.acceptance_date || "—"} · 数据版本 v${s.version}`;
  renderSettlement();
  renderRects();
  renderTail();
  renderWarranty();
  renderRecords();
}

$$("#tabs button").forEach(b => b.addEventListener("click", () => {
  $$("#tabs button").forEach(x => x.classList.remove("active"));
  $$(".tab").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  $("#tab-" + b.dataset.tab).classList.add("active");
}));

refresh().catch(e => toast(e.message, true));
