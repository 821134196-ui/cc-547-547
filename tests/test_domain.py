"""领域规则测试：金额守恒、到期限制、回执重放、签署权限、并发放款。"""
from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

from app.adapters import PaymentAdapter, SignatureAdapter
from app.db import Database
from app.errors import Conflict, DomainError, PermissionDenied
from app.pngutil import make_paint_png
from app.seed import seed_all
from app.services import Service


def make_service(tmpdir: str, clock=None) -> Service:
    db = Database(str(Path(tmpdir) / "t.db"))
    return Service(db, str(Path(tmpdir) / "photos"),
                   PaymentAdapter(), SignatureAdapter(),
                   clock=clock or date.today)


def assert_conservation(svc: Service, pid: int):
    s = svc.settlement(pid)
    total = (s["paid"]["total_cents"] + s["paid"]["in_flight_cents"]
             + s["tail"]["available_cents"] + s["tail"]["pending_cents"]
             + s["tail"]["unallocated_cents"] + s["retention"]["held_cents"])
    assert total == s["contract_cents"], (total, s["contract_cents"], s)


class ConservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = make_service(self.tmp)
        self.ids = seed_all(self.svc)
        self.pid = self.ids["project"]
        self.customer = self.ids["customer"]
        self.mgr = self.ids["mgr_new"]

    def test_seed_partitions_equal_contract(self):
        s = self.svc.settlement(self.pid)
        self.assertEqual(s["contract_cents"], 20_000_000)        # 200,000
        self.assertEqual(s["retention_cents"], 1_000_000)         # 10,000
        self.assertEqual(s["paid"]["progress_cents"], 13_000_000)
        self.assertEqual(s["tail"]["available_cents"], 1_500_000)  # R2 已签认
        # R3 被新问题挂起 20,000；R2b 待签认 8,000；R4 待整改 5,000
        self.assertEqual(s["tail"]["pending_cents"], 3_300_000)
        # 130,000 已付 + 10,000 在途 + 15,000 可付 + 33,000 待处理 + 10,000 尾差 + 10,000 质保
        self.assertEqual(s["tail"]["unallocated_cents"], 200_000)
        assert_conservation(self.svc, self.pid)

    def test_conservation_after_tail_release(self):
        self.svc.release_tail(self.pid, self.customer, [self.ids["r2"]])
        assert_conservation(self.svc, self.pid)
        s = self.svc.settlement(self.pid)
        self.assertEqual(s["tail"]["available_cents"], 0)
        self.assertEqual(s["paid"]["tail_cents"], 1_500_000)

    def test_conservation_after_delayed_receipt_confirmed(self):
        req = self.ids["r1_payment"]
        before = self.svc.settlement(self.pid)
        self.assertEqual(before["paid"]["in_flight_cents"], 1_000_000)
        self.svc.confirm_receipt(req)
        after = self.svc.settlement(self.pid)
        self.assertEqual(after["paid"]["in_flight_cents"], 0)
        self.assertEqual(after["paid"]["tail_cents"], 1_000_000)
        assert_conservation(self.svc, self.pid)

    def test_conservation_after_contract_adjustment(self):
        self.svc.adjust_contract(self.pid, self.mgr, "-2000",
                                 "减项：取消背景墙造型", "减项签证单 GC-015")
        s = self.svc.settlement(self.pid)
        self.assertEqual(s["contract_cents"], 19_800_000)
        self.assertEqual(s["retention_cents"], 990_000)  # 5% 联动
        assert_conservation(self.svc, self.pid)


class TailPaymentRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = make_service(self.tmp)
        self.ids = seed_all(self.svc)
        self.pid = self.ids["project"]
        self.customer = self.ids["customer"]
        self.mgr = self.ids["mgr_new"]

    def test_unaccepted_rect_not_payable(self):
        # 待整改 / 待签认 均不得计入可付金额
        for rid in (self.ids["r2b"], self.ids["r4"]):
            with self.assertRaises(DomainError):
                self.svc.release_tail(self.pid, self.customer, [rid])

    def test_signed_parent_blocked_by_new_customer_issue(self):
        # R3 已签认，但客户追加的新问题未结 => 尾款暂缓
        with self.assertRaises(DomainError):
            self.svc.release_tail(self.pid, self.customer, [self.ids["r3"]])
        s = self.svc.settlement(self.pid)
        self.assertEqual(s["tail"]["available_cents"], 1_500_000)  # 仅 R2

    def test_parent_releasable_after_child_closed(self):
        # 经理整改子问题 -> 客户签认 -> 父项恢复可付
        with self.svc.db.connect() as c:
            child = c.execute(
                "SELECT id FROM rectifications WHERE parent_id=? AND status='open'",
                (self.ids["r3"],)).fetchone()["id"]
        self.svc.submit_rect(child, self.mgr,
                             [(make_paint_png(pattern="grid"), "门套防潮处理后照片")])
        self.svc.sign_rect(child, self.customer)
        res = self.svc.release_tail(self.pid, self.customer, [self.ids["r3"]])
        self.assertEqual(res["amount_cents"], 2_000_000)

    def test_tail_cannot_consume_retention(self):
        # 合同 100,000，质保金 5%=5,000；非质保可付上限 95,000
        tmp = tempfile.mkdtemp()
        svc = make_service(tmp)
        cust = svc.create_user("客户甲", "customer")
        m = svc.create_user("经理甲", "manager")
        pid = svc.create_project("小项目", m, "100000", 5.0, 180,
                                 acceptance_iso=date.today().isoformat())
        svc.pay_progress(pid, m, "90000")
        # 再分配 6,000 整改尾款将突破 95,000 上限 => 拒绝（质保金不被误释放）
        with self.assertRaises(DomainError):
            svc.create_rect(pid, m, "超限整改", "6000")
        ok = svc.create_rect(pid, m, "额度内整改", "5000")
        svc.submit_rect(ok, m, [(make_paint_png(), "照片")])
        svc.sign_rect(ok, cust)
        paid = svc.release_tail(pid, cust, [ok])
        self.assertEqual(paid["amount_cents"], 500_000)
        # 质保金仍在
        self.assertEqual(svc.settlement(pid)["retention"]["held_cents"], 500_000)

    def test_double_release_same_rect_rejected(self):
        self.svc.release_tail(self.pid, self.customer, [self.ids["r2"]])
        with self.assertRaises(Conflict):
            self.svc.release_tail(self.pid, self.customer, [self.ids["r2"]])

    def test_concurrent_release_single_instruction(self):
        """两人/两请求并发放同一整改项，只产生一笔付款指令。"""
        errors = []

        def fire():
            try:
                self.svc.release_tail(self.pid, self.customer, [self.ids["r2"]])
            except (Conflict, DomainError) as e:
                errors.append(str(e))

        threads = [threading.Thread(target=fire) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with self.svc.db.connect() as c:
            n = c.execute(
                "SELECT COUNT(*) n FROM payments p JOIN payment_requests r ON r.id=p.request_id"
                " WHERE r.project_id=? AND r.kind='tail' AND p.amount_cents=1500000",
                (self.pid,)).fetchone()["n"]
            items = c.execute(
                "SELECT COUNT(*) n FROM payment_request_items WHERE rect_id=?",
                (self.ids["r2"],)).fetchone()["n"]
        self.assertEqual(n, 1)
        self.assertEqual(items, 1)
        self.assertEqual(len(errors), 1)  # 一个成功，一个冲突


class ReceiptReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = make_service(self.tmp)
        self.ids = seed_all(self.svc)

    def test_delayed_then_replay_returns_same_receipt(self):
        req = self.ids["r1_payment"]
        # 在途时重放：同一回执，不新增指令
        r1 = self.svc.replay_receipt(req)
        r2 = self.svc.replay_receipt(req)
        self.assertEqual(r1["receipt_no"], r2["receipt_no"])
        self.assertEqual(r1["payment_rows"], 1)
        # 延迟回执到达
        conf = self.svc.confirm_receipt(req)
        self.assertEqual(conf["status"], "paid")
        self.assertEqual(conf["receipt_no"], r1["receipt_no"])
        # 到账后再重放仍幂等
        r3 = self.svc.replay_receipt(req)
        self.assertEqual(r3["receipt_no"], r1["receipt_no"])
        self.assertEqual(r3["payment_rows"], 1)
        # 适配器层也不会产生第二回执
        again = self.svc.pay.pay(req, 1_000_000)
        self.assertEqual(again.receipt_no, r1["receipt_no"])


class SignaturePermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.svc = make_service(self.tmp)
        self.ids = seed_all(self.svc)
        self.pid = self.ids["project"]
        self.customer = self.ids["customer"]
        self.mgr = self.ids["mgr_new"]

    def test_manager_cannot_sign_rect(self):
        with self.assertRaises(PermissionDenied):
            self.svc.sign_rect(self.ids["r2b"], self.mgr)

    def test_customer_cannot_enter_completion(self):
        with self.assertRaises(PermissionDenied):
            self.svc.enter_completion(self.pid, self.customer, "木工", 1, "项")

    def test_customer_cannot_submit_rect(self):
        with self.assertRaises(PermissionDenied):
            self.svc.submit_rect(self.ids["r4"], self.customer,
                                 [(make_paint_png(), "x")])

    def test_only_customer_can_replace_manager(self):
        m2 = self.svc.create_user("钱经理", "manager")
        with self.assertRaises(PermissionDenied):
            self.svc.replace_manager(self.pid, self.mgr, m2, "调岗", "通知")

    def test_only_customer_can_release_tail(self):
        with self.assertRaises(PermissionDenied):
            self.svc.release_tail(self.pid, self.mgr, [self.ids["r2"]])

    def test_sign_requires_submitted_and_photo(self):
        with self.assertRaises(DomainError):  # open 状态不能签
            self.svc.sign_rect(self.ids["r4"], self.customer)

    def test_signature_token_verifies(self):
        out = self.svc.sign_rect(self.ids["r2b"], self.customer)
        with self.svc.db.tx() as c:
            self.assertTrue(self.svc.verify_rect_signature(c, self.ids["r2b"]))
        self.assertTrue(out["signature"].startswith("SIG-"))


class RetentionDueTests(unittest.TestCase):
    def _due_project(self, svc, warranty_days=7, price="100000"):
        cust = svc.create_user("客户乙", "customer")
        m = svc.create_user("经理乙", "manager")
        accepted = (date.today() - timedelta(days=warranty_days + 10)).isoformat()
        pid = svc.create_project("到期样例", m, price, 5.0, warranty_days,
                                 acceptance_iso=accepted)
        return pid, cust, m

    def test_release_before_due_blocked(self):
        # 种子项目：验收 60 天、质保 180 天，未到期
        tmp = tempfile.mkdtemp()
        svc = make_service(tmp)
        ids = seed_all(svc)
        with self.assertRaises(DomainError):
            svc.release_retention(ids["project"], ids["customer"])

    def test_release_after_due_and_no_open_items(self):
        tmp = tempfile.mkdtemp()
        svc = make_service(tmp)
        pid, cust, m = self._due_project(svc)
        res = svc.release_retention(pid, cust)
        self.assertEqual(res["amount_cents"], 500_000)
        # 结清后重复放款被拒绝
        with self.assertRaises(Conflict):
            svc.release_retention(pid, cust)

    def test_open_warranty_repair_blocks_release(self):
        tmp = tempfile.mkdtemp()
        cur = {"d": date.today()}
        svc = make_service(tmp, clock=lambda: cur["d"])
        cust = svc.create_user("客户丙", "customer")
        m = svc.create_user("经理丙", "manager")
        accepted = (cur["d"] - timedelta(days=30)).isoformat()
        pid = svc.create_project("未结返修样例", m, "100000", 5.0, 60,
                                 acceptance_iso=accepted)
        wr = svc.open_warranty_repair(pid, cust, "返碱", "照片")
        cur["d"] += timedelta(days=100)  # 推进到质保期满后
        with self.assertRaises(DomainError):
            svc.release_retention(pid, cust)
        svc.repair_done(wr, m)
        svc.confirm_repair(wr, cust)
        res = svc.release_retention(pid, cust)
        self.assertEqual(res["amount_cents"], 500_000)

    def test_claim_deducts_retention(self):
        tmp = tempfile.mkdtemp()
        cur = {"d": date.today()}
        svc = make_service(tmp, clock=lambda: cur["d"])
        cust = svc.create_user("客户丁", "customer")
        m = svc.create_user("经理丁", "manager")
        accepted = (cur["d"] - timedelta(days=30)).isoformat()
        pid = svc.create_project("索赔样例", m, "100000", 5.0, 60,
                                 acceptance_iso=accepted)
        wr = svc.open_warranty_repair(pid, cust, "瓷砖空鼓", "照片")
        cur["d"] += timedelta(days=100)
        svc.repair_done(wr, m)
        svc.confirm_repair(wr, cust, as_claim=True, cost_yuan="2000")
        res = svc.release_retention(pid, cust)
        self.assertEqual(res["amount_cents"], 300_000)  # 5,000 - 2,000
        assert_conservation(svc, pid)

    def test_warranty_repair_only_within_period(self):
        tmp = tempfile.mkdtemp()
        svc = make_service(tmp)
        pid, cust, m = self._due_project(svc, warranty_days=7)
        with self.assertRaises(DomainError):
            svc.open_warranty_repair(pid, cust, "超期报修")

    def test_retention_requires_basis_changes(self):
        tmp = tempfile.mkdtemp()
        svc = make_service(tmp)
        pid, cust, m = self._due_project(svc)
        with self.assertRaises(DomainError):
            svc.adjust_contract(pid, m, "100", "", "")
        m2 = svc.create_user("孙经理", "manager")
        with self.assertRaises(DomainError):
            svc.replace_manager(pid, cust, m2, "", "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
