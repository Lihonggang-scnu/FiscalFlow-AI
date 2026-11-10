"""Domain errors with stable, user-facing status codes."""


class TaxHarnessError(Exception):
    """Base error for deterministic business operations."""

    code = "error"


class InvalidInputError(TaxHarnessError):
    code = "invalid_input"


class NotFoundError(TaxHarnessError):
    code = "not_found"


class PermissionDeniedError(TaxHarnessError):
    code = "permission_denied"


class RuleNotFoundError(TaxHarnessError):
    code = "rule_not_found"


class RuleConflictError(TaxHarnessError):
    code = "rule_conflict"


class RuleExecutionError(TaxHarnessError):
    code = "cannot_determine"


class RevisionConflictError(TaxHarnessError):
    code = "revision_conflict"


class IdempotencyConflictError(TaxHarnessError):
    code = "idempotency_conflict"


class SourceIntegrityError(TaxHarnessError):
    code = "source_integrity_error"


class SessionOwnershipError(TaxHarnessError):
    code = "session_ownership_error"


class BudgetExceeded(TaxHarnessError):
    code = "budget_exceeded"


class ContextBudgetExceeded(TaxHarnessError):
    code = "context_budget_exceeded"


class LiveModelDisabled(TaxHarnessError):
    code = "live_model_disabled"


class TurnCancelled(TaxHarnessError):
    code = "cancelled"
