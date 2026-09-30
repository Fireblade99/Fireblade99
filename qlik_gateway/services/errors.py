class ServiceError(Exception):
    """Business-rule rejection that the API turns into an HTTP error."""

    def __init__(self, status_code: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}  # additional fields for the JSON error body
