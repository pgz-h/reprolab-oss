"""Offline creation dialogs sharing fixed Git selections and safe background reads."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from .desktop_tasks import Task
from .draft_context import MAX_FILES, _tree
from .drafts import prepare_draft
from .git_source import _git, resolve_commit
from .recipes import create_recipe, list_project_tests, repository_root
from .workspace import WORKTREE, create_workspace_case
from .discovery import list_workspace_tests, collect_project_tests
from .failure_probe import probe_failure, save_probe_case
from .test_selection import recommend_tests


def _commits(repository):
    try:
        root = repository_root(repository)
    except (ValueError, OSError) as exc:
        raise ValueError('无法读取 Git 仓库，请确认目录中已有 Git 提交。' + str(exc)) from None
    rows = _git(root, 'log', '-60', '--date=short', '--format=%H%x09%ad%x09%s').decode('utf-8', 'replace').splitlines()
    commits = [row.split('\t', 2) for row in rows]
    if not commits:
        raise ValueError('仓库还没有提交，请先保存修复前后的 Git 提交。')
    return str(root), commits


def _selection(repository, before, after, kind):
    root = repository_root(repository)
    before = resolve_commit(root, before)
    if kind == "case" and after == WORKTREE:
        data = list_workspace_tests(root)
        data.update(recommend_tests(root, before, WORKTREE, data["tests"]))
        return (str(root), before, WORKTREE, data)
    after = resolve_commit(root, after)
    if before == after:
        raise ValueError('修复前和修复后必须选择两个不同的提交。')
    if kind == 'case':
        data = list_project_tests(root, after)
        data.update(recommend_tests(root, before, after, data["tests"]))
    else:
        data = sorted(set(_tree(root, before)) | set(_tree(root, after)))
    return (str(root), before, after, data)


def _outside(path, repository, label):
    if not str(path).strip():
        raise ValueError(f'请选择{label}保存位置。')
    path = Path(path).expanduser().resolve()
    if path.is_relative_to(Path(repository).resolve()):
        raise ValueError(f'{label}必须保存在被测 Git 仓库之外。')
    return path


class _CreationDialog(QDialog):
    """One active Task; replacement reads discard stale replies by generation."""
    kind = ''

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = dict(settings)
        self.result_data = {}
        self.task = None
        self.pending = None
        self.selected_commits = None
        self._root = None
        self._generation = 0
        self._closing = False
        self._saving = False
        self.setWindowTitle('新建案例' if self.kind == 'case' else '新建离线草稿')
        self.resize(860, 760)
        self.main_layout = QVBoxLayout(self)
        self.content = QWidget()
        self.form = QFormLayout(self.content)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.form.setHorizontalSpacing(14)
        self.form.setVerticalSpacing(10)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setWidget(self.content)
        self.main_layout.addWidget(self.scroll_area, 1)
        self.repository_edit = QLineEdit()
        self.repository_edit.setPlaceholderText('选择本地 Git 仓库；只读取已提交版本')
        self.repository_button = QPushButton('选择仓库…')
        self.refresh_button = QPushButton('读取提交')
        self.form.addRow('代码仓库', self._row(self.repository_edit, self.repository_button, self.refresh_button))
        self.before_combo, self.after_combo = QComboBox(), QComboBox()
        for combo in (self.before_combo, self.after_combo):
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            combo.currentTextChanged.connect(self._invalidate_selection)
        self.form.addRow('修复前', self.before_combo)
        self.form.addRow('修复后', self.after_combo)
        self.read_button = QPushButton('读取所选版本的测试' if self.kind == 'case' else '读取可选上下文')
        self.form.addRow('', self.read_button)
        self.version_label = QLabel('选择仓库后读取最近 60 个提交；也可输入分支、标签或完整提交。')
        self.version_label.setWordWrap(True)
        self.version_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        self.form.addRow('', self.version_label)
        self.python_edit = QLineEdit(str(settings.get('python') or ''))
        self.python_edit.setPlaceholderText('留空使用应用内置 Python；有项目依赖时选择项目环境')
        self.python_button = QPushButton('选择 Python…')
        self.form.addRow('Python 环境', self._row(self.python_edit, self.python_button))
        self.python_button.clicked.connect(self._choose_python)
        self.repository_button.clicked.connect(self._choose_repository)
        self.refresh_button.clicked.connect(self.refresh_repository)
        self.read_button.clicked.connect(self.load_selection)
        self.repository_edit.textChanged.connect(self._repository_changed)
        self.status_label = QLabel('只读取本地已提交代码，不会执行测试或发送到模型。')
        self.status_label.setWordWrap(True)
        self.main_layout.addWidget(self.status_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.save_button = self.buttons.addButton('保存案例' if self.kind == 'case' else '准备离线草稿', QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.buttons.rejected.connect(self.reject)
        self.main_layout.addWidget(self.buttons)

    @staticmethod
    def _row(*widgets):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in widgets:
            layout.addWidget(widget)
        return row

    def _choose_repository(self):
        path = QFileDialog.getExistingDirectory(self, '选择本地 Git 仓库', self.repository_edit.text())
        if path:
            self.repository_edit.setText(path)
            self.refresh_repository()

    def _choose_python(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择项目 Python 解释器', self.python_edit.text(), options=QFileDialog.Option.DontResolveSymlinks)
        if path:
            # Keep the exact symlink path: resolving it loses virtualenv identity.
            self.python_edit.setText(path)

    def _repository_changed(self):
        self._invalidate_selection()
        for combo in (self.before_combo, self.after_combo):
            combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(False)
        self._root = None

    def _invalidate_selection(self):
        self._generation += 1
        self.pending = None
        self.selected_commits = None
        self._clear_choices()
        self.version_label.setText('版本已改变，请读取所选版本，避免沿用旧的测试或上下文。')
        self.version_label.setToolTip('')

    def _clear_choices(self):
        pass

    def _start(self, action, callback, message, *, saving=False, controlled=False):
        if self._closing:
            return
        generation = self._generation
        if self.task:
            if not self._saving:
                self.pending = (action, callback, message, saving, controlled)
            return
        self._saving = saving
        self.content.setEnabled(not saving)
        self.save_button.setEnabled(False)
        self.status_label.setText(message)
        task = self.task = Task(action if controlled else lambda control: action(), self)
        def succeeded(result):
            if not self._closing and generation == self._generation:
                callback(result)
        def failed(error):
            if not self._closing and generation == self._generation:
                self.status_label.setText('未完成：请检查仓库、版本和输入。' + error)
        task.succeeded.connect(succeeded, Qt.ConnectionType.QueuedConnection)
        task.failed.connect(failed, Qt.ConnectionType.QueuedConnection)
        task.finished.connect(self._finished)
        task.start()

    def _finished(self):
        task = self.task
        self.task = None
        task.deleteLater()
        self._saving = False
        self.content.setEnabled(True)
        self.save_button.setEnabled(True)
        if self._closing:
            self.pending = None
            super().reject()
        elif self.result_data:
            self.accept()
        elif self.pending:
            action, callback, message, saving, controlled = self.pending
            self.pending = None
            self._start(action, callback, message, saving=saving, controlled=controlled)

    def refresh_repository(self):
        repository = self.repository_edit.text().strip()
        self._repository_changed()
        if not repository:
            self.status_label.setText('请先选择本地 Git 仓库。')
            return
        self._start(lambda: _commits(repository), self._show_commits, '正在读取仓库提交…')

    def _show_commits(self, result):
        self._root, commits = result
        working = self.kind == 'case' and getattr(self, 'workspace_check', None) and self.workspace_check.isChecked()
        for combo, index in ((self.before_combo, 0 if working else min(1, len(commits)-1)), (self.after_combo, 0)):
            combo.blockSignals(True)
            combo.clear()
            for sha, date, subject in commits:
                combo.addItem(f'{sha[:12]} · {date} · {subject}', sha)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)
        self.load_selection()

    @staticmethod
    def _ref(combo):
        if combo.currentText() == combo.itemText(combo.currentIndex()):
            return combo.currentData()
        return combo.currentText().strip()

    def load_selection(self):
        repository = self.repository_edit.text().strip()
        before, after = self._ref(self.before_combo), self._ref(self.after_combo)
        if self.kind == 'case' and self.workspace_check.isChecked():
            after = WORKTREE
        self._invalidate_selection()
        if not repository or not before or not after:
            self.status_label.setText('请先读取仓库提交，并选择修复前后的版本。')
            return
        self._start(lambda: _selection(repository, before, after, self.kind), self._show_selection, '正在静态读取所选提交…')

    def _show_selection(self, result):
        self._root, before, after, data = result
        self.selected_commits = (before, after)
        self.version_label.setText(f'修复前 {before[:12]} → 保存时冻结当前工作区' if after == WORKTREE else f'已固定版本：修复前 {before[:12]} → 修复后 {after[:12]}')
        self.version_label.setToolTip(f'修复前完整提交：{before}\n修复后完整提交：{after}')
        self._show_choices(data)

    def _inputs(self):
        if self.task or not self.selected_commits:
            raise ValueError('请先读取所选版本，确认测试或上下文后再保存。')
        return self._root, *self.selected_commits, self.python_edit.text().strip() or None

    def _saved(self, data):
        self.result_data = data

    def reject(self):
        if self.task:
            self._closing = True
            self.task.control.cancel()
            self.pending = None
            self.content.setEnabled(False)
            self.buttons.setEnabled(False)
            self.status_label.setText('正在结束当前本地操作，完成后关闭…')
        else:
            super().reject()

    def closeEvent(self, event):
        if self.task:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)


class CaseWizard(_CreationDialog):
    """Use save_case(path) for automation, or the native Save button chooser."""
    kind = 'case'

    def __init__(self, settings, parent=None):
        super().__init__(settings, parent)
        self._actual_collection = False
        self._candidate_probe = None
        self._accepted_probe = None
        self.workspace_check = QCheckBox('验收当前未提交的修改（保存时冻结快照）')
        self.form.insertRow(3, '', self.workspace_check)
        self.workspace_check.toggled.connect(self._workspace_changed)
        self.title_edit = QLineEdit()
        self.form.addRow('案例标题', self.title_edit)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('搜索测试名称或文件路径')
        self.form.addRow('查找测试', self.search_edit)
        self.tests_list = QListWidget()
        self.tests_list.setMinimumHeight(150)
        self.tests_list.setMaximumHeight(220)
        self.form.addRow('修复后版本的测试', self.tests_list)
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText('tests/test_x.py::test_name[具体参数标识]')
        self.form.addRow('精确测试目标', self.target_edit)
        self.target_edit.textChanged.connect(self._probe_target_changed)
        self.collect_button = QPushButton('列出实际测试与参数…')
        self.collect_button.setToolTip('会在独立副本中加载可信项目及conftest，不运行测试主体或fixture准备。')
        self.form.addRow('', self.collect_button)
        self.collect_button.clicked.connect(self.collect_actual)
        self.python_edit.textChanged.connect(self._collection_environment_changed)
        self.failure_type_edit = QLineEdit('AssertionError')
        self.failure_message_edit = QLineEdit()
        self.failure_message_edit.setPlaceholderText('例如：expected 42；必须是旧版故障的稳定消息片段')
        self.probe_button = QPushButton('试运行旧版，识别故障')
        self.adopt_button = QPushButton('确认并采用此故障')
        self.adopt_button.setEnabled(False)
        self.form.addRow('', self._row(self.probe_button, self.adopt_button))
        self.probe_label = QLabel('可以先试运行旧版，查看实际失败原因后确认。')
        self.probe_label.setWordWrap(True)
        self.probe_label.setTextFormat(Qt.TextFormat.PlainText)
        self.probe_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        self.form.addRow('', self.probe_label)
        self.probe_button.clicked.connect(self.probe_old_version)
        self.adopt_button.clicked.connect(self.adopt_failure)
        self.manual_failure_check = QCheckBox('手工填写或调整故障条件')
        self.manual_failure_panel = QWidget()
        manual_form = QFormLayout(self.manual_failure_panel)
        manual_form.setContentsMargins(0, 0, 0, 0)
        manual_form.addRow('预期旧版异常', self.failure_type_edit)
        manual_form.addRow('预期消息片段', self.failure_message_edit)
        self.form.addRow('', self.manual_failure_check)
        self.form.addRow('', self.manual_failure_panel)
        self.manual_failure_panel.hide()
        self.manual_failure_check.toggled.connect(self.manual_failure_panel.setVisible)

        explanation = QLabel('旧版必须出现这里声明的异常和消息，新版同一个测试必须通过。\n默认列表为静态读取；点击实际收集可选择具体参数，会加载可信项目和conftest。最终只能选中一个测试。\n保存只创建案例，不会自动运行。')
        explanation.setWordWrap(True)
        explanation.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        self.form.addRow('', explanation)
        self.tests_list.currentTextChanged.connect(self.target_edit.setText)
        self.search_edit.textChanged.connect(self._filter_tests)
        self.save_button.clicked.connect(lambda: self.save_case())

    def _reset_probe(self):
        accepted = self._accepted_probe
        self._candidate_probe = None
        self._accepted_probe = None
        if hasattr(self, 'adopt_button'):
            self.adopt_button.setEnabled(False)
            self.probe_label.setText('可以先试运行旧版，查看实际失败原因后确认。')
            if accepted:
                self.failure_message_edit.clear()

    def _probe_target_changed(self):
        self._generation += 1
        self.pending = None
        self._reset_probe()

    def probe_old_version(self):
        try:
            root, before, after, python = self._inputs()
            target = self.target_edit.text().strip()
            if not target:
                raise ValueError('请先选择一个具体测试。')
            self._reset_probe()
            output = Path(self.settings.get('cases') or Path.home() / 'Documents/ReproLab/cases').parent / 'probes'
            self._start(lambda control: probe_failure(root, before, after, target, output,
                                                       python_executable=python, timeout=self.settings.get('timeout', 15), control=control),
                        self._show_probe, '正在固定代码并试运行旧版，请等待实际结果…', controlled=True)
        except (ValueError, OSError) as exc:
            self.status_label.setText('无法试运行 ' + str(exc))

    def _show_probe(self, result):
        self._candidate_probe = result
        lines = [result['explanation']]
        failure = (result.get('before') or {}).get('failure')
        if failure:
            lines.extend([failure['type'], failure['message'][:1800]])
        self.probe_label.setText('\n'.join(lines))
        self.probe_label.setToolTip(str(Path(result['probe_dir']) / 'probe.json'))
        self.adopt_button.setEnabled(result.get('status') == 'failure_observed')
        self.status_label.setText('旧版试运行结束，请查看实际故障及下一步。')

    def adopt_failure(self):
        result = self._candidate_probe
        if self.task or not result or result.get('status') != 'failure_observed':
            return
        suggestion = result['suggestion']
        self.failure_type_edit.setText(suggestion['type'])
        self.failure_message_edit.setText(suggestion['message_contains'])
        self._accepted_probe = result
        self.status_label.setText('已采用观察到的故障。可展开调整消息片段，保存时使用本次试运行的固定代码。')

    def _workspace_changed(self, checked):
        self.after_combo.setEnabled(not checked)
        self._invalidate_selection()
        if self._root:
            if checked and self.before_combo.count():
                self.before_combo.setCurrentIndex(0)
            self.load_selection()

    def _collection_environment_changed(self):
        self._reset_probe()
        if self._actual_collection:
            self.tests_list.clear()
            self.target_edit.clear()
            self._actual_collection = False
        self._generation += 1
        self.pending = None
        self.status_label.setText('环境已改变；可重新列出实际测试与参数。保存后使用所选环境验收。')

    def collect_actual(self):
        try:
            root, before, after, python = self._inputs()
            target = self.target_edit.text().strip() or None
            self.tests_list.clear()
            self.target_edit.clear()
            self._actual_collection = True
            self._start(lambda: collect_project_tests(root, after, target=target, python_executable=python),
                        self._show_collected, '正在独立副本中收集；会加载可信项目，不运行测试主体…')
        except (ValueError, OSError) as exc:
            self.status_label.setText('无法收集：' + str(exc))

    def _show_collected(self, result):
        self._actual_collection = True
        self.tests_list.clear()
        self.target_edit.clear()
        self.tests_list.addItems(result['tests'])
        self._filter_tests()
        extra = ' 列表仅显示前5000项，请缩小测试目标。' if result.get('truncated') else ''
        self.status_label.setText(result['explanation'] + f" 实际数量：{result.get('count')}。" + extra)
        self.status_label.setToolTip(result.get('log_excerpt', ''))

    def _clear_choices(self):
        if hasattr(self, 'tests_list'):
            self._reset_probe()
            self._actual_collection = False
            self.tests_list.clear()
            self.target_edit.clear()

    def _filter_tests(self):
        query = self.search_edit.text().casefold()
        for i in range(self.tests_list.count()):
            item = self.tests_list.item(i)
            item.setHidden(query not in item.text().casefold())

    def _show_choices(self, data):
        recommendations = data.get('recommendations', {})
        for target in data['tests']:
            item = QListWidgetItem(target)
            item.setToolTip(recommendations.get(target, '可选择的项目测试'))
            self.tests_list.addItem(item)
        self._filter_tests()
        notes = '\n'.join(data['notes'])
        self.status_label.setText(f"找到 {len(data['tests'])} 个测试，其中 {len(recommendations)} 个与本次改动有关，已优先排列。" + ('\n' + notes if notes else ''))
        if data['tests'] and recommendations:
            self.tests_list.setCurrentRow(0)

    def save_case(self, path=None):
        try:
            root, before, after, python = self._inputs()
            target = self.target_edit.text().strip()
            failure_type = self.failure_type_edit.text().strip()
            message = self.failure_message_edit.text().strip()
            title = self.title_edit.text().strip() or None
            accepted_probe = self._accepted_probe
            if not target or not failure_type or not message:
                raise ValueError('请填写精确测试目标、预期异常类型和非空消息片段。')
            if path is None:
                initial = str(Path(self.settings.get('cases') or Path.home()) / 'case.json')
                path, _ = QFileDialog.getSaveFileName(self, '保存新案例（不覆盖已有文件）', initial, '案例 JSON (*.json)', options=QFileDialog.Option.DontConfirmOverwrite)
                if not path:
                    return
            # Filesystem validation belongs to the worker as well.
            def save():
                original = Path(path).expanduser()
                if original.exists() or original.is_symlink():
                    raise ValueError(f'案例文件已经存在，未覆盖：{original}')
                destination = _outside(path, root, '案例')
                if accepted_probe:
                    return save_probe_case(accepted_probe['probe_dir'], destination, confirmed=True,
                                           failure_type=failure_type, failure_message=message, title=title)
                if after == WORKTREE:
                    return create_workspace_case(destination, root, before, target, failure_type=failure_type,
                                                 failure_message=message, title=title, python_executable=python)
                return create_recipe(destination, root, before, after, target, failure_type=failure_type,
                                     failure_message=message, title=title, python_executable=python)
            self._start(save, self._saved, '正在冻结并保存案例，不执行测试…', saving=True)
        except (ValueError, OSError) as exc:
            self.status_label.setText('未保存：' + str(exc))


class DraftWizard(_CreationDialog):
    """prepare() creates a bounded local prompt; never calls a model."""
    kind = 'draft'

    def __init__(self, settings, parent=None):
        super().__init__(settings, parent)
        self.issue_edit = QPlainTextEdit()
        self.issue_edit.setPlaceholderText('描述遇到的问题、输入和期望结果（最多 16 KiB）')
        self.issue_edit.setMinimumHeight(100)
        self.issue_edit.setMaximumHeight(120)
        self.form.addRow('问题描述', self.issue_edit)
        self.auto_context_check = QCheckBox('自动选取相关/变更 Python 文件和项目说明')
        self.auto_context_check.setChecked(True)
        self.form.addRow('代码上下文', self.auto_context_check)
        self.context_list = QListWidget()
        self.context_list.setMinimumHeight(150)
        self.context_list.setMaximumHeight(220)
        self.context_list.setEnabled(False)
        self.form.addRow('或手工勾选文件', self.context_list)
        self.context_count_label = QLabel('已选 0 / 12；单版本每文件最多 24 KiB，总计最多 96 KiB。')
        self.context_count_label.setWordWrap(True)
        self.context_count_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        self.form.addRow('', self.context_count_label)
        self.output_edit = QLineEdit(str(settings.get('drafts') or ''))
        self.output_button = QPushButton('选择目录…')
        self.form.addRow('草稿保存目录', self._row(self.output_edit, self.output_button))
        notice = QLabel('只准备本地问题、有限代码上下文和提示词。完成后可在主窗口预览实际提示词，再自行决定是否发送。不会生成或运行测试。')
        notice.setWordWrap(True)
        notice.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        self.form.addRow('', notice)
        self.auto_context_check.toggled.connect(lambda checked: self.context_list.setEnabled(not checked))
        self.context_list.itemChanged.connect(self._count_context)
        self.output_button.clicked.connect(self._choose_output)
        self.save_button.clicked.connect(self.prepare)

    def _choose_output(self):
        path = QFileDialog.getExistingDirectory(self, '选择草稿保存目录', self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def _clear_choices(self):
        if hasattr(self, 'context_list'):
            self.context_list.clear()
            self._count_context()

    def _count_context(self):
        count = len(self._checked_paths())
        self.context_count_label.setText(f'已选 {count} / {MAX_FILES}；单版本每文件最多 24 KiB，总计最多 96 KiB。')

    def _checked_paths(self):
        return [self.context_list.item(i).text() for i in range(self.context_list.count())
                if self.context_list.item(i).checkState() == Qt.CheckState.Checked]

    def _show_choices(self, paths):
        for path in paths:
            item = QListWidgetItem(path)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.context_list.addItem(item)
        self.status_label.setText(f'两个已提交版本中有 {len(paths)} 个支持的上下文文件；排除敏感目录和不支持的文件类型。')

    def prepare(self):
        try:
            root, before, after, python = self._inputs()
            issue = self.issue_edit.toPlainText().strip()
            output = self.output_edit.text().strip()
            paths = None if self.auto_context_check.isChecked() else self._checked_paths()
            if not issue:
                raise ValueError('请填写问题描述。')
            if paths is not None and not 1 <= len(paths) <= MAX_FILES:
                raise ValueError(f'请手工选择 1 至 {MAX_FILES} 个上下文文件，或启用自动选择。')
            def save():
                destination = _outside(output, root, '草稿')
                return prepare_draft(root, before, after, issue=issue, paths=paths,
                                     drafts_root=destination, python_executable=python)
            self._start(save, self._saved, '正在准备本地上下文和提示词，不调用模型…', saving=True)
        except (ValueError, OSError) as exc:
            self.status_label.setText('未准备：' + str(exc))
