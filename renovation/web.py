"""Web 结算台：单文件标准库实现（http.server），无第三方依赖。

页面：合同结算（金额并列）、整改清单（逐条展开照片与签认时间）、
尾款双人签放、质保金到期、付款台账与回执、依据与事件流。
"""
from __future__ import annotations

import html
import mimetypes
import threading
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from .adapters import MockPaymentGateway, PaymentError, ReceiptPending
from .db import connect
from .money import cents_to_yuan
from .seed import CONTRACT_ID, seed
from .service import DomainError, PermissionDenied, SettlementService

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "renovation.db"
PHOTO_DIR = DATA_DIR / "photos"

INK = "#1f2933"
ORANGE = "#e8611a"

_USERS: dict[str, str] = {}      # 懒加载 id -> name
_LOCK = threading.RLock()


class AppState:
    def __init__(self, data_dir: Path | str = DATA_DIR, contract_id: str = CONTRACT_ID) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "renovation.db"
        self.photo_dir = self.data_dir / "photos"
        self.contract_id = contract_id
        self.gateway = MockPaymentGateway()

    def service(self) -> SettlementService:
        con = connect(self.db_path)
        rows = con.execute("SELECT id,amount_cents,status,gateway_ref FROM payment_instructions")
        self.gateway.hydrate(rows)
        global _USERS
        if not _USERS:
            _USERS = {r["id"]: r["name"] for r in con.execute("SELECT id,name FROM users")}
        return SettlementService(con, gateway=self.gateway, photo_dir=self.photo_dir)


STATE = AppState()


# ----------------------------------------------------------------- 小工具

def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def yn(cents: int) -> str:
    return "¥" + cents_to_yuan(cents)


STATUS_LABEL = {
    "open": ("待整改", "pend"),
    "submitted": ("待客户确认", "pend"),
    "rejected": ("新问题·待返工", "pend"),
    "passed": ("已验收通过", "done"),
}
PAY_STATUS = {
    "pending": ("待双人签放", "pend"),
    "executed": ("已出账·待回执", "pend"),
    "receipt_confirmed": ("回执已确认", "done"),
}
KIND_LABEL = {"progress": "进度款", "final": "整改尾款", "retention": "质保金"}


def chip(status: str, table: dict[str, tuple[str, str]]) -> str:
    label, cls = table.get(status, (status, "pend"))
    return f'<span class="chip {cls}">{esc(label)}</span>'


def page(title: str, body: str, svc: SettlementService | None = None,
         as_user: str = "", msg: str = "", err: str = "") -> bytes:
    identities = ""
    if svc is not None:
        c = svc.contract(CONTRACT_ID)
        opts = [(c["customer_id"], "客户"), (c["manager_id"], "现任项目经理")]
        old = svc.con.execute(
            "SELECT manager_id FROM pm_tenures WHERE contract_id=? AND ended_at IS NOT NULL "
            "ORDER BY seq LIMIT 1", (CONTRACT_ID,)).fetchone()
        if old:
            opts.append((old["manager_id"], "前任项目经理"))
        links = []
        for uid, label in opts:
            name = _USERS.get(uid, uid)
            cls = "idlink on" if uid == as_user else "idlink"
            links.append(f'<a class="{cls}" href="/settlement?as={urllib.parse.quote(uid)}">'
                         f'{esc(label)}·{esc(name)}</a>')
        identities = '<div class="identity">当前身份：' + "　".join(links) + "</div>"

    flash = ""
    if msg:
        flash += f'<div class="flash ok">{esc(msg)}</div>'
    if err:
        flash += f'<div class="flash err">{esc(err)}</div>'

    doc = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · 装修工程结算台</title>
<style>
:root{{--ink:{INK};--ink2:#5b6470;--line:#e3e1db;--bg:#f5f4f1;--white:#fff;
--orange:{ORANGE};--orange-soft:#fdeee2;--ok:#27613f;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;}}
.wrap{{max-width:1080px;margin:0 auto;padding:16px 14px 64px}}
header.top{{background:var(--white);border-bottom:1px solid var(--line);padding:14px 0}}
header.top .wrap{{padding-bottom:0;padding-top:0}}
h1{{font-size:19px;margin:2px 0 4px}}
.sub{{color:var(--ink2);font-size:13px}}
.identity{{margin-top:10px;font-size:13px;color:var(--ink2)}}
.idlink{{display:inline-block;padding:3px 10px;border:1px solid var(--line);border-radius:14px;
margin-right:6px;color:var(--ink2);text-decoration:none;background:#faf9f7;white-space:nowrap}}
.idlink.on{{border-color:var(--orange);color:var(--orange);background:var(--orange-soft);
font-weight:600}}
.flash{{padding:9px 13px;border-radius:8px;margin:12px 0;font-size:14px}}
.flash.ok{{background:#e8f3ec;color:var(--ok);border:1px solid #bfe0cb}}
.flash.err{{background:var(--orange-soft);color:#9a3d08;border:1px solid #f3c79f}}
/* 金额四宫格：任何屏宽整行可见 */
.tiles{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:14px 0}}
.tile{{background:var(--white);border:1px solid var(--line);border-radius:10px;padding:10px 8px;
min-width:0}}
.tile .lab{{font-size:12px;color:var(--ink2);white-space:nowrap;overflow:hidden;
text-overflow:ellipsis}}
.tile .amt{{font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-.02em;
white-space:nowrap;margin-top:3px;font-size:clamp(14px,3.6vw,24px)}}
.tile .meta{{font-size:11px;color:var(--ink2);margin-top:3px;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}}
.amt.orange{{color:var(--orange)}}
.balance{{background:var(--white);border:1px solid var(--line);border-radius:10px;padding:10px 13px;
font-size:13px;color:var(--ink2);margin-bottom:14px}}
.balance b{{color:var(--ink);font-variant-numeric:tabular-nums}}
.balance .tag-ok{{color:var(--ok);font-weight:700}}
.balance .tag-bad{{color:var(--orange);font-weight:700}}
section.card{{background:var(--white);border:1px solid var(--line);border-radius:10px;
padding:4px 16px 14px;margin:14px 0}}
h2{{font-size:16px;margin:14px 0 8px;display:flex;align-items:center;gap:8px}}
h2 .hint{{font-size:12px;color:var(--ink2);font-weight:400}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}
th,td{{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{color:var(--ink2);font-weight:600;font-size:12.5px}}
td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.chip{{display:inline-block;padding:1px 9px;border-radius:11px;font-size:12px;white-space:nowrap}}
.chip.done{{background:#eef0f2;color:var(--ink);border:1px solid #d7dade}}
.chip.pend{{background:var(--orange-soft);color:var(--orange);border:1px solid #f3c79f;
font-weight:600}}
details.rect{{border:1px solid var(--line);border-radius:9px;margin:8px 0;background:#fff}}
details.rect[open]{{box-shadow:0 1px 0 #eee inset}}
summary{{cursor:pointer;list-style:none;padding:10px 12px;display:flex;gap:10px;
align-items:center;flex-wrap:wrap}}
summary::-webkit-details-marker{{display:none}}
summary .code{{font-weight:700;min-width:52px}}
summary .ttl{{flex:1;min-width:120px}}
summary .loc{{color:var(--ink2);font-size:12.5px}}
summary .hold{{font-variant-numeric:tabular-nums;white-space:nowrap}}
.arrow{{color:var(--ink2);margin-left:auto}}
details[open] .arrow{{transform:rotate(90deg)}}
.detail-body{{padding:4px 14px 14px;border-top:1px dashed var(--line)}}
.subline{{margin:10px 0 4px;font-size:13px;font-weight:600}}
.timeline{{list-style:none;margin:6px 0;padding:0}}
.timeline li{{border-left:2px solid var(--line);padding:0 0 10px 14px;position:relative;
font-size:13px;color:var(--ink2)}}
.timeline li::before{{content:"";position:absolute;left:-5px;top:5px;width:8px;height:8px;
border-radius:50%;background:#c7c3ba}}
.timeline li.orange::before{{background:var(--orange)}}
.timeline b{{color:var(--ink)}}
.sig{{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;background:#f3f2ef;
padding:1px 6px;border-radius:5px}}
.photos{{display:flex;gap:10px;flex-wrap:wrap;margin:6px 0}}
.photos figure{{margin:0;width:230px;max-width:100%}}
.photos img{{width:100%;border:1px solid var(--line);border-radius:8px;display:block}}
.photos figcaption{{font-size:12px;color:var(--ink2);margin-top:3px}}
.btn{{display:inline-block;border:1px solid var(--orange);background:var(--orange);color:#fff;
padding:5px 14px;border-radius:7px;font-size:13px;cursor:pointer;text-decoration:none}}
.btn.gray{{background:#fff;color:var(--ink);border-color:#c9c6bf}}
.btn.mini{{padding:2px 10px;font-size:12.5px}}
form.inline{{display:inline}}
textarea,input[type=text]{{width:100%;border:1px solid #cfccc5;border-radius:7px;padding:7px 9px;
font:inherit;margin:4px 0}}
.row-actions{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}}
.note{{font-size:12.5px;color:var(--ink2)}}
.kindtag{{font-size:12px;padding:0 7px;border-radius:4px;background:#eef0f2;color:var(--ink2)}}
.kindtag.final{{background:var(--orange-soft);color:var(--orange)}}
.muted{{color:var(--ink2)}}
.signers{{font-size:12px;color:var(--ink2)}}
.signers .yes{{color:var(--ink)}}
.signers .no{{color:var(--orange);font-weight:600}}
.checkline{{margin:6px 0}}
@media (max-width:560px){{
  .wrap{{padding:10px 9px 50px}}
  .tiles{{grid-template-columns:repeat(2,minmax(0,1fr))}}
  .tile .amt{{font-size:clamp(15px,5.2vw,20px)}}
  th.hide-s,td.hide-s{{display:none}}
  .photos figure{{width:calc(50% - 5px)}}
  section.card{{padding:2px 11px 12px}}
}}
</style></head>
<body><header class="top"><div class="wrap">
<div class="sub">装修工程结算台 · 账务存于 SQLite（金额单位：分）· 支付/签名为模拟适配器</div>
<h1>{esc(title)}</h1>{identities}
</div></header>
<main class="wrap">{flash}{body}</main>
</body></html>"""
    return doc.encode("utf-8")


# ----------------------------------------------------------------- 页面渲染

def render_settlement(svc: SettlementService, as_user: str, msg: str, err: str) -> bytes:
    s = svc.settlement(CONTRACT_ID)
    c = s["contract"]

    # ---- 金额四宫格 ----
    retention_meta = f"到期日 {c['warranty_end']}" + ("·已到期" if s["warranty_matured"] else "·未到期")
    tiles = f"""
<div class="tiles">
  <div class="tile"><div class="lab">合同总价（v{c['current_version']} 当前版本）</div>
    <div class="amt">{yn(s['amount_cents'])}</div>
    <div class="meta">经合同价调整，旧版保留</div></div>
  <div class="tile"><div class="lab">已付款（出账）</div>
    <div class="amt">{yn(s['paid_total_cents'])}</div>
    <div class="meta">含已出账待回单金额</div></div>
  <div class="tile"><div class="lab">可申请尾款 / 待签放</div>
    <div class="amt orange">{yn(s['payable_final_cents'])}</div>
    <div class="meta">另待双人签放 {yn(s['pending_total_cents'])}</div></div>
  <div class="tile"><div class="lab">质保金（余额）</div>
    <div class="amt">{yn(s['retention_left_cents'])}</div>
    <div class="meta">{esc(retention_meta)}</div></div>
</div>"""

    eq = (f"合同价 <b>{yn(s['amount_cents'])}</b> = 已付 <b>{yn(s['paid_total_cents'])}</b>"
          f" + 待签放 <b>{yn(s['pending_total_cents'])}</b>"
          f" + 可付尾款 <b>{yn(s['payable_final_cents'])}</b>"
          f" + 未验收留存 <b>{yn(s['blocked_final_cents'])}</b>"
          f" + 未结算工程款 <b>{yn(s['unbilled_progress_cents'])}</b>"
          f" + 质保金余额 <b>{yn(s['retention_left_cents'])}</b>")
    tag = ('<span class="tag-ok">金额守恒校验通过 ✓</span>' if s["balanced"]
           else f'<span class="tag-bad">金额不平，差额 {yn(s["unaccounted_cents"])}</span>')
    balance = f'<div class="balance">{eq}<br>{tag}</div>'

    body = tiles + balance
    body += render_final_section(svc, s, as_user)
    body += render_rect_section(svc, s, as_user)
    body += render_retention_section(svc, s, as_user)
    body += render_payment_ledger(svc, s)
    body += render_evidence(svc, s)

    return page(c["title"], body, svc, as_user, msg, err)


def render_final_section(svc: SettlementService, s: dict, as_user: str) -> str:
    c = s["contract"]
    out = ['<section class="card"><h2>整改尾款 <span class="hint">整改照片经客户签认后，'
           '才能申请释放对应留存；客户与项目经理双人签放，只出一笔款</span></h2>']

    pending = [p for p in s["payments"] if p["status"] == "pending" and p["kind"] == "final"]
    for p in pending:
        appr = svc.con.execute(
            "SELECT user_id FROM payment_approvals WHERE payment_id=?", (p["id"],)).fetchall()
        who = {a["user_id"] for a in appr}
        cust_done = c["customer_id"] in who
        mgr_done = c["manager_id"] in who
        needs_me = as_user in (c["customer_id"], c["manager_id"]) and as_user not in who
        sig = (f'<span class="signers"><span class="{"yes" if cust_done else "no"}">'
               f'客户{"已签" if cust_done else "未签"}</span> · '
               f'<span class="{"yes" if mgr_done else "no"}">'
               f'项目经理{"已签" if mgr_done else "未签"}</span></span>')
        btn = (f'<form class="inline" method="post" action="/payments/{esc(p["id"])}/approve">'
               f'<button class="btn mini" {"disabled" if not needs_me else ""}>'
               f'{"等待对方签放" if not needs_me else "我来签放（出账）"}</button></form>')
        codes = ", ".join(sorted(
            r["code"] for r in svc.con.execute(
                "SELECT r.code FROM payment_items pi JOIN rectifications r ON r.id=pi.rect_id "
                "WHERE pi.payment_id=?", (p["id"],)).fetchall()))
        out.append(
            f'<div class="checkline">待签放款 {yn(p["amount_cents"])}（整改项 {esc(codes)}，'
            f'依据：照片已经客户签认通过） {sig} {btn}</div>')

    payable = [r for r in s["rectifications"]
               if r["status"] == "passed" and r["id"] not in s["linked_rect_ids"]
               and r["kind"] == "rectification"]
    if payable:
        boxes = "".join(
            f'<label class="checkline" style="display:block">'
            f'<input type="checkbox" name="rect" value="{esc(r["id"])}" checked> '
            f'{esc(r["code"])} {esc(r["title"])} — 留存 {yn(r["holdback_cents"])}'
            f'（{esc(r["passed_at"][:16].replace("T", " "))} 客户签认）</label>'
            for r in payable)
        out.append(
            '<form method="post" action="/release-final"><div class="subline">已验收、可释放：</div>'
            + boxes +
            '<div class="row-actions"><button class="btn">申请/签放所选尾款</button>'
            '<span class="note">同一批整改项两人重复点击只产生一笔付款指令</span></div></form>')
    if not payable and not pending:
        out.append('<p class="muted">暂无新的可释放尾款；整改项须客户验收签认后才出现在这里。</p>')
    out.append('</section>')
    return "".join(out)


def render_rect_section(svc: SettlementService, s: dict, as_user: str) -> str:
    c = s["contract"]
    out = ['<section class="card"><h2>整改 / 返修清单 <span class="hint">逐条展开查看现场照片、'
           '提交与签认时间</span></h2>']
    for r in s["rectifications"]:
        label, _ = STATUS_LABEL[r["status"]]
        kind = "质保返修" if r["kind"] == "warranty" else "竣工验收整改"
        detail = svc.rectification_detail(r["id"])
        linked = r["id"] in s["linked_rect_ids"]
        hold = ("已随尾款释放" if linked and r["kind"] == "rectification"
                else (yn(r["holdback_cents"]) if r["kind"] == "rectification" else "质保范围内"))
        head = (f'<summary><span class="code">{esc(r["code"])}</span>'
                f'<span class="ttl">{esc(r["title"])}</span>'
                f'<span class="loc">{esc(r["location"])} · {kind}</span>'
                f'<span class="hold">{hold}</span>'
                f'{chip(r["status"], STATUS_LABEL)}<span class="arrow">▸</span></summary>')

        body = ['<div class="detail-body">',
                f'<div class="muted">{esc(r["description"])}</div>']

        # 操作按钮
        if r["status"] == "submitted":
            pending_sub = next(x for x in detail["submissions"] if x["decision"] == "pending")
            body.append(
                '<div class="subline">客户验收意见</div>'
                f'<form method="post" action="/decide"><input type="hidden" name="submission_id" '
                f'value="{pending_sub["id"]}">'
                '<textarea name="note" rows="2" placeholder="签认意见 / 新问题描述"></textarea>'
                '<div class="row-actions">'
                '<button class="btn mini" name="verdict" value="pass">照片确认·验收通过</button>'
                '<button class="btn gray mini" name="verdict" value="reject">提出新问题·打回</button>'
                '<span class="note">仅客户可签认</span></div></form>')
        if r["status"] == "rejected":
            body.append(
                '<div class="subline">返工后重新提交（项目经理）</div>'
                f'<form method="post" action="/resubmit"><input type="hidden" name="rect_id" '
                f'value="{esc(r["id"])}">'
                '<textarea name="note" rows="2" placeholder="返工说明（系统补存一张当日现场照片）">'
                '</textarea><div class="row-actions"><button class="btn mini">重新提交整改</button>'
                '<span class="note">提交后重新等待客户签认</span></div></form>')
        if r["status"] == "open":
            body.append('<p class="note">待项目经理整改并提交照片。</p>')

        # 提交时间线 + 照片
        for sub in detail["submissions"]:
            photos = [p for p in detail["photos"] if p["submission_id"] == sub["id"]]
            cls = "orange" if sub["decision"] == "pending" else ""
            when = sub["submitted_at"][:16].replace("T", " ")
            line = f'<li class="{cls}"><b>第 {sub["seq"]} 次提交</b> · {when} · ' \
                   f'提交人 {esc(_USERS.get(sub["submitted_by"], sub["submitted_by"]))}<br>' \
                   f'{esc(sub["note"])}'
            if sub["decision"] == "passed":
                line += (f'<br>✔ <b>客户 {esc(sub["decided_at"][:16].replace("T", " "))} 签认通过</b>'
                         f'：{esc(sub["decision_note"])}'
                         f' <span class="sig">签名 {esc(sub["signature"])}</span>')
            elif sub["decision"] == "rejected":
                line += (f'<br>↩ <b>{esc(sub["decided_at"][:16].replace("T", " "))} 打回·新问题</b>：'
                         f'{esc(sub["decision_note"])}')
            else:
                line += '<br><span style="color:var(--orange)">等待客户查看照片后签认</span>'
            line += "</li>"
            body.append('<div class="subline">提交与签认</div><ul class="timeline">' + line + "</ul>")
            if photos:
                figs = "".join(
                    f'<figure><img src="/photos/{urllib.parse.quote(p["file_path"])}" '
                    f'loading="lazy" alt="{esc(p["caption"])}">'
                    f'<figcaption>{esc(p["caption"])} · {esc(p["uploaded_at"][:10])}</figcaption>'
                    f'</figure>' for p in photos)
                body.append(f'<div class="photos">{figs}</div>')
        body.append("</div>")
        out.append(f'<details class="rect">{head}{"".join(body)}</details>')
    out.append("</section>")
    return "".join(out)


def render_retention_section(svc: SettlementService, s: dict, as_user: str) -> str:
    c = s["contract"]
    claims = svc.con.execute("SELECT * FROM warranty_claims WHERE contract_id=? ORDER BY opened_at",
                             (CONTRACT_ID,)).fetchall()
    out = ['<section class="card"><h2>质保金与质保索赔 <span class="hint">期满且无未结索赔才结清，'
           '不得被尾款误释放</span></h2>']
    mature = "已到期" if s["warranty_matured"] else "尚未到期"
    out.append(f'<p>质保金 {yn(s["retention_cents"])}（余额 {yn(s["retention_left_cents"])}），'
               f'质保期至 <b>{esc(c["warranty_end"])}</b>（{mature}）。</p>')
    for cl in claims:
        st = "未结" if cl["status"] == "open" else "已结清"
        cls = "no" if cl["status"] == "open" else "yes"
        extra = ""
        if cl["status"] == "open":
            extra = (f'<form class="inline" method="post" action="/claims/{esc(cl["id"])}/resolve">'
                     f'<button class="btn gray mini">登记处理结果</button></form>')
        out.append(
            f'<div class="checkline"><span class="signers"><span class="{cls}">索赔{st}</span></span> '
            f'{esc(cl["opened_at"][:10])} · {esc(cl["title"])} — {esc(cl["detail"])} {extra}</div>')
    open_n = sum(1 for cl in claims if cl["status"] == "open")
    out.append('<form method="post" action="/release-retention"><div class="row-actions">'
               '<button class="btn">申请结清质保金</button>'
               f'<span class="note">系统将强校验：到期日 + {open_n} 笔未结索赔</span></div></form>')
    out.append('</section>')
    return "".join(out)


def render_payment_ledger(svc: SettlementService, s: dict) -> str:
    out = ['<section class="card"><h2>付款指令台账 <span class="hint">幂等键唯一；'
           '回执可重放，不会重复出账</span></h2><table>'
           '<tr><th>类型</th><th>金额</th><th>状态</th><th class="hide-s">网关流水/回执时间</th>'
           '<th>操作</th></tr>']
    for p in s["payments"]:
        kind_cls = " final" if p["kind"] == "final" else ""
        ref = esc(p["gateway_ref"] or "—")
        if p["receipt_at"]:
            ref += f'<div class="note">回执 {esc(p["receipt_at"][:16].replace("T", " "))}</div>'
        actions = []
        if p["status"] == "executed":
            actions.append(
                f'<form class="inline" method="post" action="/payments/{esc(p["id"])}/receipt">'
                '<button class="btn gray mini">查询回执（可重放）</button></form>')
            actions.append(
                f'<form class="inline" method="post" action="/payments/{esc(p["id"])}/receipt-arrive">'
                '<button class="btn mini">模拟银行回执送达</button></form>')
            actions.append(
                f'<form class="inline" method="post" action="/payments/{esc(p["id"])}/dispute">'
                '<button class="btn gray mini">登记回执延迟争议</button></form>')
        elif p["status"] == "receipt_confirmed":
            actions.append(
                f'<form class="inline" method="post" action="/payments/{esc(p["id"])}/receipt">'
                '<button class="btn gray mini">重放回执查询</button></form>')
        action_html = " ".join(actions) or '<span class="note">—</span>'
        out.append(
            f'<tr><td><span class="kindtag{kind_cls}">{KIND_LABEL[p["kind"]]}</span></td>'
            f'<td class="num">{yn(p["amount_cents"])}</td>'
            f'<td>{chip(p["status"], PAY_STATUS)}</td><td class="hide-s">{ref}</td>'
            f'<td>{action_html}</td></tr>')
    out.append("</table></section>")
    return "".join(out)


def render_evidence(svc: SettlementService, s: dict) -> str:
    c = s["contract"]
    out = ['<section class="card"><h2>依据与版本记录 <span class="hint">只追加事件流，'
           '调价、换人、争议、签认全部留痕</span></h2>']

    versions = svc.con.execute("SELECT * FROM contract_versions WHERE contract_id=? ORDER BY version_no",
                               (CONTRACT_ID,)).fetchall()
    out.append('<div class="subline">合同版本（价格调整）</div><table>'
               '<tr><th>版本</th><th class="num">合同价</th><th>调整原因 / 时间</th></tr>')
    for v in versions:
        cur = " ← 当前" if v["version_no"] == c["current_version"] else ""
        who = _USERS.get(v["created_by"], v["created_by"])
        out.append(f'<tr><td>v{v["version_no"]}{cur}</td><td class="num">{yn(v["amount_cents"])}</td>'
                   f'<td>{esc(v["change_reason"])}<div class="note">{esc(v["created_at"][:16])} '
                   f'· {esc(who)}</div></td></tr>')
    out.append("</table>")

    tenures = svc.con.execute(
        "SELECT t.*, u.name AS mname FROM pm_tenures t JOIN users u ON u.id=t.manager_id "
        "WHERE t.contract_id=? ORDER BY t.seq", (CONTRACT_ID,)).fetchall()
    out.append('<div class="subline">项目经理任期</div><table>'
               '<tr><th>任次</th><th>项目经理</th><th>起止</th><th>更换原因</th></tr>')
    for t in tenures:
        span = t["started_at"][:10] + " 起" + ("" if not t["ended_at"] else "，至 " + t["ended_at"][:10])
        out.append(f'<tr><td>第 {t["seq"]} 任</td><td>{esc(t["mname"])}</td><td>{esc(span)}</td>'
                   f'<td>{esc(t["change_reason"] or "—")}</td></tr>')
    out.append("</table>")

    disputes = svc.con.execute("SELECT * FROM disputes WHERE contract_id=? ORDER BY raised_at",
                               (CONTRACT_ID,)).fetchall()
    if disputes:
        out.append('<div class="subline">争议记录</div>')
        for d in disputes:
            if d["status"] == "open":
                dchip = '<span class="chip pend">争议未结</span>'
                st = ""
            else:
                dchip = '<span class="chip done">已解决</span>'
                st = f'<br><span class="muted">处理结果：{esc(d["resolution"] or "")}</span>'
            out.append(f'<div class="checkline">{dchip} '
                       f'<b>{esc(d["title"])}</b> <span class="note">{esc(d["raised_at"][:16])}'
                       f'</span><div class="muted">{esc(d["detail"])}{st}</div></div>')

    events = svc.con.execute(
        "SELECT * FROM events WHERE contract_id=? ORDER BY seq DESC LIMIT 12",
        (CONTRACT_ID,)).fetchall()
    out.append('<div class="subline">最近事件（版本流）</div><table class="hide-s">'
               '<tr><th>时间</th><th>类型</th><th>操作人</th><th>内容</th></tr>')
    for e in events:
        out.append(f'<tr><td class="hide-s">{esc(e["ts"][:16])}</td><td>{esc(e["type"])}</td>'
                   f'<td>{esc(_USERS.get(e["actor"], e["actor"]))}</td>'
                   f'<td class="hide-s"><code>{esc(e["payload"][:150])}</code></td></tr>')
    out.append("</table></section>")
    return "".join(out)


# ----------------------------------------------------------------- HTTP 处理

class Handler(BaseHTTPRequestHandler):
    server_version = "RenovationSettlement/1.0"

    # -- 框架辅助 --
    def _cookies(self) -> SimpleCookie:
        ck = SimpleCookie()
        if self.headers.get("Cookie"):
            ck.load(self.headers["Cookie"])
        return ck

    def _as_user(self, svc: SettlementService) -> str:
        ck = self._cookies()
        uid = ck.get("uid")
        val = uid.value if uid else ""
        if val and val in _USERS:
            return val
        return svc.contract(CONTRACT_ID)["customer_id"]

    def _redirect(self, path: str, msg: str = "", err: str = "",
                  set_cookie: str = "") -> None:
        qs = urllib.parse.urlencode({"msg": msg, "err": err})
        loc = path + (("?" + qs) if qs and (msg or err) else "")
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", loc)
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()

    def _do_post(self, action, form: dict[str, list[str]], *args) -> None:
        with _LOCK:
            svc = STATE.service()
            uid = self._as_user(svc)
            try:
                msg = action(svc, uid, form, *args) or ""
                self._redirect("/settlement", msg=msg)
            except (DomainError, PaymentError) as ex:
                self._redirect("/settlement", err=f"操作被拒绝：{ex}")
            finally:
                svc.con.close()

    # -- 路由 --
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        if path == "/":
            self._redirect("/settlement")
            return
        if path == "/settlement":
            with _LOCK:
                svc = STATE.service()
                if q.get("as"):
                    uid = q["as"][0]
                    if uid in _USERS:
                        self._redirect("/settlement",
                                       msg=q.get("msg", [""])[0], err=q.get("err", [""])[0],
                                       set_cookie=f"uid={uid}; Path=/; Max-Age=86400")
                        svc.con.close()
                        return
                uid = self._as_user(svc)
                body = render_settlement(svc, uid, q.get("msg", [""])[0], q.get("err", [""])[0])
                self._send(200, body)
                svc.con.close()
            return
        if path.startswith("/photos/"):
            self._serve_photo(path[len("/photos/"):])
            return
        self._send(404, page("未找到", "<p>页面不存在。</p>"))

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        form = urllib.parse.parse_qs(raw, keep_blank_values=True)

        if p == "/decide":
            self._do_post(self._act_decide, form)
        elif p == "/resubmit":
            self._do_post(self._act_resubmit, form)
        elif p == "/release-final":
            self._do_post(self._act_release_final, form)
        elif p == "/release-retention":
            self._do_post(self._act_release_retention, form)
        elif p.startswith("/payments/") and p.endswith("/approve"):
            self._do_post(self._act_approve, form, p.split("/")[2])
        elif p.startswith("/payments/") and p.endswith("/receipt-arrive"):
            self._do_post(self._act_receipt_arrive, form, p.split("/")[2])
        elif p.startswith("/payments/") and p.endswith("/receipt"):
            self._do_post(self._act_receipt, form, p.split("/")[2])
        elif p.startswith("/payments/") and p.endswith("/dispute"):
            self._do_post(self._act_payment_dispute, form, p.split("/")[2])
        elif p.startswith("/claims/") and p.endswith("/resolve"):
            self._do_post(self._act_resolve_claim, form, p.split("/")[2])
        else:
            self._redirect("/settlement", err="未知操作")

    @staticmethod
    def _f(form: dict[str, list[str]], key: str, default: str = "") -> str:
        return form.get(key, [default])[0]

    # -- POST 动作 --
    def _act_decide(self, svc, uid, form):
        approved = self._f(form, "verdict") == "pass"
        note = self._f(form, "note") or ("同意通过" if approved else "需整改")
        svc.decide_submission(uid, int(self._f(form, "submission_id")), approved, note)
        return "整改照片已确认并签认，对应尾款已进入可释放清单" if approved else "已打回并记录客户新问题，整改项不得计入可付金额"

    def _act_resubmit(self, svc, uid, form):
        from .seed import _photo_svg
        rid = self._f(form, "rect_id")
        r = svc.con.execute("SELECT * FROM rectifications WHERE id=?", (rid,)).fetchone()
        now = svc.now()
        data = _photo_svg(f"{r['code']} 返工现场", "返工补照", r["location"],
                          now.strftime("%m-%d %H:%M"))
        svc.submit_rectification(uid, rid, self._f(form, "note") or "已按客户新问题返工",
                                 [("返工补照.svg", data, "返工完成现场照片")])
        return "整改已重新提交，等待客户确认照片后签认"

    def _act_release_final(self, svc, uid, form):
        rects = form.get("rect", [])
        pay = svc.release_payment(uid, CONTRACT_ID, "final", rects or None)
        if pay["status"] == "pending":
            return "尾款指令已生成，等待另一方签放（指令幂等，重复发起只产生一笔）"
        return f"双人签放完成，尾款 {yn(pay['amount_cents'])} 已出账（仅一笔指令，未动用质保金）"

    def _act_release_retention(self, svc, uid, form):
        pay = svc.release_payment(uid, CONTRACT_ID, "retention")
        return f"质保期满且无未结索赔，{yn(pay['amount_cents'])} 已结清"

    def _act_approve(self, svc, uid, form, pid):
        pay = svc.approve_payment(uid, pid)
        if pay["status"] == "pending":
            return "签放已记录，等待另一方签放"
        return f"双人签放齐备，{yn(pay['amount_cents'])} 已出账，流水 {pay['gateway_ref']}"

    def _act_receipt(self, svc, uid, form, pid):
        try:
            row = svc.refresh_receipt(pid)
            return f"回执查询成功（{row['gateway_ref']}），重复查询不产生重复指令"
        except ReceiptPending:
            raise DomainError("银行回执尚未到达（回执延迟），可登记争议或稍后重放查询")

    def _act_receipt_arrive(self, svc, uid, form, pid):
        STATE.gateway.deliver_receipt(pid)
        row = svc.refresh_receipt(pid)
        return f"模拟回执已送达并确认（{row['gateway_ref']}）"

    def _act_payment_dispute(self, svc, uid, form, pid):
        row = svc.con.execute("SELECT * FROM payment_instructions WHERE id=?", (pid,)).fetchone()
        svc.raise_dispute(
            uid, CONTRACT_ID, "付款回执延迟",
            f"{KIND_LABEL[row['kind']]} {yn(row['amount_cents'])}（{row['gateway_ref']}）"
            "已出账但银行回执未送达，挂账待补回单。",
            ref_type="payment", ref_id=pid)
        return "已登记回执延迟争议，账务状态不变"

    def _act_resolve_claim(self, svc, uid, form, cid):
        svc.resolve_claim(uid, cid, self._f(form, "note") or "返修完成，客户确认")
        return "质保索赔已标记结清"

    # -- 静态照片（限定在 PHOTO_DIR 内）--
    def _serve_photo(self, rel: str) -> None:
        rel = urllib.parse.unquote(rel)
        base = PHOTO_DIR.resolve()
        target = (base / rel).resolve()
        if not str(target).startswith(str(base) + "/") or not target.is_file():
            self._send(404, b"not found", ctype="text/plain")
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype=ctype)

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args) -> None:
        pass  # 静默；事件流本身即留痕


def main(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True) -> None:
    from http.server import ThreadingHTTPServer

    if not DB_PATH.exists():
        print("首次启动：生成部分验收样例数据 …")
        seed(DATA_DIR)
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/settlement"
    print(f"装修工程结算台已启动：{url}")
    print("（Ctrl+C 停止；删除 data/ 后重启可重新生成样例）")
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
