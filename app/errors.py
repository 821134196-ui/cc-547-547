class DomainError(Exception):
    """业务规则被违反（金额守恒、到期限制等）。"""


class PermissionDenied(Exception):
    """当前角色无权执行该操作，例如客户代录完成量、项目经理自签整改。"""


class NotFound(LookupError):
    pass


class Conflict(Exception):
    """并发冲突或唯一约束冲突。"""
