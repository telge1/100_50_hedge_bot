import threading


class WSReady:
    """
    Simple helper to signal when the WebSocket has delivered the first position snapshot.
    """

    def __init__(self):
        self._event = threading.Event()

    def mark_ready(self):
        self._event.set()

    def wait(self, timeout=None):
        self._event.wait(timeout=timeout)

    def is_ready(self):
        return self._event.is_set()
