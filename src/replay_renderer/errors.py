class ReplayRendererError(ValueError):
    """An input cannot be handled by the currently supported format subset."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
