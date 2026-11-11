"""One background operation at a time; Qt signals never touch widgets off-thread."""
from PySide6.QtCore import QThread, Signal

from .runner import RunControl, RunCancelled


class Task(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    progress = Signal(str, object)

    def __init__(self, action, parent=None, *, runtime_values=None):
        super().__init__(parent)
        self.action = action
        self.control = RunControl(progress=self.progress.emit, runtime_values=runtime_values)

    def run(self):
        try:
            result = self.action(self.control)
        except RunCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)
        else:
            self.succeeded.emit(result)
