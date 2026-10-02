"""领域服务：结算、整改验收、尾款、质保金、争议。

关键不变量：
  合同价 == 已出账 + 待双人签放 + 可申请尾款(已验收未申请) + 未验收留存 + 质保金余额
  - 未验收整改项的 holdback 永远不计入可付金额；
  - 尾款只释放整改留存，质保金只能由 retention 指令在到期且无未结索赔时结清；
  - 付款指令按幂等键唯一，并发放款合并为一笔，网关侧也只扣一次。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterable

from .adapters import MockPaymentGateway, MockSignatureService, ReceiptPending
from .db import record_event


class DomainError(Exception):
    """业务规则冲突（金额守恒、到期限制等）。"""


class PermissionDenied(DomainError):
    """签署/录入角色不符。"""


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class SettlementService:
    def __init__(
        self,
        con: sqlite3.Connection,
        gateway: MockPaymentGateway | None = None,
        signatures: MockSignatureService | None = None,
        photo_dir: str | Path = "data/photos",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.con = con
        self.gateway = gateway or MockPaymentGateway()
        self.signatures = signatures or MockSignatureService()
        self.photo_dir = Path(photo_dir)
        self.clock = clock or datetime.now

    # ---------------------------------------------------------------- 基础

    def now(self) -> datetime:
        return self.clock()

    def create_contract(self, contract_id: str, title: str, customer_id: str,
                        manager_id: str, amount_cents: int, retention_cents: int,
                        warranty_end: str) -> None:
        """建档：写入合同 v1 与首任项目经理任期。"""
        if retention_cents > amount_cents:
            raise DomainError("质保金不得超过合同总价")
        now_iso = self.now().isoformat(timespec="seconds")
        self._tx()
        try:
            self.con.execute(
                "INSERT INTO contracts(id,title,customer_id,manager_id,current_version,"
                "retention_cents,warranty_end,created_at) VALUES (?,?,?,?,1,?,?,?)",
                (contract_id, title, customer_id, manager_id,
                 retention_cents, warranty_end, now_iso))
            self.con.execute(
                "INSERT INTO contract_versions(contract_id,version_no,amount_cents,"
                "change_reason,created_by,created_at) VALUES (?,1,?,'合同签订价',?,?)",
                (contract_id, amount_cents, customer_id, now_iso))
            self.con.execute(
                "INSERT INTO pm_tenures(contract_id,manager_id,seq,started_at)"
                " VALUES (?,?,1,?)", (contract_id, manager_id, now_iso))
            self._event("contract_created", customer_id,
                        {"contract": contract_id, "cents": amount_cents,
                         "retention": retention_cents, "warranty_end": warranty_end},
                        contract_id)
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise

    def user(self, user_id: str) -> sqlite3.Row:
        row = self.con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise DomainError(f"用户不存在: {user_id}")
        return row

    def _require_role(self, user_id: str, role: str) -> sqlite3.Row:
        u = self.user(user_id)
        if u["role"] != role:
            raise PermissionDenied(f"仅{ '客户' if role == 'customer' else '项目经理'}可执行该操作")
        return u

    def contract(self, contract_id: str) -> sqlite3.Row:
        row = self.con.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
        if not row:
            raise DomainError("合同不存在")
        return row

    def _event(self, etype: str, actor: str, payload: dict, contract_id: str | None = None,
               ts: datetime | None = None) -> None:
        record_event(self.con, etype, actor, payload, contract_id, ts or self.now())

    def _tx(self):
        self.con.execute("BEGIN IMMEDIATE")

    # ---------------------------------------------------------------- 合同版本 / 换人

    def adjust_price(self, actor_id: str, contract_id: str, new_amount_cents: int,
                     reason: str) -> int:
        """合同价调整：新增版本行，旧版本永久保留。客户或经理均可发起，但必须写明原因。"""
        self.user(actor_id)
        if new_amount_cents < 0:
            raise DomainError("合同金额不能为负")
        if not reason.strip():
            raise DomainError("合同价调整必须填写原因作为依据")
        self._tx()
        try:
            c = self.contract(contract_id)
            old = self.current_amount(contract_id)
            new_no = c["current_version"] + 1
            self.con.execute(
                "INSERT INTO contract_versions(contract_id, version_no, amount_cents,"
                " change_reason, created_by, created_at) VALUES (?,?,?,?,?,?)",
                (contract_id, new_no, new_amount_cents, reason, actor_id,
                 self.now().isoformat(timespec="seconds")),
            )
            self.con.execute("UPDATE contracts SET current_version=? WHERE id=?",
                             (new_no, contract_id))
            self._event("price_adjusted", actor_id,
                        {"old_cents": old, "new_cents": new_amount_cents,
                         "version": new_no, "reason": reason}, contract_id)
            self.con.commit()
            return new_no
        except Exception:
            self.con.rollback()
            raise

    def replace_manager(self, actor_id: str, contract_id: str, new_manager_id: str,
                        reason: str) -> None:
        """更换项目经理：结束旧任期、开启新任期，历史任期保留。"""
        self.user(actor_id)
        m = self._require_role(new_manager_id, "manager")
        if not reason.strip():
            raise DomainError("更换项目经理必须填写原因作为依据")
        self._tx()
        try:
            c = self.contract(contract_id)
            if c["manager_id"] == new_manager_id:
                raise DomainError("新经理与现任经理相同")
            now_iso = self.now().isoformat(timespec="seconds")
            last = self.con.execute(
                "SELECT seq FROM pm_tenures WHERE contract_id=? ORDER BY seq DESC LIMIT 1",
                (contract_id,)).fetchone()
            seq = (last["seq"] + 1) if last else 1
            self.con.execute(
                "UPDATE pm_tenures SET ended_at=?, change_reason=? "
                "WHERE contract_id=? AND ended_at IS NULL",
                (now_iso, reason, contract_id))
            self.con.execute(
                "INSERT INTO pm_tenures(contract_id, manager_id, seq, started_at, change_reason)"
                " VALUES (?,?,?,?,?)",
                (contract_id, new_manager_id, seq, now_iso, reason))
            self.con.execute("UPDATE contracts SET manager_id=? WHERE id=?",
                             (new_manager_id, contract_id))
            self._event("manager_replaced", actor_id,
                        {"old_manager": c["manager_id"], "new_manager": new_manager_id,
                         "manager_name": m["name"], "reason": reason, "tenure_seq": seq},
                        contract_id)
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise

    def current_amount(self, contract_id: str) -> int:
        c = self.contract(contract_id)
        row = self.con.execute(
            "SELECT amount_cents FROM contract_versions WHERE contract_id=? AND version_no=?",
            (contract_id, c["current_version"])).fetchone()
        return row["amount_cents"]

    # ---------------------------------------------------------------- 完成量

    def record_progress(self, actor_id: str, contract_id: str, item_name: str,
                        quantity: float, unit: str, amount_cents: int) -> int:
        """工程完成量仅项目经理可录入；客户代录会被拒绝。"""
        u = self._require_role(actor_id, "manager")
        c = self.contract(contract_id)
        if u["id"] != c["manager_id"]:
            raise PermissionDenied("只有本合同现任项目经理可录入完成量")
        if amount_cents <= 0:
            raise DomainError("完成量金额必须为正")
        self._tx()
        try:
            cur = self.con.execute(
                "INSERT INTO progress_entries(contract_id,item_name,quantity,unit,"
                "amount_cents,recorded_by,recorded_at) VALUES (?,?,?,?,?,?,?)",
                (contract_id, item_name, quantity, unit, amount_cents, actor_id,
                 self.now().isoformat(timespec="seconds")))
            self._event("progress_recorded", actor_id,
                        {"item": item_name, "qty": quantity, "unit": unit,
                         "cents": amount_cents}, contract_id)
            self.con.commit()
            return cur.lastrowid
        except Exception:
            self.con.rollback()
            raise

    # ---------------------------------------------------------------- 整改与验收

    def add_rectification(self, actor_id: str, contract_id: str, code: str, title: str,
                          location: str, description: str, holdback_cents: int,
                          kind: str = "rectification") -> str:
        self.user(actor_id)
        rid = _uid("R")
        self.con.execute(
            "INSERT INTO rectifications(id,contract_id,kind,code,title,location,description,"
            "holdback_cents,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rid, contract_id, kind, code, title, location, description,
             holdback_cents, "open", self.now().isoformat(timespec="seconds")))
        self.con.commit()
        return rid

    def submit_rectification(self, actor_id: str, rect_id: str, note: str,
                             photos: Iterable[tuple[str, bytes, str]] = ()) -> int:
        """经理提交整改结果（可附现场照片）；被打回后允许再次提交。"""
        u = self._require_role(actor_id, "manager")
        r = self._rect(rect_id)
        c = self.contract(r["contract_id"])
        if u["id"] != c["manager_id"]:
            raise PermissionDenied("只有现任项目经理可提交整改")
        if r["status"] not in ("open", "rejected"):
            raise DomainError(f"整改项当前状态 {r['status']}，不可提交")
        photos = list(photos)
        self._tx()
        try:
            last = self.con.execute(
                "SELECT MAX(seq) AS m FROM rect_submissions WHERE rect_id=?",
                (rect_id,)).fetchone()
            seq = (last["m"] or 0) + 1
            now_iso = self.now().isoformat(timespec="seconds")
            cur = self.con.execute(
                "INSERT INTO rect_submissions(rect_id,seq,note,submitted_by,submitted_at,"
                "decision) VALUES (?,?,?,?,?,'pending')",
                (rect_id, seq, note, actor_id, now_iso))
            sub_id = cur.lastrowid
            for filename, data, caption in photos:
                rel = self._save_photo(r["contract_id"], rect_id, seq, filename, data)
                self.con.execute(
                    "INSERT INTO rect_photos(rect_id,submission_id,file_path,caption,uploaded_at)"
                    " VALUES (?,?,?,?,?)", (rect_id, sub_id, rel, caption, now_iso))
            self.con.execute("UPDATE rectifications SET status='submitted' WHERE id=?", (rect_id,))
            self._event("rect_submitted", actor_id,
                        {"rect": rect_id, "code": r["code"], "seq": seq,
                         "photos": len(photos)},
                        r["contract_id"])
            self.con.commit()
            return sub_id
        except Exception:
            self.con.rollback()
            raise

    def decide_submission(self, actor_id: str, submission_id: int, approved: bool,
                          note: str) -> None:
        """客户对整改提交验收签认。经理无权代签；不通过即客户提出新问题，整改回到 rejected。"""
        u = self._require_role(actor_id, "customer")
        sub = self.con.execute("SELECT * FROM rect_submissions WHERE id=?",
                               (submission_id,)).fetchone()
        if not sub:
            raise DomainError("提交不存在")
        r = self._rect(sub["rect_id"])
        c = self.contract(r["contract_id"])
        if u["id"] != c["customer_id"]:
            raise PermissionDenied("只有本合同客户可验收签认")
        if sub["decision"] != "pending":
            raise DomainError("该次提交已有签认结论，不可重复签署")
        self._tx()
        try:
            now_dt = self.now()
            now_iso = now_dt.isoformat(timespec="seconds")
            decision = "passed" if approved else "rejected"
            status = "passed" if approved else "rejected"
            sig_token = None
            if approved:
                sig = self.signatures.sign(
                    u["id"], u["name"], f"rectification:{r['id']}:{sub['seq']}",
                    {"rect": r["id"], "code": r["code"], "submission": sub["seq"],
                     "decision": "passed"}, now=now_dt)
                sig_token = sig.token
            self.con.execute(
                "UPDATE rect_submissions SET decision=?,decided_by=?,decided_at=?,"
                "decision_note=?,signature=? WHERE id=?",
                (decision, actor_id, now_iso, note, sig_token, submission_id))
            self.con.execute("UPDATE rectifications SET status=?, passed_at=? WHERE id=?",
                             ("passed" if approved else "rejected",
                              now_iso if approved else None, r["id"]))
            self._event("rect_decided", actor_id,
                        {"rect": r["id"], "code": r["code"], "submission": sub["seq"],
                         "decision": decision, "note": note,
                         "signature": sig_token}, r["contract_id"], now_dt)
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise

    def _rect(self, rect_id: str) -> sqlite3.Row:
        r = self.con.execute("SELECT * FROM rectifications WHERE id=?", (rect_id,)).fetchone()
        if not r:
            raise DomainError("整改项不存在")
        return r

    def _save_photo(self, contract_id: str, rect_id: str, seq: int,
                    filename: str, data: bytes) -> str:
        folder = self.photo_dir / contract_id / rect_id
        folder.mkdir(parents=True, exist_ok=True)
        safe = Path(filename).name.replace("..", "_")
        rel = Path(contract_id) / rect_id / f"s{seq}-{safe}"
        (self.photo_dir / rel).write_bytes(data)
        return str(rel)

    # ---------------------------------------------------------------- 质保索赔 / 争议

    def open_warranty_claim(self, actor_id: str, contract_id: str, title: str,
                            detail: str, rect_id: str | None = None) -> str:
        u = self._require_role(actor_id, "customer")
        cid = _uid("C")
        self._tx()
        try:
            self.con.execute(
                "INSERT INTO warranty_claims(id,contract_id,rect_id,title,detail,status,"
                "opened_by,opened_at) VALUES (?,?,?,?,?,'open',?,?)",
                (cid, contract_id, rect_id, title, detail, actor_id,
                 self.now().isoformat(timespec="seconds")))
            self._event("warranty_claim_opened", actor_id,
                        {"claim": cid, "title": title, "rect": rect_id}, contract_id)
            self.con.commit()
            return cid
        except Exception:
            self.con.rollback()
            raise

    def resolve_claim(self, actor_id: str, claim_id: str, resolution: str) -> None:
        self.user(actor_id)
        row = self.con.execute("SELECT * FROM warranty_claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            raise DomainError("索赔不存在")
        if row["status"] == "resolved":
            return
        self._tx()
        try:
            now_iso = self.now().isoformat(timespec="seconds")
            self.con.execute(
                "UPDATE warranty_claims SET status='resolved',resolved_at=? WHERE id=?",
                (now_iso, claim_id))
            self._event("warranty_claim_resolved", actor_id,
                        {"claim": claim_id, "resolution": resolution}, row["contract_id"])
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise

    def raise_dispute(self, actor_id: str, contract_id: str, title: str, detail: str,
                      ref_type: str | None = None, ref_id: str | None = None) -> str:
        """争议记录：回执延迟、客户新问题、调价/换人异议等都在此留据。"""
        self.user(actor_id)
        did = _uid("D")
        self._tx()
        try:
            self.con.execute(
                "INSERT INTO disputes(id,contract_id,title,detail,ref_type,ref_id,status,"
                "raised_by,raised_at) VALUES (?,?,?,?,?,?,'open',?,?)",
                (did, contract_id, title, detail, ref_type, ref_id, actor_id,
                 self.now().isoformat(timespec="seconds")))
            self._event("dispute_raised", actor_id,
                        {"dispute": did, "title": title, "ref_type": ref_type,
                         "ref_id": ref_id}, contract_id)
            self.con.commit()
            return did
        except Exception:
            self.con.rollback()
            raise

    def resolve_dispute(self, actor_id: str, dispute_id: str, resolution: str) -> None:
        self.user(actor_id)
        self._tx()
        try:
            self.con.execute(
                "UPDATE disputes SET status='resolved',resolved_at=?,resolution=? WHERE id=?",
                (self.now().isoformat(timespec="seconds"), resolution, dispute_id))
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise

    # ---------------------------------------------------------------- 付款

    def book_progress_payment(self, actor_id: str, contract_id: str, amount_cents: int,
                              basis: str, idem_key: str | None = None) -> sqlite3.Row:
        """验收前已发生的进度款入账（种子/迁移用）：直接在模拟网关出账并持双人签放记录。"""
        self._require_role(actor_id, "manager")
        if amount_cents <= 0:
            raise DomainError("付款金额必须为正")
        key = idem_key or f"progress:{contract_id}:{uuid.uuid4().hex[:10]}"
        return self._create_and_execute(
            contract_id, "progress", amount_cents, key,
            {"basis": basis}, actor=actor_id, auto_approve=True)

    def payable_rectifications(self, contract_id: str) -> list[sqlite3.Row]:
        """可申请尾款的整改项：已验收通过、且尚未挂到任何付款指令。"""
        return self.con.execute(
            "SELECT r.* FROM rectifications r LEFT JOIN payment_items pi ON pi.rect_id=r.id "
            "WHERE r.contract_id=? AND r.kind='rectification' AND r.status='passed' "
            "AND pi.payment_id IS NULL ORDER BY r.code",
            (contract_id,)).fetchall()

    def release_payment(self, actor_id: str, contract_id: str, kind: str,
                        rect_ids: list[str] | None = None) -> sqlite3.Row:
        """客户/经理任一方发起放款（幂等），双方都签放后才真正出账。

        两人并发调用同样安全：指令按幂等键唯一，事务串行化，网关只扣一笔。
        """
        u = self.user(actor_id)
        c = self.contract(contract_id)
        if u["id"] not in (c["customer_id"], c["manager_id"]):
            raise PermissionDenied("只有本合同客户或项目经理可发起放款")

        if kind == "final":
            return self._release_final(u, c, rect_ids)
        if kind == "retention":
            return self._release_retention(u, c)
        raise DomainError("放款类型必须是 final 或 retention")

    def _release_final(self, u: sqlite3.Row, c: sqlite3.Row,
                       rect_ids: list[str] | None) -> sqlite3.Row:
        payable = self.payable_rectifications(c["id"])
        by_id = {r["id"]: r for r in payable}
        if rect_ids:
            chosen = sorted(rect_ids)
            key = "final:" + c["id"] + ":" + ",".join(chosen)
            existing = self._by_key(key)
            if existing is not None:
                # 第二人签放 / 重复点击：复用同一笔指令
                return self._attach_approval(c, u, existing)
            missing = [rid for rid in chosen if rid not in by_id]
            if missing:
                # 未验收 / 已申请 / 质保返修（holdback=0）均不得计入尾款
                raise DomainError("存在不可付的整改项（未验收或已申请），尾款不得释放")
            amount = sum(by_id[rid]["holdback_cents"] for rid in chosen)
        else:
            if not payable:
                raise DomainError("没有可释放的尾款：整改项未验收通过或已申请过")
            chosen = sorted(r["id"] for r in payable)
            amount = sum(r["holdback_cents"] for r in payable)
            key = "final:" + c["id"] + ":" + ",".join(chosen)
        if amount <= 0:
            raise DomainError("尾款金额必须为正")
        return self._create_approve_and_maybe_execute(
            c, u, "final", amount, key,
            {"rect_ids": chosen},
            items=[(rid, by_id[rid]["holdback_cents"]) for rid in chosen])

    def _release_retention(self, u: sqlite3.Row, c: sqlite3.Row) -> sqlite3.Row:
        key = f"retention:{c['id']}"
        existing = self._by_key(key)
        if existing is not None:
            # 第二人签放 / 重复点击：同一笔质保金指令
            return self._attach_approval(c, u, existing)
        today = self.now().date()
        end = date.fromisoformat(c["warranty_end"])
        if today < end:
            raise DomainError(f"质保金未到期（{end} 期满），不得提前结清")
        open_claims = self.con.execute(
            "SELECT COUNT(*) AS n FROM warranty_claims WHERE contract_id=? AND status='open'",
            (c["id"],)).fetchone()["n"]
        if open_claims:
            raise DomainError(f"存在 {open_claims} 笔未结质保索赔，质保金暂缓结清")
        paid = self.con.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM payment_instructions "
            "WHERE contract_id=? AND kind='retention' AND status!='pending'",
            (c["id"],)).fetchone()["s"]
        amount = c["retention_cents"] - paid
        if amount <= 0:
            raise DomainError("质保金已结清，不得重复释放")
        return self._create_approve_and_maybe_execute(
            c, u, "retention", amount, key, {"warranty_end": c["warranty_end"]})

    def _by_key(self, key: str) -> sqlite3.Row | None:
        return self.con.execute(
            "SELECT * FROM payment_instructions WHERE idempotency_key=?", (key,)).fetchone()

    def _attach_approval(self, c: sqlite3.Row, u: sqlite3.Row,
                         row: sqlite3.Row) -> sqlite3.Row:
        """在已存在的指令上补签；双方齐备即出账。出账后重复调用幂等返回。"""
        pid = row["id"]
        self._tx()
        try:
            latest = self.con.execute(
                "SELECT * FROM payment_instructions WHERE id=?", (pid,)).fetchone()
            if latest["status"] == "pending":
                now_iso = self.now().isoformat(timespec="seconds")
                self.con.execute(
                    "INSERT OR IGNORE INTO payment_approvals(payment_id,user_id,approved_at)"
                    " VALUES (?,?,?)", (pid, u["id"], now_iso))
                self._event("payment_approved", u["id"],
                            {"payment": pid, "kind": latest["kind"]}, c["id"])
                if self._both_roles_approved(c, pid):
                    ref = self.gateway.execute(pid, latest["amount_cents"])
                    self.con.execute(
                        "UPDATE payment_instructions SET status='executed',gateway_ref=?,"
                        "executed_at=? WHERE id=?", (ref, now_iso, pid))
                    self._event("payment_executed", u["id"],
                                {"payment": pid, "kind": latest["kind"],
                                 "cents": latest["amount_cents"], "gateway_ref": ref}, c["id"])
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise
        return self.con.execute(
            "SELECT * FROM payment_instructions WHERE id=?", (pid,)).fetchone()

    def _create_approve_and_maybe_execute(self, c: sqlite3.Row, u: sqlite3.Row, kind: str,
                                          amount_cents: int, key: str, basis: dict,
                                          items: list[tuple[str, int]] | None = None
                                          ) -> sqlite3.Row:
        import json
        self._tx()
        try:
            now_iso = self.now().isoformat(timespec="seconds")
            existing = self.con.execute(
                "SELECT * FROM payment_instructions WHERE idempotency_key=?", (key,)).fetchone()
            if existing is not None:
                # 幂等：两人重复发起/并发发起都复用同一笔指令
                if existing["amount_cents"] != amount_cents:
                    raise DomainError("同幂等键指令金额不一致")
                pid = existing["id"]
            else:
                try:
                    pid = _uid("P")
                    self.con.execute(
                        "INSERT INTO payment_instructions(id,contract_id,kind,amount_cents,"
                        "idempotency_key,basis,status,created_by,created_at) "
                        "VALUES (?,?,?,?,?,?,'pending',?,?)",
                        (pid, c["id"], kind, amount_cents, key,
                         json.dumps(basis, ensure_ascii=False), u["id"], now_iso))
                    if items:
                        self.con.executemany(
                            "INSERT INTO payment_items(payment_id,rect_id,amount_cents)"
                            " VALUES (?,?,?)",
                            [(pid, rid, amt) for rid, amt in items])
                    self._event("payment_created", u["id"],
                                {"payment": pid, "kind": kind, "cents": amount_cents,
                                 "idempotency_key": key, **basis}, c["id"])
                except sqlite3.IntegrityError:  # 并发下的最后一道防线
                    row = self.con.execute(
                        "SELECT * FROM payment_instructions WHERE idempotency_key=?",
                        (key,)).fetchone()
                    if row["amount_cents"] != amount_cents:
                        raise DomainError("同幂等键指令金额不一致") from None
                    pid = row["id"]

            self.con.execute(
                "INSERT OR IGNORE INTO payment_approvals(payment_id,user_id,approved_at) "
                "VALUES (?,?,?)", (pid, u["id"], now_iso))
            self._event("payment_approved", u["id"], {"payment": pid, "kind": kind}, c["id"])

            row = self.con.execute(
                "SELECT * FROM payment_instructions WHERE id=?", (pid,)).fetchone()
            if row["status"] == "pending" and self._both_roles_approved(c, pid):
                ref = self.gateway.execute(pid, amount_cents)
                self.con.execute(
                    "UPDATE payment_instructions SET status='executed',gateway_ref=?,"
                    "executed_at=? WHERE id=?", (ref, now_iso, pid))
                self._event("payment_executed", u["id"],
                            {"payment": pid, "kind": kind, "cents": amount_cents,
                             "gateway_ref": ref}, c["id"])
            self.con.commit()
            return self.con.execute(
                "SELECT * FROM payment_instructions WHERE id=?", (pid,)).fetchone()
        except Exception:
            self.con.rollback()
            raise

    def approve_payment(self, actor_id: str, payment_id: str) -> sqlite3.Row:
        """对已存在的待签放指令补签（双人签放的第二人）。齐备则立即出账。"""
        u = self.user(actor_id)
        row = self.con.execute("SELECT * FROM payment_instructions WHERE id=?",
                               (payment_id,)).fetchone()
        if not row:
            raise DomainError("付款指令不存在")
        c = self.contract(row["contract_id"])
        if u["id"] not in (c["customer_id"], c["manager_id"]):
            raise PermissionDenied("只有本合同客户或项目经理可签放")
        return self._attach_approval(c, u, row)

    def _both_roles_approved(self, c: sqlite3.Row, payment_id: str) -> bool:
        row = self.con.execute(
            "SELECT COUNT(DISTINCT u.role) AS n FROM payment_approvals pa "
            "JOIN users u ON u.id=pa.user_id WHERE pa.payment_id=?",
            (payment_id,)).fetchone()
        need = self.con.execute(
            "SELECT COUNT(*) AS n FROM payment_approvals WHERE payment_id=? AND user_id IN (?,?)",
            (payment_id, c["customer_id"], c["manager_id"])).fetchone()["n"]
        return row["n"] >= 2 and need >= 2

    def _create_and_execute(self, contract_id: str, kind: str, amount_cents: int, key: str,
                            basis: dict, actor: str, auto_approve: bool = False
                            ) -> sqlite3.Row:
        import json
        c = self.contract(contract_id)
        self._tx()
        try:
            now_iso = self.now().isoformat(timespec="seconds")
            pid = _uid("P")
            self.con.execute(
                "INSERT INTO payment_instructions(id,contract_id,kind,amount_cents,"
                "idempotency_key,basis,status,created_by,created_at,executed_at,gateway_ref) "
                "VALUES (?,?,?,?,?,?, 'executed', ?, ?, ?, ?)",
                (pid, contract_id, kind, amount_cents, key,
                 json.dumps(basis, ensure_ascii=False), actor, now_iso, now_iso,
                 self.gateway.execute(pid, amount_cents)))
            for uid in (c["customer_id"], c["manager_id"]):
                self.con.execute(
                    "INSERT OR IGNORE INTO payment_approvals(payment_id,user_id,approved_at)"
                    " VALUES (?,?,?)", (pid, uid, now_iso))
            self._event("payment_executed", actor,
                        {"payment": pid, "kind": kind, "cents": amount_cents,
                         "basis": basis}, contract_id)
            self.con.commit()
            return self.con.execute(
                "SELECT * FROM payment_instructions WHERE id=?", (pid,)).fetchone()
        except Exception:
            self.con.rollback()
            raise

    def refresh_receipt(self, payment_id: str) -> sqlite3.Row:
        """向支付网关查询回执。回执延迟时抛 ReceiptPending；重复回单不产生重复效果。"""
        row = self.con.execute("SELECT * FROM payment_instructions WHERE id=?",
                               (payment_id,)).fetchone()
        if not row:
            raise DomainError("付款指令不存在")
        if row["status"] == "receipt_confirmed":
            return row  # 回执重放：已确认则幂等返回
        ref = self.gateway.fetch_receipt(payment_id)
        self._tx()
        try:
            self.con.execute(
                "UPDATE payment_instructions SET status='receipt_confirmed',receipt_at=? "
                "WHERE id=?", (self.now().isoformat(timespec="seconds"), payment_id))
            self._event("receipt_confirmed", "system",
                        {"payment": payment_id, "gateway_ref": ref}, row["contract_id"])
            self.con.commit()
            return self.con.execute("SELECT * FROM payment_instructions WHERE id=?",
                                    (payment_id,)).fetchone()
        except Exception:
            self.con.rollback()
            raise

    # ---------------------------------------------------------------- 结算视图

    def settlement(self, contract_id: str) -> dict:
        c = self.contract(contract_id)
        amount = self.current_amount(contract_id)
        retention = c["retention_cents"]

        pays = self.con.execute(
            "SELECT * FROM payment_instructions WHERE contract_id=? ORDER BY created_at, id",
            (contract_id,)).fetchall()
        executed = [p for p in pays if p["status"] in ("executed", "receipt_confirmed")]
        pending = [p for p in pays if p["status"] == "pending"]
        paid_total = sum(p["amount_cents"] for p in executed)
        pending_total = sum(p["amount_cents"] for p in pending)
        retention_paid = sum(p["amount_cents"] for p in executed if p["kind"] == "retention")
        retention_left = retention - retention_paid

        rects = self.con.execute(
            "SELECT * FROM rectifications WHERE contract_id=? ORDER BY code",
            (contract_id,)).fetchall()
        linked = {r["rect_id"] for r in self.con.execute(
            "SELECT pi.rect_id FROM payment_items pi JOIN payment_instructions p "
            "ON p.id=pi.payment_id WHERE p.contract_id=?", (contract_id,)).fetchall()}

        payable_final = blocked_final = 0
        total_holdback = 0
        for r in rects:
            if r["kind"] != "rectification":
                continue
            total_holdback += r["holdback_cents"]
            if r["status"] == "passed" and r["id"] not in linked:
                payable_final += r["holdback_cents"]
            elif r["id"] not in linked:
                blocked_final += r["holdback_cents"]

        # 分类付款
        executed_progress = sum(p["amount_cents"] for p in executed if p["kind"] == "progress")
        executed_retention = sum(p["amount_cents"] for p in executed if p["kind"] == "retention")
        pending_retention = sum(p["amount_cents"] for p in pending if p["kind"] == "retention")
        # 质保金余额：已结清与已挂起待签放的部分都不再计入
        retention_left = retention - executed_retention - pending_retention

        # 未结算工程款 = 合同价 − 质保金 − 整改留存总额 − 已付进度款
        # （整改留存通过尾款释放，其余通过进度款释放）
        unbilled_progress = amount - retention - total_holdback - executed_progress

        # 金额守恒恒等式（含待签放）
        accounted = (paid_total + pending_total + payable_final + blocked_final
                     + max(unbilled_progress, 0) + retention_left)
        balanced = accounted == amount and unbilled_progress >= 0 and retention_left >= 0

        return {
            "contract": c,
            "amount_cents": amount,
            "retention_cents": retention,
            "retention_left_cents": retention_left,
            "paid_total_cents": paid_total,
            "pending_total_cents": pending_total,
            "payable_final_cents": payable_final,
            "blocked_final_cents": blocked_final,
            "unbilled_progress_cents": unbilled_progress,
            "balanced": balanced,
            "unaccounted_cents": amount - accounted,
            "warranty_end": c["warranty_end"],
            "warranty_matured": self.now().date() >= date.fromisoformat(c["warranty_end"]),
            "open_claim_count": self.con.execute(
                "SELECT COUNT(*) AS n FROM warranty_claims WHERE contract_id=? AND status='open'",
                (contract_id,)).fetchone()["n"],
            "payments": pays,
            "rectifications": rects,
            "linked_rect_ids": linked,
        }

    def rectification_detail(self, rect_id: str) -> dict:
        r = self._rect(rect_id)
        subs = self.con.execute(
            "SELECT * FROM rect_submissions WHERE rect_id=? ORDER BY seq", (rect_id,)).fetchall()
        photos = self.con.execute(
            "SELECT * FROM rect_photos WHERE rect_id=? ORDER BY id", (rect_id,)).fetchall()
        return {"rect": r, "submissions": subs, "photos": photos}
