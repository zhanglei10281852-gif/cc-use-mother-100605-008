"""领域错误。

所有错误都继承 ValueError，既符合起始项目 ``raise ValueError`` 的既有风格，
又能让调用方按具体类型区分处理（HTTP 层据此映射状态码）。
"""

from __future__ import annotations


class DomainError(ValueError):
    """业务规则错误基类。"""


class NotFoundError(DomainError):
    """引用的实体不存在。"""


class ConflictError(DomainError):
    """请求与当前状态冲突（重复创建、编号占用等）。"""


class ValidationFailure(DomainError):
    """输入未通过业务校验。"""


class PermissionDeniedError(DomainError):
    """当前角色在当前工单阶段无权执行该动作。"""


class InvalidStageError(DomainError):
    """工单状态机不允许该转换。"""


class DuplicateWorkOrderError(DomainError):
    """重复报修：已存在同一设备、同一故障且尚未关闭的工单。"""

    def __init__(self, message: str, existing_work_order_id: str):
        super().__init__(message)
        self.existing_work_order_id = existing_work_order_id


class BackfillRejectedError(DomainError):
    """离线补录被拒绝（时间越界或工单已终态）。"""


class LogIntegrityError(DomainError):
    """事件日志哈希链断裂，数据可能被篡改或损坏。"""

    def __init__(self, message: str, broken_seq: int | None = None):
        super().__init__(message)
        self.broken_seq = broken_seq
