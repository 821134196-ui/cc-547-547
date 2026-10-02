"""金额工具：系统内部一律以「分」(int) 表示。"""
from __future__ import annotations


def yuan_to_cents(value: float | int | str) -> int:
    """元转分，四舍五入到整数分，避免二进制浮点误差。"""
    from decimal import Decimal, ROUND_HALF_UP

    return int(
        (Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def cents_to_yuan(cents: int) -> str:
    """分转展示用元字符串，保留两位小数并加千分位。"""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    whole, frac = divmod(cents, 100)
    return f"{sign}{whole:,}.{frac:02d}"
