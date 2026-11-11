"""Structured human review; save a new fixed plan before running any checks."""
from copy import deepcopy

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QPlainTextEdit, QPushButton, QScrollArea, QSplitter,
                               QVBoxLayout, QWidget)
from PySide6.QtCore import Qt

from .desktop_tasks import Task
from .repair_suite import MAX_CHECKS
from .suite_review import editable_plan, revise_suite


class SuiteReviewDialog(QDialog):
    def __init__(self, directory, output_root=None, parent=None):
        super().__init__(parent)
        self.directory = directory
        self.output_root = output_root
        self.plan = editable_plan(directory)
        self.result_data = {}
        self.task = None
        self.pending = None
        self._current = None
        self._closing = False
        self.setWindowTitle('编辑检查 · 另存审阅版本')
        self.resize(1020, 800)
        layout = QVBoxLayout(self)
        notice = QLabel('修改后另存独立计划，保留原计划与已有报告。保存后请重新审阅并明确开始验收。')
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.content = QWidget()
        content = QVBoxLayout(self.content)
        heading = QFormLayout()
        self.title_edit = QLineEdit(self.plan['title'])
        self.overview_edit = QPlainTextEdit(self.plan['overview'])
        self.overview_edit.setMaximumHeight(65)
        heading.addRow('计划标题', self.title_edit)
        heading.addRow('检查思路', self.overview_edit)
        content.addLayout(heading)
        split = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.check_list = QListWidget()
        self.check_list.currentRowChanged.connect(self._select)
        left_layout.addWidget(self.check_list)
        actions = QHBoxLayout()
        self.add_button = QPushButton('添加检查')
        self.remove_button = QPushButton('移除检查')
        self.add_button.clicked.connect(self.add_check)
        self.remove_button.clicked.connect(self.remove_check)
        actions.addWidget(self.add_button)
        actions.addWidget(self.remove_button)
        left_layout.addLayout(actions)
        split.addWidget(left)
        form_widget = QWidget()
        self.form = QFormLayout(form_widget)
        self.id_edit = QLineEdit()
        self.check_title_edit = QLineEdit()
        self.role_combo = QComboBox()
        self.role_combo.addItem('目标修复：旧版复现声明故障，新版通过', 'repair')
        self.role_combo.addItem('回归保护：比较两版实际表现', 'regression')
        self.purpose_edit = QPlainTextEdit()
        self.expected_edit = QPlainTextEdit()
        self.assumptions_edit = QPlainTextEdit()
        self.assumptions_edit.setPlaceholderText('每行一个需要审阅的假设，至少一项')
        for edit in (self.purpose_edit, self.expected_edit, self.assumptions_edit):
            edit.setMaximumHeight(75)
        self.source_combo = QComboBox()
        self.source_combo.addItem('独立测试代码', 'standalone')
        self.source_combo.addItem('已有项目测试（使用修复后版本的文件）', 'project')
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText('tests/test_example.py::test_one[参数标识]')
        self.code_edit = QPlainTextEdit()
        self.code_edit.setMinimumHeight(150)
        self.failure_type_edit = QLineEdit()
        self.failure_message_edit = QLineEdit()
        self.form.addRow('检查 ID', self.id_edit)
        self.form.addRow('检查标题', self.check_title_edit)
        self.form.addRow('检查用途', self.role_combo)
        self.form.addRow('检查目标', self.purpose_edit)
        self.form.addRow('预期行为', self.expected_edit)
        self.form.addRow('需要审阅的假设', self.assumptions_edit)
        self.form.addRow('测试来源', self.source_combo)
        self.form.addRow('精确测试目标', self.target_edit)
        self.form.addRow('独立测试代码', self.code_edit)
        self.form.addRow('旧版异常类型', self.failure_type_edit)
        self.form.addRow('旧版消息包含', self.failure_message_edit)
        self.role_combo.currentIndexChanged.connect(self._conditions)
        self.source_combo.currentIndexChanged.connect(self._conditions)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(form_widget)
        split.addWidget(scroll)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        content.addWidget(split, 1)
        layout.addWidget(self.content, 1)
        self.status_label = QLabel('可以逐项修改。计划需保留 1–20 项检查和至少一个目标修复项。')
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        self.save_button = buttons.addButton('另存审阅版本', QDialogButtonBox.ButtonRole.ActionRole)
        self.save_button.clicked.connect(self.save_revision)
        layout.addWidget(buttons)
        self._refresh(0)

    def _conditions(self):
        project = self.source_combo.currentData() == 'project'
        self.form.setRowVisible(self.target_edit, project)
        self.form.setRowVisible(self.code_edit, not project)
        repair = self.role_combo.currentData() == 'repair'
        self.form.setRowVisible(self.failure_type_edit, repair)
        self.form.setRowVisible(self.failure_message_edit, repair)

    def _store_current(self):
        if self._current is None:
            return
        project = self.source_combo.currentData() == 'project'
        repair = self.role_combo.currentData() == 'repair'
        self.plan['checks'][self._current] = {
            'id': self.id_edit.text().strip(), 'title': self.check_title_edit.text().strip(),
            'role': self.role_combo.currentData(), 'purpose': self.purpose_edit.toPlainText(),
            'expected_behavior': self.expected_edit.toPlainText(),
            'assumptions': [line.strip() for line in self.assumptions_edit.toPlainText().splitlines() if line.strip()],
            'test_target': self.target_edit.text().strip() if project else None,
            'test_code': None if project else self.code_edit.toPlainText(),
            'expected_failure': {'type': self.failure_type_edit.text().strip(),
                                 'message_contains': self.failure_message_edit.text()} if repair else None,
        }
        self.check_list.item(self._current).setText(self.check_title_edit.text() or self.id_edit.text())

    def _select(self, row):
        self._store_current()
        self._current = row if 0 <= row < len(self.plan['checks']) else None
        if self._current is None:
            return
        check = self.plan['checks'][row]
        self.id_edit.setText(check['id'])
        self.check_title_edit.setText(check['title'])
        self.role_combo.setCurrentIndex(self.role_combo.findData(check['role']))
        self.purpose_edit.setPlainText(check['purpose'])
        self.expected_edit.setPlainText(check['expected_behavior'])
        self.assumptions_edit.setPlainText('\n'.join(check['assumptions']))
        self.source_combo.setCurrentIndex(self.source_combo.findData('project' if check['test_target'] is not None else 'standalone'))
        self.target_edit.setText(check['test_target'] or '')
        self.code_edit.setPlainText(check['test_code'] or '')
        expected = check['expected_failure'] or {}
        self.failure_type_edit.setText(expected.get('type', ''))
        self.failure_message_edit.setText(expected.get('message_contains', ''))
        self._conditions()

    def _refresh(self, row):
        self._current = None
        self.check_list.blockSignals(True)
        self.check_list.clear()
        self.check_list.addItems([check['title'] or check['id'] for check in self.plan['checks']])
        self.check_list.setCurrentRow(row)
        self.check_list.blockSignals(False)
        self._select(row)
        self.add_button.setEnabled(len(self.plan['checks']) < MAX_CHECKS)
        self.remove_button.setEnabled(len(self.plan['checks']) > 1)

    def add_check(self):
        if self.task or len(self.plan['checks']) >= MAX_CHECKS:
            return
        self._store_current()
        ids = {check['id'] for check in self.plan['checks']}
        number = next(i for i in range(1, MAX_CHECKS + 2) if f'check-{i}' not in ids)
        self.plan['checks'].append({'id': f'check-{number}', 'title': '新检查', 'role': 'regression',
                                   'purpose': '', 'expected_behavior': '', 'assumptions': [],
                                   'test_code': '', 'test_target': None, 'expected_failure': None})
        self._refresh(len(self.plan['checks']) - 1)

    def remove_check(self):
        if self.task or self._current is None or len(self.plan['checks']) <= 1:
            return
        row = self._current
        self.plan['checks'].pop(row)
        self._refresh(min(row, len(self.plan['checks']) - 1))

    def save_revision(self):
        if self.task:
            return
        self._store_current()
        self.plan.update(title=self.title_edit.text().strip(), overview=self.overview_edit.toPlainText())
        plan = deepcopy(self.plan)
        self.content.setEnabled(False)
        self.save_button.setEnabled(False)
        self.status_label.setText('正在校验检查并复制固定输入…')
        self.task = Task(lambda control: revise_suite(self.directory, plan, self.output_root), self)
        self.task.succeeded.connect(self._saved)
        self.task.failed.connect(lambda error: self.status_label.setText('未保存：' + error))
        self.task.finished.connect(self._finished)
        self.task.start()

    def _saved(self, state):
        self.result_data = state

    def _finished(self):
        self.task.deleteLater()
        self.task = None
        self.content.setEnabled(True)
        self.save_button.setEnabled(True)
        if self._closing:
            super().reject()
        elif self.result_data:
            self.accept()

    def reject(self):
        if self.task:
            self._closing = True
            self.status_label.setText('正在完成本次保存，完成后关闭。')
        else:
            super().reject()

    def closeEvent(self, event):
        if self.task:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
