class LLMServiceUnavailable(RuntimeError):
    """The configured LLM provider could not return a usable response."""

    def __init__(self, message: str, failure_code: str = "provider_failure",
                 upstream_status_code: int | None = None) -> None:
        super().__init__(message)
        self.failure_code = failure_code if failure_code in {
            "timeout", "connect_timeout", "read_timeout", "provider_failure",
        } else "provider_failure"
        self.upstream_status_code = (upstream_status_code if isinstance(upstream_status_code, int)
                                     and not isinstance(upstream_status_code, bool)
                                     and 100 <= upstream_status_code <= 599 else None)
