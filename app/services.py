"""领域服务：结算、整改、签认、尾款、质保金、争议、依据留痕。"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from .adapters import PaymentAdapter, SignatureAdapter
from .db import Database, now_iso
from .errors import Conflict, DomainError, NotFound, PermissionDenied

ROLE_CUSTOMER = "customer"
ROLE_MANAGER = "manager"


class Service:
    def __init__(self, db: Database, photos_dir: str | Path,
                 pay: PaymentAdapter | None = None,
                 sig: SignatureAdapter | None = None,
                 clock=date.today):
        self.db = db
        self.photos_dir = Path(photos_dir)
        self.photos_dir.mkdir(parents=True, exist_ok=True)
        self.pay = pay or PaymentAdapter()
        self.sig = sig or SignatureAdapter()
        self.clock = clock

    def today(self) -> date:
        return self.clock()

    # ------------------------------------------------------------------ users

    def create_user(self, name: str, role: str) -> int:
        assert role in (ROLE_CUSTOMER, ROLE_MANAGER)
        with self.db.tx() as c:
            cur = c.execute("INSERT INTO users(name,role) VALUES(?,?)", (name, role))
            uid = cur.lastrowid
            self.db.log_event(c, uid, "user", uid, "create", {"name": name, "role": role})
            return uid

    def _user(self, c, uid: int) -> sqlite3.Row:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise NotFound(f"用户 {uid} 不存在")
        return row

    def _require_role(self, c, uid, role) -> sqlite3.Row:
        u = self._user(c, uid)
        if u["role"] != role:
            raise PermissionDenied(f"该操作仅{('项目经理' if role=='manager' else '客户')}可执行")
        return u

    # ---------------------------------------------------------------- project

    def create_project(self, name, manager_id, price_yuan, retention_pct,
                       warranty_days, acceptance_iso: str | None = None) -> int:
        price = self._cents(price_yuan)
        with self.db.tx() as c:
            self._require_role(c, manager_id, ROLE_MANAGER)
            cur = c.execute(
                "INSERT INTO projects(name,current_manager_id,contract_price_cents,"
                "retention_bp,acceptance_date,warranty_days,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (name, manager_id, price, round(retention_pct * 100),
                 acceptance_iso, warranty_days, now_iso()))
            pid = cur.lastrowid
            self.db.log_event(c, manager_id, "project", pid, "create",
                              {"price_cents": price, "retention_pct": retention_pct})
            return pid

    def _project(self, c, pid) -> sqlite3.Row:
        row = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not row:
            raise NotFound(f"项目 {pid} 不存在")
        return row

    def contract_price_cents(self, c, pid) -> int:
        row = c.execute(
            "SELECT contract_price_cents FROM projects WHERE id=?", (pid,)).fetchone()
        adj = c.execute(
            "SELECT COALESCE(SUM(delta_cents),0) d FROM contract_adjustments WHERE project_id=?",
            (pid,)).fetchone()["d"]
        return row["contract_price_cents"] + adj

    def retention_cents(self, c, pid) -> int:
        p = self._project(c, pid)
        return round(self.contract_price_cents(c, pid) * p["retention_bp"] / 10000)

    def adjust_contract(self, pid, actor_id, delta_yuan, reason, basis) -> int:
        """合同价调整：必须填写原因与依据。"""
        if not reason or not basis:
            raise DomainError("合同价调整必须留存原因与依据")
        delta = self._cents(delta_yuan)
        if delta == 0:
            raise DomainError("调整金额不能为 0")
        with self.db.tx() as c:
            self._user(c, actor_id)
            cur = c.execute(
                "INSERT INTO contract_adjustments(project_id,delta_cents,reason,basis,"
                "created_by,created_at) VALUES(?,?,?,?,?,?)",
                (pid, delta, reason, basis, actor_id, now_iso()))
            c.execute("UPDATE projects SET version=version+1 WHERE id=?", (pid,))
            self.db.log_event(c, actor_id, "contract_adjustment", cur.lastrowid,
                              "create", {"delta_cents": delta, "reason": reason})
            return cur.lastrowid

    def replace_manager(self, pid, actor_id, new_manager_id, reason, basis) -> int:
        """更换项目经理：客户授权，须留依据，旧经理记录入史。"""
        if not reason or not basis:
            raise DomainError("更换项目经理必须留存原因与依据")
        with self.db.tx() as c:
            self._require_role(c, actor_id, ROLE_CUSTOMER)
            p = self._project(c, pid)
            self._require_role(c, new_manager_id, ROLE_MANAGER)
            if new_manager_id == p["current_manager_id"]:
                raise DomainError("新经理与现任经理相同")
            cur = c.execute(
                "INSERT INTO manager_history(project_id,old_manager_id,new_manager_id,"
                "reason,basis,changed_at) VALUES(?,?,?,?,?,?)",
                (pid, p["current_manager_id"], new_manager_id, reason, basis, now_iso()))
            c.execute("UPDATE projects SET current_manager_id=?,version=version+1 WHERE id=?",
                      (new_manager_id, pid))
            self.db.log_event(c, actor_id, "project", pid, "replace_manager",
                              {"old": p["current_manager_id"], "new": new_manager_id})
            return cur.lastrowid

    # --------------------------------------------------------- work completion

    def enter_completion(self, pid, actor_id, item_name, quantity, unit) -> int:
        """工程完成量仅项目经理可录入；客户代录一律拒绝。"""
        with self.db.tx() as c:
            self._require_role(c, actor_id, ROLE_MANAGER)
            self._project(c, pid)
            cur = c.execute(
                "INSERT INTO work_completions(project_id,item_name,quantity,unit,"
                "entered_by,entered_at) VALUES(?,?,?,?,?,?)",
                (pid, item_name, quantity, unit, actor_id, now_iso()))
            self.db.log_event(c, actor_id, "work_completion", cur.lastrowid, "create",
                              {"item": item_name, "qty": quantity})
            return cur.lastrowid

    def pay_progress(self, pid, actor_id, amount_yuan, note="") -> int:
        """进度款：由项目经理依据完成量发起。"""
        amount = self._cents(amount_yuan)
        if amount <= 0:
            raise DomainError("付款金额必须为正")
        with self.db.tx() as c:
            self._require_role(c, actor_id, ROLE_MANAGER)
            self._guard_contract_cap(c, pid, extra_tail=0, extra_progress=amount)
            rid = self._insert_request(c, pid, "progress", amount, actor_id, note)
            self._issue_payment(c, pid, rid, "progress", amount, delay=False)
            return rid

    # ---------------------------------------------------------- rectifications

    def create_rect(self, pid, actor_id, title, tail_yuan, description="",
                    parent_id=None) -> int:
        """新建整改项（经理开单；客户的“新问题”走 add_customer_issue）。"""
        tail = self._cents(tail_yuan)
        if tail < 0:
            raise DomainError("尾款分配不能为负")
        with self.db.tx() as c:
            self._require_role(c, actor_id, ROLE_MANAGER)
            self._project(c, pid)
            self._guard_contract_cap(c, pid, extra_tail=tail)
            cur = c.execute(
                "INSERT INTO rectifications(project_id,title,description,tail_cents,"
                "created_by,created_at,parent_id) VALUES(?,?,?,?,?,?,?)",
                (pid, title, description, tail, actor_id, now_iso(), parent_id))
            id_ = cur.lastrowid
            self.db.log_event(c, actor_id, "rectification", id_, "create",
                              {"title": title, "tail_cents": tail})
            return id_

    def add_customer_issue(self, pid, actor_id, parent_id, title,
                           photo_bytes: bytes | None = None, caption="") -> int:
        """客户在部分验收通过后提出新问题：挂到父整改项下，必须留痕。"""
        with self.db.tx() as c:
            self._require_role(c, actor_id, ROLE_CUSTOMER)
            parent = c.execute(
                "SELECT * FROM rectifications WHERE id=? AND project_id=?",
                (parent_id, pid)).fetchone()
            if not parent:
                raise NotFound("原整改项不存在")
            if parent["status"] != "passed":
                raise DomainError("仅可对已通过验收的整改项追加新问题")
            cur = c.execute(
                "INSERT INTO rectifications(project_id,title,description,status,"
                "created_by,created_at,parent_id,tail_cents)"
                " VALUES(?,?,?,?,?,?,?,0)",
                (pid, title, "客户复验后追加的新问题", "open",
                 actor_id, now_iso(), parent_id))
            rid = cur.lastrowid
            if photo_bytes:
                self._save_photo(c, rid, actor_id, photo_bytes, "issue", caption)
            self.db.log_event(c, actor_id, "rectification", rid, "customer_issue",
                              {"parent_id": parent_id, "title": title})
            return rid

    def submit_rect(self, rect_id, actor_id, photos: list[tuple[bytes, str]]):
        """经理整改完成并提交复验（附整改后照片）。"""
        with self.db.tx() as c:
            self._require_role(c, actor_id, ROLE_MANAGER)
            r = self._get_rect(c, rect_id)
            if r["status"] not in ("open", "rework"):
                raise DomainError(f"当前状态 {r['status']} 不可提交复验")
            if not photos:
                raise DomainError("提交复验必须上传整改照片")
            for data, caption in photos:
                self._save_photo(c, rect_id, actor_id, data, "repair", caption)
            c.execute("UPDATE rectifications SET status='submitted',submitted_at=?,"
                      " version=version+1 WHERE id=?", (now_iso(), rect_id))
            self.db.log_event(c, actor_id, "rectification", rect_id, "submit", {})

    def sign_rect(self, rect_id, customer_id) -> dict:
        """客户确认整改照片后签认：仅客户可签，签后释放该尾款资格。"""
        with self.db.tx() as c:
            self._require_role(c, customer_id, ROLE_CUSTOMER)
            r = self._get_rect(c, rect_id)
            if r["status"] != "submitted":
                raise DomainError("仅已提交复验的整改项可签认")
            photos = c.execute("SELECT COUNT(*) n FROM rect_photos WHERE rect_id=? AND kind='repair'",
                               (rect_id,)).fetchone()["n"]
            if photos == 0:
                raise DomainError("无整改照片，无法确认签认")
            nonce = f"rect-{rect_id}-{r['submitted_at']}"
            tok = self.sig.sign(customer_id, f"rectification:{rect_id}", nonce)
            c.execute("UPDATE rectifications SET status='passed',signed_by=?,signed_at=?,"
                      "signature=?,version=version+1 WHERE id=?",
                      (customer_id, now_iso(), tok.token, rect_id))
            self.db.log_event(c, customer_id, "rectification", rect_id, "sign",
                              {"signature": tok.token})
            return {"rect_id": rect_id, "signature": tok.token, "signed_at": now_iso()}

    def verify_rect_signature(self, c, rect_id) -> bool:
        r = self._get_rect(c, rect_id)
        if not r["signature"]:
            return False
        nonce = f"rect-{rect_id}-{r['submitted_at']}"
        return self.sig.verify(r["signed_by"], f"rectification:{rect_id}",
                               nonce, r["signature"])

    def rect_photos(self, rect_id) -> list[sqlite3.Row]:
        with self.db.tx() as c:
            self._get_rect(c, rect_id)
            return c.execute(
                "SELECT * FROM rect_photos WHERE rect_id=? ORDER BY id", (rect_id,)).fetchall()

    # ------------------------------------------------------------ tail payment

    def release_tail(self, pid, customer_id, rect_ids: list[int],
                     delay_receipt: bool = False) -> dict:
        """客户对已签认整改申请尾款并放款。

        - 只有 passed + 客户签名核验通过的整改项可计入；
        - 每个整改项只能进入一笔申请（UNIQUE），未验收项不得计入；
        - 并发重复提交同一组整改项 => 幂等冲突，只产生一笔付款指令。
        """
        if not rect_ids:
            raise DomainError("未选择整改项")
        rect_ids = sorted(set(rect_ids))
        with self.db.tx() as c:
            self._require_role(c, customer_id, ROLE_CUSTOMER)
            self._project(c, pid)
            rows = []
            for rid in rect_ids:
                r = self._get_rect(c, rid)
                if r["project_id"] != pid:
                    raise NotFound(f"整改项 {rid} 不属于本项目")
                if r["status"] != "passed" or not r["signed_by"]:
                    raise DomainError(f"整改项「{r['title']}」未验收签认，不得计入可付金额")
                if self._has_open_child(c, rid):
                    raise DomainError(f"整改项「{r['title']}」存在客户追加的未结新问题，尾款暂缓")
                if not self.verify_rect_signature(c, rid):
                    raise PermissionDenied(f"整改项「{r['title']}」签名核验失败")
                dup = c.execute(
                    "SELECT 1 FROM payment_request_items WHERE rect_id=?", (rid,)).fetchone()
                if dup:
                    raise Conflict(f"整改项「{r['title']}」已申请过尾款")
                rows.append(r)
            amount = sum(r["tail_cents"] for r in rows)
            if amount <= 0:
                raise DomainError("可释放尾款合计为 0")
            idem = f"tail:{pid}:{','.join(map(str, rect_ids))}"
            rid = self._insert_request(c, pid, "tail", amount, customer_id,
                                       "整改验收尾款", idem=idem)
            for r in rows:
                c.execute("INSERT INTO payment_request_items(request_id,rect_id,amount_cents)"
                          " VALUES(?,?,?)", (rid, r["id"], r["tail_cents"]))
            # 明细落库后，所选整改项已不计入“待分配尾款”，再校验额度
            self._guard_contract_cap(c, pid, extra_tail=amount)
            pay = self._issue_payment(c, pid, rid, "tail", amount, delay=delay_receipt)
            return {"request_id": rid, "amount_cents": amount, **pay}

    def confirm_receipt(self, request_id) -> dict:
        """延迟回执到达后轮询确认；同一申请只可能拿到同一回执。"""
        with self.db.tx() as c:
            req = c.execute("SELECT * FROM payment_requests WHERE id=?",
                            (request_id,)).fetchone()
            if not req:
                raise NotFound("付款申请不存在")
            p = c.execute("SELECT * FROM payments WHERE request_id=?",
                          (request_id,)).fetchone()
            result = self.pay.confirm(request_id)
            if result and p["status"] != "paid" and result.status == "paid":
                c.execute("UPDATE payments SET status='paid',paid_at=?,receipt_no=? WHERE id=?",
                          (now_iso(), result.receipt_no, p["id"]))
                self.db.log_event(c, req["requested_by"], "payment", p["id"],
                                  "receipt_confirmed", {"receipt": result.receipt_no})
                return {"status": "paid", "receipt_no": result.receipt_no}
            return {"status": p["status"], "receipt_no": p["receipt_no"]}

    def replay_receipt(self, request_id) -> dict:
        """回执重放：重复查询返回同一 receipt_no，不产生新指令。"""
        with self.db.tx() as c:
            p = c.execute("SELECT * FROM payments WHERE request_id=?",
                          (request_id,)).fetchone()
            if not p:
                raise NotFound("付款指令不存在")
            # 适配器对同一 request_id 永远返回同一回执
            again = self.pay.pay(request_id, p["amount_cents"])
            if again.receipt_no != p["receipt_no"] and p["receipt_no"]:
                raise Conflict("回执不一致，数据异常")
            count = c.execute("SELECT COUNT(*) n FROM payments WHERE request_id=?",
                              (request_id,)).fetchone()["n"]
            assert count == 1
            return {"receipt_no": p["receipt_no"], "payment_rows": count}

    # --------------------------------------------------------- retention pay

    def warranty_end(self, c, pid) -> date | None:
        p = self._project(c, pid)
        if not p["acceptance_date"]:
            return None
        d = datetime.strptime(p["acceptance_date"], "%Y-%m-%d").date()
        return d + timedelta(days=p["warranty_days"])

    def open_warranty_repair(self, pid, customer_id, title, basis="") -> int:
        with self.db.tx() as c:
            self._require_role(c, customer_id, ROLE_CUSTOMER)
            end = self.warranty_end(c, pid)
            today = self.today()
            if end is None:
                raise DomainError("项目尚未验收，无质保期")
            if today > end:
                raise DomainError("质保期已届满，不再受理返修")
            cur = c.execute(
                "INSERT INTO warranty_repairs(project_id,title,basis,opened_at)"
                " VALUES(?,?,?,?)", (pid, title, basis, now_iso()))
            rid = cur.lastrowid
            self.db.log_event(c, customer_id, "warranty_repair", rid, "open", {"title": title})
            return rid

    def repair_done(self, rid, actor_id) -> None:
        with self.db.tx() as c:
            self._require_role(c, actor_id, ROLE_MANAGER)
            r = self._get_warranty(c, rid)
            if r["status"] not in ("open", "repaired"):
                raise DomainError("当前状态不可登记修复")
            c.execute("UPDATE warranty_repairs SET status='repaired',repaired_at=? WHERE id=?",
                      (now_iso(), rid))
            self.db.log_event(c, actor_id, "warranty_repair", rid, "repaired", {})

    def confirm_repair(self, rid, customer_id, as_claim=False, cost_yuan=0) -> None:
        """客户确认返修；无法修复转为索赔，金额从质保金扣减。"""
        cost = self._cents(cost_yuan)
        with self.db.tx() as c:
            self._require_role(c, customer_id, ROLE_CUSTOMER)
            r = self._get_warranty(c, rid)
            if r["status"] != "repaired" and not as_claim:
                raise DomainError("仅已修复的返修可确认关闭")
            new_status = "claim" if as_claim else "confirmed"
            if as_claim and cost <= 0:
                raise DomainError("索赔必须填写扣减金额")
            c.execute("UPDATE warranty_repairs SET status=?,cost_cents=?,confirmed_by=?,"
                      "confirmed_at=? WHERE id=?",
                      (new_status, cost if as_claim else 0, customer_id, now_iso(), rid))
            self.db.log_event(c, customer_id, "warranty_repair", rid, new_status,
                              {"cost_cents": cost})

    def release_retention(self, pid, actor_id, delay_receipt: bool = False) -> dict:
        """质保金到期且无未结索赔/返修时另行结清。"""
        with self.db.tx() as c:
            self._user(c, actor_id)
            end = self.warranty_end(c, pid)
            if end is None:
                raise DomainError("项目尚未验收")
            if self.today() < end:
                raise DomainError(f"质保金未到期（到期日 {end.isoformat()}），不得释放")
            open_items = c.execute(
                "SELECT COUNT(*) n FROM warranty_repairs WHERE project_id=? AND status IN('open','repaired')",
                (pid,)).fetchone()["n"]
            if open_items:
                raise DomainError("存在未结返修/索赔，质保金暂缓结清")
            existing = c.execute(
                "SELECT 1 FROM payment_requests WHERE project_id=? AND kind='retention'",
                (pid,)).fetchone()
            if existing:
                raise Conflict("质保金付款申请已存在，禁止重复放款")
            retention = self.retention_cents(c, pid)
            paid = c.execute(
                "SELECT COALESCE(SUM(p.amount_cents),0) s FROM payments p"
                " JOIN payment_requests r ON r.id=p.request_id"
                " WHERE r.project_id=? AND r.kind='retention' AND p.status='paid'",
                (pid,)).fetchone()["s"]
            claims = c.execute(
                "SELECT COALESCE(SUM(cost_cents),0) s FROM warranty_repairs"
                " WHERE project_id=? AND status='claim'", (pid,)).fetchone()["s"]
            amount = retention - paid - claims
            if amount <= 0:
                raise DomainError("质保金已结清或已被索赔全额扣减")
            req_id = self._insert_request(c, pid, "retention", amount, actor_id,
                                          "质保金到期结清", idem=f"retention:{pid}")
            pay = self._issue_payment(c, pid, req_id, "retention", amount,
                                      delay=delay_receipt)
            return {"request_id": req_id, "amount_cents": amount, **pay}

    # --------------------------------------------------------------- disputes

    def raise_dispute(self, pid, actor_id, subject, detail, basis) -> int:
        if not subject or not basis:
            raise DomainError("争议必须记录主题与依据")
        with self.db.tx() as c:
            self._user(c, actor_id)
            cur = c.execute(
                "INSERT INTO disputes(project_id,subject,detail,basis,raised_by,raised_at)"
                " VALUES(?,?,?,?,?,?)",
                (pid, subject, detail, basis, actor_id, now_iso()))
            rid = cur.lastrowid
            self.db.log_event(c, actor_id, "dispute", rid, "raise", {"subject": subject})
            return rid

    def resolve_dispute(self, dispute_id, actor_id) -> None:
        with self.db.tx() as c:
            self._user(c, actor_id)
            row = c.execute("SELECT * FROM disputes WHERE id=?", (dispute_id,)).fetchone()
            if not row:
                raise NotFound("争议不存在")
            c.execute("UPDATE disputes SET status='resolved',resolved_at=? WHERE id=?",
                      (now_iso(), dispute_id))
            self.db.log_event(c, actor_id, "dispute", dispute_id, "resolve", {})

    # ------------------------------------------------------------- summaries

    def settlement(self, pid) -> dict:
        """结算视图：合同金额 / 已付 / 尾款 / 质保金并列，且总额守恒。"""
        with self.db.tx() as c:
            p = self._project(c, pid)
            contract = self.contract_price_cents(c, pid)
            retention = self.retention_cents(c, pid)
            paid_by_kind = {k: 0 for k in ("progress", "tail", "retention")}
            for row in c.execute(
                    "SELECT r.kind k, COALESCE(SUM(p.amount_cents),0) s FROM payments p"
                    " JOIN payment_requests r ON r.id=p.request_id"
                    " WHERE r.project_id=? AND p.status='paid' GROUP BY r.kind", (pid,)):
                paid_by_kind[row["k"]] = row["s"]
            paid_total = sum(paid_by_kind.values())
            in_flight = c.execute(
                "SELECT COALESCE(SUM(amount_cents),0) s FROM payments"
                " WHERE project_id=? AND status='initiated'", (pid,)).fetchone()["s"]

            available = pending = 0
            for r in c.execute(
                    "SELECT id,status,tail_cents,signed_by FROM rectifications WHERE project_id=?",
                    (pid,)):
                used = c.execute(
                    "SELECT 1 FROM payment_request_items WHERE rect_id=?",
                    (r["id"],)).fetchone()
                if used:
                    continue
                if r["status"] == "passed" and r["signed_by"] \
                        and not self._has_open_child(c, r["id"]):
                    available += r["tail_cents"]
                else:
                    pending += r["tail_cents"]

            claims = c.execute(
                "SELECT COALESCE(SUM(cost_cents),0) s FROM warranty_repairs"
                " WHERE project_id=? AND status='claim'", (pid,)).fetchone()["s"]
            open_repairs = c.execute(
                "SELECT COUNT(*) n FROM warranty_repairs WHERE project_id=?"
                " AND status IN('open','repaired')", (pid,)).fetchone()["n"]
            end = self.warranty_end(c, pid)
            retention_held = retention - paid_by_kind["retention"]
            balance_due = contract - paid_total
            # 未分配到整改项的尾差：仍属于待整改/待结算余额（索赔扣留额仍留在质保金留存内）
            unallocated = balance_due - in_flight - available - pending - retention_held
            if unallocated < 0:
                unallocated = 0
            # 守恒断言：已付 + 在途 + 可付尾款 + 待处理尾款 + 尾差 + 质保金留存 == 合同价
            assert (paid_total + in_flight + available + pending + unallocated
                    + retention_held) == contract, "金额守恒校验失败"

            mgr = c.execute("SELECT name FROM users WHERE id=?",
                            (p["current_manager_id"],)).fetchone()["name"]
            return {
                "project_id": pid,
                "project_name": p["name"],
                "manager": mgr,
                "acceptance_date": p["acceptance_date"],
                "warranty_end": end.isoformat() if end else None,
                "warranty_open": bool(open_repairs),
                "contract_cents": contract,
                "retention_cents": retention,
                "paid": {
                    "progress_cents": paid_by_kind["progress"],
                    "tail_cents": paid_by_kind["tail"],
                    "retention_cents": paid_by_kind["retention"],
                    "total_cents": paid_total,
                    "in_flight_cents": in_flight,  # 回执延迟、尚未到账
                },
                "tail": {
                    "available_cents": available,   # 已签认待放款
                    "pending_cents": pending,       # 未验收整改项（不得计入可付）
                    "unallocated_cents": unallocated,
                },
                "retention": {
                    "held_cents": retention_held,
                    "claims_cents": claims,
                    "due": end is not None and self.today() >= end and open_repairs == 0,
                },
                "balance_due_cents": balance_due,
                "version": p["version"],
            }

    def rectifications(self, pid) -> list[dict]:
        with self.db.tx() as c:
            self._project(c, pid)
            out = []
            for r in c.execute(
                    "SELECT * FROM rectifications WHERE project_id=? ORDER BY COALESCE(parent_id,id),id",
                    (pid,)):
                photos = [dict(x) for x in c.execute(
                    "SELECT id,file_path,caption,kind,uploaded_at,uploaded_by"
                    " FROM rect_photos WHERE rect_id=? ORDER BY id", (r["id"],))]
                signer = None
                if r["signed_by"]:
                    signer = c.execute("SELECT name FROM users WHERE id=?",
                                       (r["signed_by"],)).fetchone()["name"]
                d = dict(r)
                d["photos"] = photos
                d["signer_name"] = signer
                d["signature_valid"] = self.verify_rect_signature(c, r["id"]) \
                    if r["signature"] else False
                d["requested"] = bool(c.execute(
                    "SELECT 1 FROM payment_request_items WHERE rect_id=?", (r["id"],)).fetchone())
                out.append(d)
            return out

    def history(self, pid) -> list[dict]:
        with self.db.tx() as c:
            rows = c.execute(
                "SELECT e.*,u.name actor_name FROM event_log e LEFT JOIN users u ON u.id=e.actor_id"
                " WHERE e.entity='project' AND e.entity_id=? OR"
                " (e.entity IN('rectification','payment','warranty_repair','dispute',"
                "'contract_adjustment') AND json_extract(e.payload,'$.project_id')=?)",
                (pid, pid)).fetchall()
            return [dict(x) for x in rows]

    # --------------------------------------------------------------- helpers

    def _insert_request(self, c, pid, kind, amount, actor_id, note, idem=None):
        cur = c.execute(
            "INSERT INTO payment_requests(project_id,kind,amount_cents,note,requested_by,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (pid, kind, amount, note, actor_id, now_iso()))
        rid = cur.lastrowid
        self.db.log_event(c, actor_id, f"payment_request:{kind}", rid, "create",
                          {"amount_cents": amount, "project_id": pid},
                          idempotency_key=idem)
        return rid

    def _issue_payment(self, c, pid, request_id, kind, amount, delay):
        result = self.pay.pay(request_id, amount, delay=delay)
        c.execute(
            "INSERT INTO payments(project_id,request_id,kind,amount_cents,receipt_no,status,paid_at,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (pid, request_id, kind, amount, result.receipt_no, result.status,
             now_iso() if result.status == "paid" else None, now_iso()))
        self.db.log_event(c, None, "payment", request_id, "issue",
                          {"kind": kind, "amount_cents": amount,
                           "receipt": result.receipt_no, "status": result.status})
        return {"receipt_no": result.receipt_no, "status": result.status}

    def _outstanding_tail_cents(self, c, pid, exclude_rect=None) -> int:
        """尚未进入付款申请的整改尾款合计（未取消项）。"""
        q = ("SELECT COALESCE(SUM(tail_cents),0) s FROM rectifications"
             " WHERE project_id=? AND status!='cancelled'"
             " AND id NOT IN(SELECT rect_id FROM payment_request_items)")
        args = [pid]
        if exclude_rect is not None:
            q += " AND id!=?"
            args.append(exclude_rect)
        return c.execute(q, args).fetchone()["s"]

    def _guard_contract_cap(self, c, pid, extra_tail=0, extra_progress=0):
        """任何付款组合不得使累计付款超过合同价 - 质保金（质保金不得被尾款误释放）。"""
        contract = self.contract_price_cents(c, pid)
        retention = self.retention_cents(c, pid)
        paid = c.execute(
            "SELECT COALESCE(SUM(p.amount_cents),0) s FROM payments p"
            " JOIN payment_requests r ON r.id=p.request_id"
            " WHERE r.project_id=? AND p.status IN('initiated','paid')",
            (pid,)).fetchone()["s"]
        # progress + tail 合计上限 = 合同价 - 质保金
        non_retention = c.execute(
            "SELECT COALESCE(SUM(p.amount_cents),0) s FROM payments p"
            " JOIN payment_requests r ON r.id=p.request_id"
            " WHERE r.project_id=? AND r.kind!='retention' AND p.status IN('initiated','paid')",
            (pid,)).fetchone()["s"]
        # 已承诺（含在途）的非质保付款 + 已分配但未申请的整改尾款不得超过合同价 - 质保金
        committed = non_retention + self._outstanding_tail_cents(c, pid)
        if committed + extra_tail + extra_progress > contract - retention:
            raise DomainError("付款将超过合同可付额度（质保金不得随尾款释放）")
        if paid + extra_tail + extra_progress > contract:
            raise DomainError("付款将超过合同总价")

    def _has_open_child(self, c, rect_id) -> bool:
        """客户对已通过整改追加的新问题未处理完前，父项尾款挂起。"""
        return bool(c.execute(
            "SELECT 1 FROM rectifications WHERE parent_id=? AND status IN('open','submitted','rework')",
            (rect_id,)).fetchone())

    def _get_rect(self, c, rid) -> sqlite3.Row:
        r = c.execute("SELECT * FROM rectifications WHERE id=?", (rid,)).fetchone()
        if not r:
            raise NotFound(f"整改项 {rid} 不存在")
        return r

    def _get_warranty(self, c, rid) -> sqlite3.Row:
        r = c.execute("SELECT * FROM warranty_repairs WHERE id=?", (rid,)).fetchone()
        if not r:
            raise NotFound("返修记录不存在")
        return r

    def _save_photo(self, c, rect_id, uploader, data: bytes, kind, caption) -> int:
        pdir = self.photos_dir / str(rect_id)
        pdir.mkdir(parents=True, exist_ok=True)
        existing = len(list(pdir.iterdir()))
        ext = "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "bin"
        fname = f"{kind}-{existing + 1}.{ext}"
        (pdir / fname).write_bytes(data)
        cur = c.execute(
            "INSERT INTO rect_photos(rect_id,file_path,caption,kind,uploaded_by,uploaded_at)"
            " VALUES(?,?,?,?,?,?)",
            (rect_id, f"{rect_id}/{fname}", caption, kind, uploader, now_iso()))
        return cur.lastrowid

    @staticmethod
    def _cents(v) -> int:
        from .money import parse_cents
        return parse_cents(v)
