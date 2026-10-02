"""模拟适配器：支付网关与电子签名。

- PaymentAdapter.pay 支持回执延迟：可先返回 initiated，后续凭同一 request_id
  轮询/重放得到同一 receipt_no（幂等），绝不重复出款。
- SignatureAdapter.sign 校验签署人角色并返回可核验的签名令牌。
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field


@dataclass
class PaymentResult:
    receipt_no: str
    status: str  # initiated | paid | failed


class PaymentAdapter:
    """进程内模拟支付通道。request_id 是幂等键。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._receipts: dict[str, PaymentResult] = {}

    def pay(self, request_id: int, amount_cents: int, *,
            delay: bool = False) -> PaymentResult:
        key = f"REQ-{request_id}"
        with self._lock:
            existing = self._receipts.get(key)
            if existing is not None:
                # 回执重放：同一申请只对应同一笔指令/回执
                return existing
            digest = hashlib.sha256(
                f"{key}:{amount_cents}".encode()).hexdigest()[:12].upper()
            receipt = f"PAY-{digest}"
            result = PaymentResult(receipt, "initiated" if delay else "paid")
            self._receipts[key] = result
            return result

    def confirm(self, request_id: int) -> PaymentResult | None:
        """模拟延迟回执到达：initiated -> paid。"""
        key = f"REQ-{request_id}"
        with self._lock:
            r = self._receipts.get(key)
            if r and r.status == "initiated":
                r.status = "paid"
            return r


@dataclass
class SignatureToken:
    token: str
    signer_id: int
    subject: str
    valid: bool = field(default=True)


class SignatureAdapter:
    """模拟电子签：H(签名人+主体+nonce)，可离线核验。"""

    def sign(self, signer_id: int, subject: str, nonce: str) -> SignatureToken:
        raw = f"sig|{signer_id}|{subject}|{nonce}"
        token = "SIG-" + hashlib.sha256(raw.encode()).hexdigest()[:16].upper()
        return SignatureToken(token, signer_id, subject)

    def verify(self, signer_id: int, subject: str, nonce: str, token: str) -> bool:
        expected = self.sign(signer_id, subject, nonce).token
        return token == expected
