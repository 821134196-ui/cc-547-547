"""模拟适配器：支付网关与电子签名。

真实环境会换成银行/支付通道与 CA 签名服务；这里用本地确定性实现，
但保留真实适配器应有的接口与失败/延迟语义。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime


class PaymentError(Exception):
    pass


class ReceiptPending(PaymentError):
    """网关已受理，但付款回执延迟（争议记录的常见来源）。"""


class MockPaymentGateway:
    """模拟支付通道。

    - execute 对同一 (payment_id, amount) 幂等：重复出账返回同一流水号，不重复扣款。
    - fetch_receipt 回执可能尚未到达，抛 ReceiptPending。
    - 网关侧按 payment_id 记录已处理金额，可直接验证「只扣一笔」。
    """

    def __init__(self) -> None:
        self._executed: dict[str, int] = {}   # payment_id -> cents
        self._receipts: dict[str, str] = {}   # payment_id -> gateway_ref（已回单）

    def execute(self, payment_id: str, amount_cents: int, *, now: datetime | None = None) -> str:
        if amount_cents <= 0:
            raise PaymentError("出账金额必须为正")
        if payment_id in self._executed:
            # 幂等：已出账则直接回放原流水号，绝不重复扣款
            if self._executed[payment_id] != amount_cents:
                raise PaymentError("同笔指令金额与已出账金额不一致")
            return self._ref(payment_id)
        self._executed[payment_id] = amount_cents
        ref = self._ref(payment_id)
        return ref

    def deliver_receipt(self, payment_id: str) -> str:
        """模拟回执延迟后到达。"""
        if payment_id not in self._executed:
            raise PaymentError("该指令尚未出账")
        ref = self._ref(payment_id)
        self._receipts[payment_id] = ref
        return ref

    def fetch_receipt(self, payment_id: str) -> str:
        ref = self._receipts.get(payment_id)
        if ref is None:
            raise ReceiptPending(f"付款 {payment_id} 的银行回执尚未到达")
        return ref

    def executed_total_cents(self) -> int:
        """测试辅助：网关实际累计扣款（用于金额守恒断言）。"""
        return sum(self._executed.values())

    def executed_count(self) -> int:
        return len(self._executed)

    def hydrate(self, rows) -> None:
        """重启后从账务库恢复网关状态：已出账指令恢复扣款记录，已回单的恢复回执。"""
        for row in rows:
            keys = row.keys()
            if row["status"] in ("executed", "receipt_confirmed"):
                self._executed.setdefault(row["id"], row["amount_cents"])
            if row["status"] == "receipt_confirmed":
                ref = row["gateway_ref"] if "gateway_ref" in keys else None
                self._receipts[row["id"]] = ref or self._ref(row["id"])

    @staticmethod
    def _ref(payment_id: str) -> str:
        digest = hashlib.sha1(payment_id.encode()).hexdigest()[:10].upper()
        return f"MOCK-{digest}"


@dataclass
class Signature:
    token: str
    signer: str
    subject: str
    signed_at: str


class MockSignatureService:
    """模拟电子签名：对「签署人 + 签署内容」生成可校验令牌。"""

    def sign(self, signer_id: str, signer_name: str, subject: str,
             payload: dict, *, now: datetime | None = None) -> Signature:
        ts = (now or datetime.now()).isoformat(timespec="seconds")
        basis = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(f"{signer_id}|{subject}|{basis}|{ts}".encode()).hexdigest()[:16]
        return Signature(token=f"SIG-{digest.upper()}", signer=signer_id,
                         subject=subject, signed_at=ts)

    def verify(self, sig: Signature, signer_id: str, payload: dict) -> bool:
        return bool(sig.token) and sig.signer == signer_id and sig.subject != ""
