"""AI project acceptance on the existing desktop, with explicit send and run steps."""
from pathlib import Path
import json
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .desktop_creation import _CreationDialog
from .desktop_batch import BatchDialog
from .desktop_environment import ProjectEnvironmentDialog
from .desktop_models import ModelConnectionDialog, SavedModelSelector
from .desktop_store import generation_preview, check_preview, read_json
from .desktop_tasks import Task
from .desktop_suite_review import SuiteReviewDialog
from .suite_probe import probe_suite_check, read_suite_probe, checked_probe_suggestion
from .repair_suite import prepare_suite, generate_suite, recover_suite, read_suite, _checked_state, prepare_explanation, explain_suite
from .workspace import WORKTREE


def _preview(parent, directory, config):
    preview = generation_preview(directory, config)
    dialog = QDialog(parent)
    dialog.setWindowTitle('发送前预览')
    dialog.resize(840, 620)
    layout = QVBoxLayout(dialog)
    heading = QLabel(f"服务 {preview['endpoint']}\n模型 {preview['model']}\n协议 {preview['protocol']}\n思考强度 {preview['reasoning_description']}")
    heading.setWordWrap(True)
    heading.setTextFormat(Qt.TextFormat.PlainText)
    layout.addWidget(heading)
    text = QPlainTextEdit(preview['prompt'])
    text.setReadOnly(True)
    layout.addWidget(text)
    consent = QCheckBox('我已审阅以上内容，同意发送到此服务')
    layout.addWidget(consent)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
    send = buttons.addButton('发送', QDialogButtonBox.ButtonRole.AcceptRole)
    send.setEnabled(False)
    consent.toggled.connect(send.setEnabled)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return preview if dialog.exec() == QDialog.DialogCode.Accepted else None


class ExplanationDialog(QDialog):
    def __init__(self, summary_path, config_path, parent=None):
        super().__init__(parent)
        self.summary_path = summary_path
        self.config_path = config_path
        self.task = None
        self._closing = False
        self.setWindowTitle('AI 解读实际验收结果')
        self.resize(840, 650)
        layout = QVBoxLayout(self)
        source = Path(summary_path)
        if source.is_dir():
            source = source / ('summary.json' if (source / 'summary.json').is_file() else 'report.json')
        summary = read_json(source)
        self.status_label = QLabel('实际验收状态 ' + summary['status'])
        layout.addWidget(self.status_label)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        layout.addWidget(self.text, 1)
        self.generate_button = QPushButton('预览并请求 AI 解读')
        self.generate_button.clicked.connect(self.generate)
        layout.addWidget(self.generate_button)

    def generate(self):
        if self.task:
            return
        try:
            prepared = prepare_explanation(self.summary_path)
            directory = prepared['directory']
            preview = _preview(self, directory, self.config_path)
            if not preview:
                return
            def action(control):
                check_preview(directory, self.config_path, preview)
                return explain_suite(directory, config_file=self.config_path)
            self.generate_button.setEnabled(False)
            self.task = Task(action, self)
            self.task.succeeded.connect(self._shown)
            self.task.failed.connect(lambda error: self.text.setPlainText('解读未完成 ' + error))
            self.task.finished.connect(self._finished)
            self.task.start()
        except (OSError, ValueError) as exc:
            self.text.setPlainText(str(exc))

    def _shown(self, data):
        lines = ['AI 解读', data['summary'], '']
        lines.extend(f"检查 {item['id']}  {item['comment']}" for item in data['findings'])
        lines.extend(['', '建议的下一步', *data['next_steps']])
        self.text.setPlainText('\n'.join(lines))

    def _finished(self):
        self.task.deleteLater()
        self.task = None
        self.generate_button.setEnabled(True)
        if self._closing:
            super().reject()

    def reject(self):
        if self.task:
            self._closing = True
            self.status_label.setText('正在结束模型请求，完成后关闭')
        else:
            super().reject()

    def closeEvent(self, event):
        if self.task:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)


class AISuiteDialog(_CreationDialog):
    kind = 'case'

    def __init__(self, store, parent=None):
        super().__init__(dict(store.settings), parent)
        self.store = store
        self.directory = None
        self.state = None
        self.last_summary = None
        self.last_batch = None
        self.last_review = None
        self.probe_data = None
        self._probing = False
        self.setWindowTitle('AI 项目验收')
        self.resize(960, 820)
        self.workspace_check = QCheckBox('使用当前未提交的修改')
        self.workspace_check.setChecked(True)
        self.after_combo.setEnabled(False)
        self.workspace_check.toggled.connect(self._workspace_changed)
        self.form.insertRow(3, '', self.workspace_check)
        self.issue_edit = QPlainTextEdit()
        self.issue_edit.setPlaceholderText('描述这次修复的问题、预期行为，以及需要保护的相关功能')
        self.issue_edit.setMaximumHeight(100)
        self.form.addRow('问题描述', self.issue_edit)
        self.issue_edit.textChanged.connect(self._discard_prepared)
        self.model_edit = QLineEdit(str(store.settings.get('model_config') or ''))
        self.model_edit.setParent(self)
        self.model_edit.hide()
        self.model_selector = SavedModelSelector(self.model_edit, self.work_root / 'models', self)
        self.model_selector.changed.connect(self._model_selected)
        self.model_button = QPushButton('选择 / 连接模型…')
        self.model_button.clicked.connect(self.configure_model)
        self.form.addRow('模型连接', self._row(self.model_selector, self.model_button))
        self.environment_button = QPushButton('检查 / 准备项目环境…')
        self.environment_button.clicked.connect(self.inspect_environment)
        self.load_button = QPushButton('打开已有计划…')
        self.load_button.clicked.connect(lambda: self.load_plan())
        self.import_button = QPushButton('导入模型响应…')
        self.import_button.clicked.connect(lambda: self.import_response())
        self.form.addRow('', self._row(self.environment_button, self.load_button, self.import_button))
        self.recover_button = QPushButton('恢复已保存响应')
        self.recover_button.setEnabled(False)
        self.recover_button.setToolTip('只读取匹配的本地响应，不调用模型或执行测试')
        self.recover_button.clicked.connect(self.recover_response)
        self.edit_button = QPushButton('编辑检查 / 另存审阅版本…')
        self.edit_button.setEnabled(False)
        self.edit_button.clicked.connect(self.edit_plan)
        self.form.addRow('', self._row(self.recover_button, self.edit_button))
        self.check_tree = QTreeWidget()
        self.check_tree.setHeaderLabels(['检查', '用途'])
        self.check_tree.setMaximumHeight(180)
        self.check_tree.currentItemChanged.connect(self.show_check)
        self.form.addRow('审阅检查清单', self.check_tree)
        self.review = QPlainTextEdit()
        self.review.setReadOnly(True)
        self.review.setMaximumHeight(180)
        self.form.addRow('检查代码和假设', self.review)
        self.probe_button = QPushButton('试运行所选检查的旧版')
        self.probe_button.setToolTip('会实际执行可信项目的旧版测试，保存日志和固定输入；不调用模型。')
        self.probe_button.clicked.connect(self.probe_selected_check)
        self.open_probe_button = QPushButton('打开旧版观察记录…')
        self.open_probe_button.clicked.connect(lambda: self.open_probe())
        self.form.addRow('', self._row(self.probe_button, self.open_probe_button))
        self.probe_output = QPlainTextEdit()
        self.probe_output.setReadOnly(True)
        self.probe_output.setMaximumHeight(160)
        self.probe_output.setPlaceholderText('试运行只观察旧版；正式验收仍需重新运行两版。')
        self.form.addRow('旧版实际观察', self.probe_output)
        self.probe_confirm = QCheckBox('我已核对观察到的故障，确认它对应本次目标问题')
        self.probe_confirm.toggled.connect(self._sync_probe_controls)
        self.use_probe_button = QPushButton('将观察到的条件带入编辑窗口…')
        self.use_probe_button.clicked.connect(self.use_probe)
        self.form.addRow('', self.probe_confirm)
        self.form.addRow('', self.use_probe_button)
        self.cancel_probe_button = self.buttons.addButton('取消试运行', QDialogButtonBox.ButtonRole.ActionRole)
        self.cancel_probe_button.clicked.connect(self.cancel_probe)
        self.python_edit.textChanged.connect(self._reset_probe)
        self.explain_button = QPushButton('AI 解读本次结果…')
        self.explain_button.setEnabled(False)
        self.explain_button.clicked.connect(self.explain_result)
        self.form.addRow('', self.explain_button)
        self.save_button.setText('准备验收计划')
        self.save_button.clicked.connect(self.advance)
        self.read_button.setText('读取项目版本与测试')
        self.status_label.setText('先选择项目、问题和环境，准备后预览要发送的有限代码。')
        self._sync_probe_controls()

    @property
    def work_root(self):
        return Path(self.store.settings['cases']).expanduser().parent

    def _finished(self):
        # A complete response may have been saved before materialization failed.
        # Keep the useful failure text while exposing recovery in this window.
        if self.directory and self.state and self.state.get('status') != 'planned' and not self._closing:
            self.recover_button.setEnabled(Path(self.directory, 'response-record.json').is_file())
        super()._finished()
        self._probing = False
        self._sync_probe_controls()

    def _selected_check(self):
        current = self.check_tree.currentItem()
        return current.data(0, Qt.ItemDataRole.UserRole) if current else None

    def _sync_probe_controls(self):
        ready = bool(not self.task and not self._closing and self.state and self.state.get('status') == 'planned')
        check = self._selected_check()
        observed = bool(ready and check and check['role'] == 'repair' and self.probe_data
                        and self.probe_data.get('status') == 'failure_observed' and self.probe_data.get('suggestion'))
        self.probe_button.setEnabled(ready and bool(check))
        self.open_probe_button.setEnabled(ready)
        self.probe_confirm.setEnabled(observed)
        self.use_probe_button.setEnabled(observed and self.probe_confirm.isChecked())
        self.cancel_probe_button.setEnabled(bool(self.task and self._probing and not self._closing))

    def _reset_probe(self, *args):
        self.probe_data = None
        if hasattr(self, 'probe_output'):
            self.probe_output.clear()
            self.probe_confirm.setChecked(False)
            self._sync_probe_controls()

    def probe_selected_check(self):
        check = self._selected_check()
        if self.task or not self.directory or not check:
            return
        self._reset_probe()
        directory, identity = self.directory, check['id']
        python = self.python_edit.text().strip() or sys.executable
        output = Path(directory).parent / 'probes'
        self._probing = True
        self._start(lambda control: probe_suite_check(directory, identity, output, python_executable=python,
                    timeout=self.settings.get('timeout', 15), control=control), self._probe_shown,
                    '正在独立副本中试运行所选检查的旧版…', saving=True, controlled=True)
        self.task.cancelled.connect(self._probe_cancelled)
        self._sync_probe_controls()

    def cancel_probe(self):
        if self.task and self._probing:
            self.task.control.cancel()
            self.cancel_probe_button.setEnabled(False)
            self.status_label.setText('正在取消旧版试运行并清理工作进程…')

    def _probe_cancelled(self):
        if not self._closing:
            self.status_label.setText('旧版试运行已取消，没有可采用的故障条件。')

    def _probe_shown(self, record):
        self.probe_data = record
        self.probe_confirm.setChecked(False)
        before = record.get('before') or {}
        lines = [record.get('explanation', ''), '旧版状态：' + before.get('kind', '未完成'),
                 '测试标识：' + str(before.get('test_id') or '未返回'),
                 'Python：' + str(record.get('runtime', {}).get('executable', '未记录')),
                 '材料位置：' + record['probe_dir'], '']
        if record.get('input_integrity_notice'):
            lines.extend([record['input_integrity_notice'], ''])
        if before.get('failure'):
            lines.extend([before['failure']['type'], before['failure']['message'], ''])
        lines.extend(['旧版日志节选', before.get('log_excerpt', ''), '',
                      '仅观察旧版，尚未进行正式两版验收。'])
        self.probe_output.setPlainText('\n'.join(lines))
        self.status_label.setText(record.get('explanation', '已读取旧版观察。'))
        self._sync_probe_controls()
        self.scroll_area.ensureWidgetVisible(self.probe_output)

    def open_probe(self, path=None):
        if self.task or not self.directory:
            return
        if path is None:
            path = QFileDialog.getExistingDirectory(self, '选择旧版观察目录', str(Path(self.directory).parent / 'probes'))
        if not path:
            return
        try:
            record = read_suite_probe(self.directory, path)
            for index in range(self.check_tree.topLevelItemCount()):
                item = self.check_tree.topLevelItem(index)
                if item.data(0, Qt.ItemDataRole.UserRole)['id'] == record['check_id']:
                    self.check_tree.setCurrentItem(item)
                    break
            self._probe_shown(record)
        except (OSError, ValueError) as exc:
            self._reset_probe()
            self.status_label.setText('无法读取旧版观察：' + str(exc))

    def use_probe(self):
        if self.task or not self.probe_data or not self.probe_confirm.isChecked():
            return
        try:
            suggestion = checked_probe_suggestion(self.directory, self.probe_data['probe_dir'],
                                                   python_executable=self.python_edit.text().strip() or sys.executable,
                                                   expected_probe_sha256=self.probe_data['record_sha256'])
            editor = self.last_review = SuiteReviewDialog(self.directory, parent=self)
            index = next(i for i, check in enumerate(editor.plan['checks']) if check['id'] == suggestion['check_id'])
            editor.check_list.setCurrentRow(index)
            editor.failure_type_edit.setText(suggestion['expected_failure']['type'])
            editor.failure_message_edit.setText(suggestion['expected_failure']['message_contains'])
            editor.status_label.setText('已预填实际观察到的条件，请审阅稳定消息片段和测试，再另存独立版本。')
            if editor.exec() == QDialog.DialogCode.Accepted:
                self._planned(editor.result_data)
        except (OSError, ValueError) as exc:
            self._reset_probe()
            self.status_label.setText('未采用旧版观察：' + str(exc))

    def _discard_prepared(self):
        self._generation += 1
        self.pending = None
        self.directory = self.state = None
        self.last_summary = None
        if hasattr(self, "explain_button"):
            self.explain_button.setEnabled(False)
        if hasattr(self, 'check_tree'):
            self.check_tree.clear()
            self.review.clear()
            self.save_button.setText('准备验收计划')
            self.recover_button.setEnabled(False)
            self.edit_button.setEnabled(False)
            self._reset_probe()

    def _clear_choices(self):
        if hasattr(self, 'directory'):
            self._discard_prepared()

    def _show_choices(self, data):
        self.status_label.setText(f"已找到 {len(data['tests'])} 个已有测试，可准备AI检查计划。")

    def _workspace_changed(self, checked):
        self.after_combo.setEnabled(not checked)
        self._invalidate_selection()
        if self._root:
            if checked:
                self.before_combo.setCurrentIndex(0)
            self.load_selection()

    def _model_selected(self, config):
        previous = self.store.settings.get('model_config', '')
        self.store.settings['model_config'] = config
        try:
            self.store.save()
        except OSError:
            self.store.settings['model_config'] = previous
            self.model_edit.setText(previous)
            self.status_label.setText('模型选择未保存，已保留先前连接。')
            return
        self.status_label.setText('已切换模型；发送前预览将使用当前选择。')

    def configure_model(self):
        if self.task:
            return
        dialog = ModelConnectionDialog(self.model_edit.text().strip() or None, self.work_root / 'models', self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.model_edit.setText(dialog.result_data['config'])
            self.store.settings['model_config'] = dialog.result_data['config']
            self.store.save()

    def inspect_environment(self):
        if self.task:
            return
        project = str(Path(self.directory) / 'inputs/after') if self.directory else self._root or self.repository_edit.text().strip()
        if not project:
            self.status_label.setText('请先选择项目')
            return
        dialog = ProjectEnvironmentDialog(project, self.python_edit.text().strip(), self.work_root / 'environments', self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.python_edit.setText(dialog.result_data['python'])

    def advance(self):
        if self.task:
            return
        try:
            if self.state and self.state.get('status') == 'planned':
                self.run_plan()
            elif self.directory:
                config = self.model_edit.text().strip()
                preview = _preview(self, self.directory, config)
                if preview:
                    self.generate_from_config(config, preview)
            else:
                root, before, after, python = self._inputs()
                issue = self.issue_edit.toPlainText().strip()
                self._start(lambda: prepare_suite(root, before, after, issue, self.work_root / 'suites', python_executable=python),
                            self._prepared, '正在固定输入和准备模型上下文…', saving=True)
        except (OSError, ValueError) as exc:
            self.status_label.setText('未完成 ' + str(exc))

    def _prepared(self, state):
        self.last_summary = None
        self.explain_button.setEnabled(False)
        self.state = state
        self.directory = state['directory']
        self.check_tree.clear()
        self.review.clear()
        self._reset_probe()
        self.edit_button.setEnabled(False)
        recoverable = state.get('status') != 'planned' and Path(self.directory, 'response-record.json').is_file()
        self.recover_button.setEnabled(recoverable)
        self.save_button.setText('预览并生成检查')
        self.status_label.setText(('已有保存的响应，可尝试离线恢复；也可以重新预览生成或导入模型响应。' if recoverable
                                  else '输入已固定，可以预览并生成检查计划，也可导入模型响应。')
                                  + ('\n' + state['input_integrity_notice'] if state.get('input_integrity_notice') else ''))

    def generate_from_config(self, config, preview):
        directory = self.directory
        def action():
            check_preview(directory, config, preview)
            return generate_suite(directory, config_file=config)
        self._start(action, self._planned, '正在请求模型生成检查计划…', saving=True)

    def import_response(self, path=None):
        if self.task or not self.directory:
            self.status_label.setText('请先准备验收计划')
            return
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, '选择模型JSON响应', '', 'JSON (*.json)')
        if path:
            directory = self.directory
            self._start(lambda: generate_suite(directory, response_file=path), self._planned, '正在校验检查计划…', saving=True)

    def _planned(self, state):
        self.state = state
        self.directory = state['directory']
        self.last_summary = self.last_batch = None
        self.explain_button.setEnabled(False)
        self.recover_button.setEnabled(False)
        self.edit_button.setEnabled(True)
        self._reset_probe()
        self.check_tree.clear()
        for check in state['plan']['checks']:
            item = QTreeWidgetItem([check['title'], '目标修复' if check['role'] == 'repair' else '回归保护'])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked)
            item.setData(0, Qt.ItemDataRole.UserRole, check)
            self.check_tree.addTopLevelItem(item)
        if self.check_tree.topLevelItemCount():
            self.check_tree.setCurrentItem(self.check_tree.topLevelItem(0))
        self.save_button.setText('开始项目验收')
        self.status_label.setText('检查计划已准备，请审阅用途、故障条件、代码和假设；可另存修订，再开始实际执行。'
                                  + ('\n恢复前的未完成文件保存在 ' + state['recovery_artifacts'] if state.get('recovery_artifacts') else '')
                                  + ('\n' + state['input_integrity_notice'] if state.get('input_integrity_notice') else ''))
        self.scroll_area.ensureWidgetVisible(self.check_tree)

    def show_check(self, current, previous=None):
        self._reset_probe()
        if not current or not self.directory:
            return
        check = current.data(0, Qt.ItemDataRole.UserRole)
        code = Path(self.directory, 'tests', check['id'] + '.py').read_bytes().decode('utf-8', errors='replace')
        role = '目标修复' if check['role'] == 'repair' else '回归保护'
        lines = [f'检查用途：{role}', '测试目标：' + (check['test_target'] or '独立测试代码')]
        if check['expected_failure']:
            lines.extend(['旧版异常类型：' + check['expected_failure']['type'],
                          '旧版消息包含：' + check['expected_failure']['message_contains']])
        else:
            lines.append('比较修复前后表现，不要求预声明故障。')
        lines.extend(['', check['purpose'], check['expected_behavior'], *check['assumptions'], '', code])
        self.review.setPlainText('\n'.join(lines))

    def recover_response(self):
        if self.task or not self.directory:
            return
        directory = self.directory
        self._start(lambda: recover_suite(directory), self._planned, '正在校验已保存响应并离线恢复…', saving=True)

    def edit_plan(self):
        if self.task or not self.directory or not self.state or self.state.get('status') != 'planned':
            return
        try:
            # Keep the Python owner alive while Qt processes deferred deletion
            # of its completed Task, including the next nested batch event loop.
            dialog = self.last_review = SuiteReviewDialog(self.directory, parent=self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._planned(dialog.result_data)
        except (OSError, ValueError) as exc:
            self.status_label.setText('无法编辑计划 ' + str(exc))

    def run_plan(self):
        state = read_suite(self.directory)
        selected = [self.check_tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) for i in range(self.check_tree.topLevelItemCount())
                    if self.check_tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked]
        if not any(check['role'] == 'repair' for check in selected):
            raise ValueError('请至少选择一个目标修复检查')
        paths = [Path(self.directory) / check['case'] for check in selected]
        dialog = self.last_batch = BatchDialog(self.store, paths, python_executable=self.python_edit.text().strip(),
                                               timeout=self.settings.get('timeout', 15), parent=self,
                                               suite_directory=self.directory, suite_ids=[check['id'] for check in selected])
        dialog.setWindowTitle(state['plan']['title'])
        dialog.start()
        dialog.exec()
        if dialog.result_data:
            self.last_summary = dialog.result_data['summary_dir']
            self.explain_button.setEnabled(True)
            self.status_label.setText(dialog.status_label.text())

    def explain_result(self):
        if self.last_summary and not self.task:
            ExplanationDialog(self.last_summary, self.model_edit.text().strip(), self).exec()

    def load_plan(self, path=None):
        if self.task:
            return
        if path is None:
            path = QFileDialog.getExistingDirectory(self, '选择验收计划目录', str(self.work_root / 'suites'))
        if not path:
            return
        try:
            _, state = _checked_state(path)
            if state.get('status') == 'planned':
                state = read_suite(path)
            widgets = (self.repository_edit, self.before_combo, self.after_combo, self.workspace_check, self.issue_edit)
            for widget in widgets:
                widget.blockSignals(True)
            self.repository_edit.setText(state['repository'])
            self.before_combo.setEditText(state['before_commit'])
            self.after_combo.setEditText(state['after_commit'])
            self.workspace_check.setChecked(state['after_commit'] == WORKTREE)
            self.after_combo.setEnabled(state['after_commit'] != WORKTREE)
            self.issue_edit.setPlainText(state['issue'])
            chosen_python = state['runtime']['executable']
            self.python_edit.setText('' if chosen_python == sys.executable else chosen_python)
            for widget in widgets:
                widget.blockSignals(False)
            self._root = state['repository']
            self.selected_commits = (state['before_commit'], state['after_commit'])
            self._prepared(state)
            if state.get('status') == 'planned':
                self._planned(state)
        except (OSError, ValueError, KeyError) as exc:
            self.status_label.setText('无法读取计划 ' + str(exc))
