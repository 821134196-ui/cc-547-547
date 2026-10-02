"""核心规则测试：金额守恒、到期限制、回执重放、签署权限。

另含：双人并发只出一笔指令、未验收整改不计可付、尾款不吞质保金、
合同调价/换经理留痕、质保返修 holdback=0。
直接运行：python3 -m unittest discover -s tests
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from renovation.adapters import MockPaymentGateway, ReceiptPending
from renovation.db import connect
from renovation.money import cents_to_yuan, yuan_to_cents
from renovation.service import DomainError, PermissionDenied, SettlementService


class _Clock:
    def __init__(self, dt: datetime) -> None:
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt

    def set(self, dt: datetime) -> None:
        self.dt = dt


PHOTO = b"<svg xmlns='http://www.w3.org/2000/svg'/>"


class CaseBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="reno-test-"))
        self.db = self.tmp / "t.db"
        self.photos = self.tmp / "photos"
        self.con = connect(self.db)
        self.gateway = MockPaymentGateway()
        self.clock = _Clock(datetime(2026, 9, 20, 10, 0))
        self.svc = SettlementService(self.con, gateway=self.gateway,
                                     photo_dir=self.photos, clock=self.clock)
        self.cid = "C1"
        for uid, name, role in [("u_c", "客户甲", "customer"),
                                ("u_m", "经理乙", "manager"),
                                ("u_m2", "经理丙", "manager")]:
            self.con.execute("INSERT INTO users(id,name,role) VALUES (?,?,?)", (uid, name, role))
        self.con.commit()
        self.amount = yuan_to_cents("100000")
        self.retention = yuan_to_cents("5000")
        self.svc.create_contract(self.cid, "测试工程", "u_c", "u_m",
                                 self.amount, self.retention, "2028-09-20")

    def tearDown(self) -> None:
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def settle(self):
        return self.svc.settlement(self.cid)

    def add_rect(self, code: str, holdback_yuan: str, status_path=None) -> str:
        """建整改项；status_path 可选 (submit, pass) 直接走到验收通过。"""
        rid = self.svc.add_rectification(
            "u_m", self.cid, code, f"{code} 标题", "某处", "问题描述",
            yuan_to_cents(holdback_yuan))
        if status_path:
            do_submit, do_pass = status_path
            if do_submit:
                sid = self.svc.submit_rectification(
                    "u_m", rid, "已整改", [(f"{code}.svg", PHOTO, "照片")])
                if do_pass:
                    self.svc.decide_submission("u_c", sid, True, "通过")
        return rid


class TestConservation(CaseBase):
    def assert_balanced(self):
        s = self.settle()
        self.assertTrue(
            s["balanced"],
            f"金额不守恒：差额 {cents_to_yuan(s['unaccounted_cents'])} | "
            f"{s['amount_cents']} vs paid={s['paid_total_cents']} "
            f"pending={s['pending_total_cents']} payable={s['payable_final_cents']} "
            f"blocked={s['blocked_final_cents']} retention={s['retention_left_cents']}")
        return s

    def test_initial_conservation(self):
        s = self.assert_balanced()
        self.assertEqual(s["amount_cents"], self.amount)
        self.assertEqual(s["paid_total_cents"], 0)
        self.assertEqual(s["retention_left_cents"], self.retention)
        # 初始：除质保金外全部是「未结算工程款」，尚无整改留存
        self.assertEqual(s["blocked_final_cents"], 0)
        self.assertEqual(s["unbilled_progress_cents"] + s["retention_left_cents"], self.amount)

    def test_conservation_through_lifecycle(self):
        # 进度款 60,000
        self.svc.book_progress_payment("u_m", self.cid, yuan_to_cents("60000"),
                                       "进度款", idem_key="k-p1")
        self.assert_balanced()
        # 两个整改项共留存 8,000；其余 27,000 未挂整改
        r1 = self.add_rect("R1", "3000")
        r2 = self.add_rect("R2", "5000")
        self.assert_balanced()
        # R1 未提交/未验收 → 不可付
        self.assertEqual(self.settle()["payable_final_cents"], 0)
        self.assertEqual(self.settle()["blocked_final_cents"], yuan_to_cents("8000"))
        self.assertEqual(self.settle()["unbilled_progress_cents"], yuan_to_cents("27000"))
        # R1 提交但客户尚未确认 → 仍不可付
        self.svc.submit_rectification("u_m", r1, "改了", [("x.svg", PHOTO, "x")])
        self.assertEqual(self.settle()["payable_final_cents"], 0)
        # 客户确认 R1 → 可付 3,000
        sub = self.con.execute(
            "SELECT id FROM rect_submissions WHERE rect_id=? ORDER BY seq DESC LIMIT 1",
            (r1,)).fetchone()["id"]
        self.svc.decide_submission("u_c", sub, True, "ok")
        s = self.assert_balanced()
        self.assertEqual(s["payable_final_cents"], yuan_to_cents("3000"))
        self.assertEqual(s["blocked_final_cents"], yuan_to_cents("5000"))
        self.assertEqual(s["unbilled_progress_cents"], yuan_to_cents("27000"))
        # R2 被打回 → 不计可付
        sid2 = self.svc.submit_rectification("u_m", r2, "改了", [])
        self.svc.decide_submission("u_c", sid2, False, "新问题：没改好")
        self.assertEqual(self.settle()["payable_final_cents"], yuan_to_cents("3000"))
        # 经理发起、客户签放 R1 尾款 3,000
        p = self.svc.release_payment("u_m", self.cid, "final", [r1])
        self.assertEqual(p["status"], "pending")
        self.assert_balanced()  # 待签放仍守恒
        p = self.svc.release_payment("u_c", self.cid, "final", [r1])
        self.assertEqual(p["status"], "executed")
        s = self.assert_balanced()
        self.assertEqual(s["paid_total_cents"], yuan_to_cents("63000"))
        self.assertEqual(s["payable_final_cents"], 0)
        # 质保金独立：尾款释放后质保金原封不动
        self.assertEqual(s["retention_left_cents"], self.retention)

    def test_price_adjustment_keeps_versions_and_conservation(self):
        self.svc.adjust_price("u_c", self.cid, yuan_to_cents("108000"),
                              "增项 8,000：加做吊顶")
        s = self.assert_balanced()
        self.assertEqual(s["amount_cents"], yuan_to_cents("108000"))
        versions = self.con.execute(
            "SELECT version_no,amount_cents,change_reason FROM contract_versions "
            "ORDER BY version_no").fetchall()
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0]["amount_cents"], self.amount)   # 旧版本保留
        self.assertEqual(versions[1]["amount_cents"], yuan_to_cents("108000"))
        self.assertIn("吊顶", versions[1]["change_reason"])

    def test_warranty_rectification_has_no_holdback(self):
        rid = self.svc.add_rectification("u_m", self.cid, "W1", "质保返修", "卫生间",
                                         "渗水", 0, kind="warranty")
        sid = self.svc.submit_rectification("u_m", rid, "修好", [])
        self.svc.decide_submission("u_c", sid, True, "ok")
        s = self.assert_balanced()
        # 质保返修通过不产生尾款，也不从质保金扣款（索赔处理另走流程）
        self.assertEqual(s["payable_final_cents"], 0)
        self.assertEqual(s["retention_left_cents"], self.retention)


class TestRetentionMaturity(CaseBase):
    def test_release_before_maturity_rejected(self):
        self.clock.set(datetime(2028, 9, 19, 9, 0))
        with self.assertRaises(DomainError) as cm:
            self.svc.release_payment("u_m", self.cid, "retention")
        self.assertIn("未到期", str(cm.exception))

    def test_release_blocked_by_open_claim_after_maturity(self):
        self.clock.set(datetime(2028, 9, 20, 9, 0))
        self.svc.open_warranty_claim("u_c", self.cid, "窗台渗水", "打胶不密实")
        with self.assertRaises(DomainError) as cm:
            self.svc.release_payment("u_m", self.cid, "retention")
        self.assertIn("未结", str(cm.exception))
        # 到期前一天即使无索赔也不行（到期与索赔双重限制）
        self.clock.set(datetime(2028, 9, 19, 9, 0))
        with self.assertRaises(DomainError):
            self.svc.release_payment("u_m", self.cid, "retention")

    def test_release_succeeds_when_matured_and_claims_resolved(self):
        self.clock.set(datetime(2028, 9, 20, 9, 0))
        claim = self.svc.open_warranty_claim("u_c", self.cid, "小问题", "x")
        # 未结时被挡
        with self.assertRaises(DomainError):
            self.svc.release_payment("u_m", self.cid, "retention")
        self.svc.resolve_claim("u_m", claim, "修好")
        # 双人签放后结清，金额恰为质保金
        p = self.svc.release_payment("u_m", self.cid, "retention")
        self.assertEqual(p["status"], "pending")
        p = self.svc.release_payment("u_c", self.cid, "retention")
        self.assertEqual(p["status"], "executed")
        self.assertEqual(p["amount_cents"], self.retention)
        # 结清后重复发起：幂等返回原指令，不新增、不重复扣款
        p_again = self.svc.release_payment("u_m", self.cid, "retention")
        self.assertEqual(p_again["id"], p["id"])
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) AS n FROM payment_instructions WHERE kind='retention'").fetchone()["n"], 1)
        self.assertEqual(self.gateway.executed_total_cents(), self.retention)
        s = self.settle()
        self.assertTrue(s["balanced"])
        self.assertEqual(s["retention_left_cents"], 0)

    def test_final_release_cannot_touch_retention(self):
        self.clock.set(datetime(2027, 1, 1, 9, 0))  # 远未到期
        rid = self.add_rect("R1", "2000", (True, True))
        p = self.svc.release_payment("u_m", self.cid, "final", [rid])
        p = self.svc.release_payment("u_c", self.cid, "final", [rid])
        self.assertEqual(p["kind"], "final")
        self.assertEqual(p["amount_cents"], yuan_to_cents("2000"))
        kinds = {r["kind"] for r in self.con.execute(
            "SELECT DISTINCT kind FROM payment_instructions WHERE status!='pending'")}
        self.assertNotIn("retention", kinds)
        self.assertEqual(self.settle()["retention_left_cents"], self.retention)


class TestReceiptReplay(CaseBase):
    def test_receipt_delayed_then_replay_is_idempotent(self):
        pay = self.svc.book_progress_payment(
            "u_m", self.cid, yuan_to_cents("12345"), "进度款", idem_key="k-rcp")
        # 出账后、回执延迟
        with self.assertRaises(ReceiptPending):
            self.svc.refresh_receipt(pay["id"])
        self.assertEqual(
            self.con.execute("SELECT status FROM payment_instructions WHERE id=?",
                             (pay["id"],)).fetchone()["status"], "executed")
        # 回执到达 → 确认一次
        self.gateway.deliver_receipt(pay["id"])
        row = self.svc.refresh_receipt(pay["id"])
        self.assertEqual(row["status"], "receipt_confirmed")
        first_receipt_at = row["receipt_at"]
        # 回执重放：再次查询不报错、不改状态、不产生新付款
        row2 = self.svc.refresh_receipt(pay["id"])
        self.assertEqual(row2["status"], "receipt_confirmed")
        self.assertEqual(row2["receipt_at"], first_receipt_at)
        n = self.con.execute("SELECT COUNT(*) AS n FROM payment_instructions").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_gateway_execute_idempotent_under_repeated_calls(self):
        amt = yuan_to_cents("999")
        r1 = self.gateway.execute("PAY-X", amt)
        r2 = self.gateway.execute("PAY-X", amt)  # 重试/重放
        self.assertEqual(r1, r2)
        self.assertEqual(self.gateway.executed_count(), 1)
        self.assertEqual(self.gateway.executed_total_cents(), amt)
        with self.assertRaises(Exception):
            self.gateway.execute("PAY-X", amt + 1)  # 同键不同金额拒绝


class TestSigningPermission(CaseBase):
    def test_only_customer_can_accept_rectification(self):
        rid = self.add_rect("R1", "1000")
        sid = self.svc.submit_rectification("u_m", rid, "改好", [])
        # 经理不能自签
        with self.assertRaises(PermissionDenied):
            self.svc.decide_submission("u_m", sid, True, "我自己验收")
        # 其他经理也不能签
        with self.assertRaises(PermissionDenied):
            self.svc.decide_submission("u_m2", sid, True, "旁观经理代签")
        # 客户签认生效，且产生签名令牌
        self.svc.decide_submission("u_c", sid, True, "合格")
        row = self.con.execute("SELECT decision,signature FROM rect_submissions WHERE id=?",
                               (sid,)).fetchone()
        self.assertEqual(row["decision"], "passed")
        self.assertTrue(row["signature"].startswith("SIG-"))

    def test_customer_cannot_record_progress(self):
        with self.assertRaises(PermissionDenied):
            self.svc.record_progress("u_c", self.cid, "地砖", 10, "㎡", yuan_to_cents("1000"))

    def test_non_manager_cannot_submit_rectification(self):
        rid = self.add_rect("R1", "1000")
        with self.assertRaises(PermissionDenied):
            self.svc.submit_rectification("u_c", rid, "客户代整改", [])

    def test_former_manager_cannot_record_after_replacement(self):
        self.svc.replace_manager("u_c", self.cid, "u_m2", "原经理调岗")
        with self.assertRaises(PermissionDenied):
            self.svc.record_progress("u_m", self.cid, "墙面", 5, "㎡", yuan_to_cents("500"))
        # 新任经理可以
        self.svc.record_progress("u_m2", self.cid, "墙面", 5, "㎡", yuan_to_cents("500"))
        # 任期历史保留两任
        tenures = self.con.execute(
            "SELECT seq,manager_id,ended_at FROM pm_tenures ORDER BY seq").fetchall()
        self.assertEqual(len(tenures), 2)
        self.assertIsNotNone(tenures[0]["ended_at"])
        self.assertIsNone(tenures[1]["ended_at"])

    def test_duplicate_decision_rejected(self):
        rid = self.add_rect("R1", "1000")
        sid = self.svc.submit_rectification("u_m", rid, "改好", [])
        self.svc.decide_submission("u_c", sid, True, "ok")
        with self.assertRaises(DomainError):  # 不可重复签署/翻案
            self.svc.decide_submission("u_c", sid, False, "反悔了")


class TestDualReleaseIdempotent(CaseBase):
    def _passed_rect(self):
        return self.add_rect("R1", "4000", (True, True))

    def test_two_roles_one_instruction_sequential(self):
        rid = self._passed_rect()
        p1 = self.svc.release_payment("u_m", self.cid, "final", [rid])
        p2 = self.svc.release_payment("u_c", self.cid, "final", [rid])  # 第二人
        p3 = self.svc.release_payment("u_c", self.cid, "final", [rid])  # 再点一次
        self.assertEqual(p1["id"], p2["id"])
        self.assertEqual(p2["id"], p3["id"])
        self.assertEqual(p2["status"], "executed")
        n = self.con.execute(
            "SELECT COUNT(*) AS n FROM payment_instructions WHERE kind='final'").fetchone()["n"]
        self.assertEqual(n, 1)
        self.assertEqual(self.gateway.executed_count(), 1)
        self.assertEqual(self.gateway.executed_total_cents(), yuan_to_cents("4000"))

    def test_concurrent_dual_release_one_instruction(self):
        rid = self._passed_rect()
        errors: list[Exception] = []
        barrier = threading.Barrier(2)
        results: list[str] = []

        def actor(uid: str) -> None:
            con2 = connect(self.db)
            svc2 = SettlementService(con2, gateway=self.gateway,
                                     photo_dir=self.photos, clock=self.clock)
            try:
                barrier.wait(timeout=5)
                p = svc2.release_payment(uid, self.cid, "final", [rid])
                results.append(p["id"])
            except Exception as ex:  # noqa: BLE001
                errors.append(ex)
            finally:
                con2.close()

        t1 = threading.Thread(target=actor, args=("u_m",))
        t2 = threading.Thread(target=actor, args=("u_c",))
        t1.start(); t2.start(); t1.join(10); t2.join(10)
        self.assertFalse(errors, f"并发放款出现异常: {errors}")
        self.assertEqual(len(results), 2)
        self.assertEqual(len(set(results)), 1, "两人并发放款产生了多笔指令")
        n = self.con.execute(
            "SELECT COUNT(*) AS n FROM payment_instructions WHERE kind='final'").fetchone()["n"]
        self.assertEqual(n, 1)
        self.assertEqual(self.gateway.executed_count(), 1)
        self.assertEqual(self.gateway.executed_total_cents(), yuan_to_cents("4000"))
        self.assertTrue(self.settle()["balanced"])

    def test_unpassed_rectification_cannot_be_released(self):
        rid = self.add_rect("R1", "1000")  # open，未提交
        with self.assertRaises(DomainError):
            self.svc.release_payment("u_m", self.cid, "final", [rid])
        rid2 = self.add_rect("R2", "2000")
        sid = self.svc.submit_rectification("u_m", rid2, "改", [])
        self.svc.decide_submission("u_c", sid, False, "新问题")
        with self.assertRaises(DomainError):  # 打回项不可付
            self.svc.release_payment("u_c", self.cid, "final", [rid2])
        self.assertEqual(self.gateway.executed_count(), 0)


class TestMoney(unittest.TestCase):
    def test_roundtrip(self):
        for y, c in [("0", 0), ("518000", 51800000), ("207200", 20720000),
                     ("3000.05", 300005), ("12.005", 1201)]:
            self.assertEqual(yuan_to_cents(y), c, y)
            self.assertEqual(cents_to_yuan(c), f"{c // 100:,}.{c % 100:02d}")


if __name__ == "__main__":
    unittest.main()
