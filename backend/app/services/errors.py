"""业务异常：路由层把这类异常翻译成可读的失败结果，而不是 500。"""
from __future__ import annotations


class DomainError(RuntimeError):
    """请求本身不满足业务规则（缺字段、冲突、轮次失效等）。"""
