"""Dialogs for exchanging and reviewing saved replay evidence."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from .project_review_summary import render_replay_markdown
from .replay_verification import replay_verification_text


_PATH_LABELS = {'source': '原始证据', 'replay': '重放证据', 'receipt': '回传目录', 'destination': '保存目录'}


class ReceiptPathsDialog(QDialog):
    def __init__(self, title, fields, initial_directory, parent, *, suggested_name='replay-receipt'):
        super().__init__(parent)
        self.setObjectName('replayReceiptPaths')
        self.setWindowTitle(title)
        self.initial_directory = initial_directory
        self.suggested_name = suggested_name
        self.edits = {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        layout.addLayout(form)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.submit = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.submit.setText(title)
        self.submit.setObjectName('primary')
        self.submit.setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        for field in fields:
            self._add_path(form, field)
        next(iter(self.edits.values())).setFocus()
        self.resize(580, self.sizeHint().height())

    def _add_path(self, form, field):
        label = QLabel(_PATH_LABELS[field])
        edit = QLineEdit()
        edit.setObjectName('receiptPath_' + field)
        edit.setAccessibleName(label.text())
        label.setBuddy(edit)
        if field in ('source', 'replay'):
            edit.setPlaceholderText('证据 ZIP 或 report.json')
        elif field == 'destination':
            edit.setPlaceholderText('新目录路径')
            edit.setToolTip('选择已有的上级目录，或直接输入新的回执目录路径。')
        else:
            edit.setPlaceholderText('接收方回传的目录')
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        browse = QPushButton('选择位置…' if field == 'destination' else '浏览…')
        browse.setAutoDefault(False)
        browse.setAccessibleName('选择' + label.text())
        browse.clicked.connect(lambda: self._browse(field))
        layout.addWidget(browse)
        form.addRow(label, row)
        self.edits[field] = edit
        edit.textChanged.connect(self._sync_submit)

    def _sync_submit(self):
        self.submit.setEnabled(all(edit.text().strip() for edit in self.edits.values()))

    def _browse(self, field):
        label = _PATH_LABELS[field]
        start = self.edits[field].text().strip() or str(self.initial_directory)
        if field in ('source', 'replay'):
            path, _ = QFileDialog.getOpenFileName(self, '选择' + label, start,
                                                  '项目证据 (*.zip report.json)')
        elif field == 'destination':
            current = self.edits[field].text().strip()
            start = str(Path(current).expanduser().parent) if current else str(self.initial_directory)
            parent = QFileDialog.getExistingDirectory(self, '选择回执的上级目录', start)
            path = str(Path(parent) / self.suggested_name) if parent else ''
        else:
            path = QFileDialog.getExistingDirectory(self, '选择回传目录', start)
        if path:
            self.edits[field].setText(path)

    def paths(self):
        return {field: Path(edit.text().strip()).expanduser() for field, edit in self.edits.items()}


class ReceiptResultDialog(QDialog):
    def __init__(self, title, headline, verification, parent, *, issues=(), save=False, directory=None, open_folder=None):
        super().__init__(parent)
        self.setObjectName('replayReceiptResult')
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        summary = QLabel(headline)
        summary.setObjectName('receiptResultHeadline')
        summary.setWordWrap(True)
        summary.setTextFormat(Qt.TextFormat.PlainText)
        summary.setStyleSheet('font-size: 18px; font-weight: 600;')
        layout.addWidget(summary)
        if verification:
            outcome = verification['summary'].partition(' · ')[0]
            if outcome != headline:
                layout.addWidget(QLabel(outcome))
            if counts := verification.get('counts'):
                self._add_counts(layout, counts)
        if directory:
            location = QLabel(str(directory))
            location.setWordWrap(True)
            location.setTextFormat(Qt.TextFormat.PlainText)
            location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            location.setAccessibleName('回执保存目录')
            layout.addWidget(location)

        details = QPlainTextEdit()
        details.setObjectName('receiptResultDetails')
        details.setAccessibleName('回执核对详情')
        details.setReadOnly(True)
        sections = ['\n'.join(issues)] if issues else []
        if verification:
            body = replay_verification_text(verification).partition('\n')[2].strip()
            if body:
                sections.append(body)
        content = '\n\n'.join(sections)
        details.setPlainText(content)
        layout.addWidget(details, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText('关闭')
        buttons.rejected.connect(self.reject)
        if verification and not issues and verification['status'] != 'unavailable':
            copy = buttons.addButton('复制 Markdown', QDialogButtonBox.ButtonRole.ActionRole)
            copy.setObjectName('receiptCopyMarkdown')

            def copy_markdown():
                QApplication.clipboard().setText(render_replay_markdown(verification))
                copy.setText('已复制')

            copy.clicked.connect(copy_markdown)
        if save:
            export = buttons.addButton('保存回执…', QDialogButtonBox.ButtonRole.AcceptRole)
            export.setObjectName('receiptSave')
            buttons.accepted.connect(self.accept)
        if open_folder:
            browse = buttons.addButton('打开回执目录', QDialogButtonBox.ButtonRole.ActionRole)
            browse.setObjectName('receiptOpenFolder')
            browse.clicked.connect(open_folder)
        layout.addWidget(buttons)
        self.resize(620, min(500, 256 + 20 * content.count('\n')))

    @staticmethod
    def _add_counts(layout, counts):
        metrics = QHBoxLayout()
        for key, title in (('matched', '一致'), ('different', '有变化'), ('incomplete', '未能核对')):
            card = QFrame()
            card.setObjectName('detailPanel')
            column = QVBoxLayout(card)
            column.setContentsMargins(12, 8, 12, 8)
            column.setSpacing(2)
            value = QLabel(str(counts[key]))
            value.setObjectName('cardTitle')
            value.setAccessibleName(title + ' ' + str(counts[key]))
            column.addWidget(value)
            column.addWidget(QLabel(title))
            metrics.addWidget(card, 1)
        layout.addLayout(metrics)
