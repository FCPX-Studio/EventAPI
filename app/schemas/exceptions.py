class LimitExceededError(Exception):
    """A claim request refused by a limit: answered with `status_code` and a Retry-After header"""

    def __init__(self, status_code: int, retry_after: int, reason: str) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        self.reason = reason
        super().__init__(reason)