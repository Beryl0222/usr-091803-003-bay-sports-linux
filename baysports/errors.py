"""领域错误。

所有业务拒绝都使用 DomainError 的子类，错误码稳定，便于 API 映射与跨地区排错。
"""


class DomainError(Exception):
    code = "domain_error"
    http_status = 400

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "details": self.details}


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 422


class RuleViolationError(DomainError):
    """名单窗口、替补资格等硬规则被触碰。"""

    code = "rule_violation"
    http_status = 409


class AuthorizationError(DomainError):
    code = "forbidden"
    http_status = 403


class ConflictError(DomainError):
    """幂等冲突：同一登记编号重复提交但内容不同。"""

    code = "idempotency_conflict"
    http_status = 409
