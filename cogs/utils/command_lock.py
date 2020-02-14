class CommandIsLocked(Exception):
    pass


class CLock:
    """Super primitive class for command locking."""

    def __init__(self):
        self._locked = False

    def __enter__(self):
        if self._locked:
            raise CommandIsLocked

        self._locked = True
        # We don't care about `with X`.
        return None

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._locked = False
