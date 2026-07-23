"""Qt bridge for asynchronous, batched thumbnail delivery."""

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from thumbnail_service import ThumbnailRequest


class _Signals(QObject):
    completed = Signal(object, object)


class _Task(QRunnable):
    def __init__(self, function, token):
        super().__init__()
        self.function = function
        self.token = token
        self.signals = _Signals()

    def run(self):
        try:
            value = self.function()
            self.signals.completed.emit(self.token, value)
        except Exception as exc:
            self.signals.completed.emit(self.token, exc)


class QtThumbnailAdapter(QObject):
    """Collect UI requests into one <=50 item background render batch."""

    def __init__(self, database, service, dimensions=(200, 280),
                 synchronous=False, parent=None):
        super().__init__(parent)
        self.database = database
        self.service = service
        self.dimensions = tuple(dimensions)
        self.synchronous = synchronous
        self._pending = []
        self._scheduled = False
        self._running = False
        self._token = 0
        self._callbacks = {}

    def request_thumbnail(self, document_id, page_number, receiver):
        self._pending.append((int(document_id), int(page_number), receiver))
        if len(self._pending) > 50:
            # A workspace must never ask for more than its visible batch.
            self._pending.pop()
            raise ValueError("thumbnail batch cannot exceed 50 items")
        if not self.synchronous and not self._scheduled:
            self._scheduled = True
            QTimer.singleShot(0, self.flush)

    def flush(self):
        self._scheduled = False
        if self._running or not self._pending:
            return
        pending, self._pending = self._pending, []
        self._token += 1
        token = self._token
        requests = []
        callbacks = []
        for document_id, page_number, receiver in pending:
            document = self.database.get_source_document(document_id)
            if not document:
                receiver(None)
                continue
            identity = document.get("file_sha256") or "{}:{}".format(
                document.get("file_size", 0), document.get("file_mtime_ns", 0))
            requests.append(ThumbnailRequest(
                Path(document["filepath"]), identity, page_number,
                self.dimensions))
            callbacks.append(receiver)
        if not requests:
            return
        if self.synchronous:
            self._deliver(callbacks, self._get(requests))
            return
        self._running = True
        self._callbacks[token] = callbacks
        task = _Task(lambda: self._get(requests), token)
        task.signals.completed.connect(self._finished)
        QThreadPool.globalInstance().start(task)

    def _get(self, requests):
        return self.service.get_batch(requests)

    def _finished(self, token, value):
        callbacks = self._callbacks.pop(token, [])
        self._running = False
        self._deliver(callbacks, value)
        if self._pending:
            self.flush()

    @staticmethod
    def _deliver(callbacks, value):
        if isinstance(value, Exception):
            for callback in callbacks:
                callback(None)
            return
        for callback, result in zip(callbacks, value):
            callback(result)
