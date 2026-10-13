class WorkflowError(Exception):
    """A workflow action was refused for a reason that can be shown to the user."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message

    def __str__(self):
        return self.message


STALE_MESSAGE = "This record was changed by someone else. Please review the latest details and try again."

REASON_MAX_LENGTH = 2000


def clean_reason(value, *, required, message="Please give a reason."):
    """Trim a decision reason/note and enforce the server-side length limit.

    Reasons are kept permanently in the append-only audit history, so they are limited in length
    and the forms ask HR not to include personal details beyond what the decision needs.
    """
    value = (value or "").strip()
    if required and not value:
        raise WorkflowError(message)
    if len(value) > REASON_MAX_LENGTH:
        raise WorkflowError(f"Please keep this to {REASON_MAX_LENGTH} characters or fewer.")
    return value
