"""金额工具：系统内部一律使用“分”(INTEGER) 存储，杜绝浮点误差。"""
from __future__ import annotations


def to_yuan(cents: int) -> str:
    """分 -> 会计格式元，例如 1000000 -> '10,000.00'。"""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100:,}.{cents % 100:02d}"


def parse_cents(yuan) -> int:
    """元字符串/数字 -> 分(四舍五入到整分)。"""
    if isinstance(yuan, bool):
        raise ValueError("金额非法")
    if isinstance(yuan, int):
        return yuan * 100
    s = str(yuan).replace(",", "").replace("¥", "").strip()
    if not s:
        raise ValueError("金额为空")
    return int(round(float(s) * 100))
