"""Project regression workbench built around persisted, immutable project inputs.

Qt owns every worker and dialog.  A worker is retained until ``finished`` even
when its result has already arrived; closing the host only requests cancellation.
"""
from collections import Counter
import csv
import difflib
import hashlib
import io
from itertools import islice
import json
from pathlib import Path, PurePosixPath
import stat
import sys
import tokenize
import uuid

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLayout, QLineEdit,
    QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSplitter,
    QScrollArea, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .desktop_tasks import Task
from .runner import RunCancelled
from .project_sessions import (
    check_project_report, collect_project, list_projects, pack_project_report,
    prepare_project, prepare_project_ai, project_replay_settings, read_project, replay_project_report,
    run_project, unpack_project_report,
)
from .workspace import WORKTREE
from .persistence import atomic_write, save_json
from .desktop_store import read_json
from .project_selections import read_selection, write_selection, reconcile_selection, requested_selection, validate_selection
from .project_refresh import refresh_project, refresh_from_report, refresh_from_displayed_report
from .project_history import (
    read_report_snapshot, report_reference, resolve_report_reference, source_report_directory,
    validate_report_reference, read_recheck_progress,
)
from .desktop_selection import SelectionReviewDialog
from .project_presets import list_presets, read_preset, save_preset, delete_preset
from .project_diagnostics import entry_action, entry_guidance, group_project_failures, format_diagnostic_group
from .project_progress import progress_text, PHASE_LABELS, OBSERVATION_LABELS
from .project_collection import collection_diagnosis
from .case import parse_test_target
from .project_failure import repair_eligibility, load_repair_material
from .desktop_failure import RepairBriefDialog, ReportRepairDialog, RepairDraftPicker


_ROLE = Qt.ItemDataRole.UserRole
_STATUS = {
    'regression_passed': '两版通过', 'regression_detected': '新增失败',
    'baseline_failure': '两版均失败', 'improved': '已改善',
    'inconclusive': '受阻 / 无法判断', 'attention': '需要处理',
    'cancelled': '已取消', 'running': '运行中',
    'passed': '通过', 'failed': '失败', 'skipped': '跳过',
    'not_run': '未执行', 'collected': '发现完成',
    'collection_failed': '发现遇到错误',
}
_RUN_STATUS = {
    'regression_passed': '所选项目回归通过', 'regression_detected': '发现新增失败',
    'attention': '需要处理 · 不能判为回归通过', 'cancelled': '已取消 · 保留已完成结果',
}
_KIND = {'added': '新增', 'removed': '删除', 'modified': '修改',
         'renamed': '重命名', 'mode': '权限变化'}
_COLOR = {'regression_detected': '#b42332', 'baseline_failure': '#94602a',
          'inconclusive': '#7b5b9b', 'improved': '#5d7392',
          'regression_passed': '#687485', 'cancelled': '#6c7787'}
_MAX_SELECTED = 2000
_MAX_REPORT_BYTES = 128 * 1024 * 1024


def _label(text=''):
    widget = QLabel(text)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


class _CompactLabel(QLabel):
    """One visible line; retain the complete notice for tooltips and callers."""

    def __init__(self, text=''):
        super().__init__()
        self._full_text = ''
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(False)
        self.setMinimumWidth(0)
        self.setMinimumHeight(20)
        self.setMaximumHeight(25)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setText(text)

    def setText(self, value):
        self._full_text = str(value or '')
        self.setToolTip(self._full_text)
        self._render()

    def text(self):
        return self._full_text

    def _render(self):
        first_line = self._full_text.splitlines()[0] if self._full_text else ''
        QLabel.setText(self, self.fontMetrics().elidedText(
            first_line, Qt.TextElideMode.ElideRight, max(20, self.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()


def _button(text, callback, primary=False):
    widget = QPushButton(text)
    widget.setMinimumHeight(36)
    if primary:
        widget.setObjectName('primary')
    widget.clicked.connect(lambda: callback())
    return widget


def _row(*widgets):
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    for child in widgets:
        layout.addWidget(child)
    return widget


def _tree(headers, stretch=0):
    widget = QTreeWidget()
    widget.setHeaderLabels(headers)
    widget.setRootIsDecorated(False)
    widget.setAlternatingRowColors(True)
    widget.setUniformRowHeights(True)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.header().setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    for column in range(len(headers)):
        if column != stretch:
            widget.header().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
    return widget


def _text_view(placeholder=''):
    widget = QPlainTextEdit()
    widget.setReadOnly(True)
    widget.setPlaceholderText(placeholder)
    widget.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    return widget


def _short(value, limit=40000):
    text = str(value or '')
    return text if len(text) <= limit else text[:limit] + '\n…显示已截断；完整内容在保存材料中。'


def _format_json(value, limit=25000):
    return _short(json.dumps(value, ensure_ascii=False, indent=2, default=str), limit)


def _duration(value):
    try:
        return f'{float(value):.3f} s'
    except (ValueError, TypeError):
        return '—'


def _ref_value(combo):
    index = combo.currentIndex()
    if index >= 0 and combo.currentText() == combo.itemText(index) and combo.currentData():
        return str(combo.currentData())
    return combo.currentText().strip()


def _read_report(directory):
    return read_report_snapshot(directory)

class ProjectWorkbench(QWidget):
    """Freeze versions, select real pytest node IDs, and review paired results."""

    busyChanged = Signal(bool)
    projectOpened = Signal(str, str)
    casePrepared = Signal(str)

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.task = None
        self.state = None
        self.collection = None
        self.report = None
        self.review_progress = None
        self.ai_dialog = None
        self.environment_dialog = None
        self.failure_dialog = None
        self._ai_plan_path = None
        self._generation = 0
        self._loading_inputs = False
        self._changing_checks = False
        self._closing = False
        self._after_finished = None
        self._live_entries = {}
        self._change_by_path = {}
        self._report_items = {}
        self._last_progress = []
        self._operation = ''
        self._diagnostic_by_id = {}
        self._diagnostic_summary = None
        self._restoring_view = False
        self._view_dirty = False
        self._has_restored_selection = False
        self._presets = {}
        self.comparison_dialog = None
        self.processing_dialog = None
        self.selection_dialog = None
        self._selection_review = None
        self._pending_selection = None
        self._selection_intent = None
        self._report_reference = None
        self.view_save_timer = QTimer(self)
        self.view_save_timer.setSingleShot(True)
        self.view_save_timer.setInterval(350)
        self.view_save_timer.timeout.connect(self._save_view_state)
        self._build_ui()
        self._connect_inputs()
        self.find_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self)
        self.find_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.find_shortcut.activated.connect(self.focus_search)
        self.session_combo.currentIndexChanged.connect(self._sync_controls)
        self._sync_controls()
        QTimer.singleShot(0, self.refresh_sessions)

    @property
    def project_root(self):
        return Path(self.store.settings['cases']).expanduser().parent / 'projects'

    @property
    def report_root(self):
        return Path(self.store.settings['reports']).expanduser()

    @property
    def preset_root(self):
        return self.project_root.parent / 'selection-presets'

    @property
    def busy(self):
        if self.task is not None:
            return True
        for dialog in (self.ai_dialog, self.environment_dialog, self.failure_dialog, self.processing_dialog):
            if dialog is not None:
                for child in (dialog, *dialog.findChildren(QDialog)):
                    if getattr(child, 'task', None) is not None:
                        return True
        return False

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        layout = outer
        layout.setSpacing(10)
        self.session_combo = QComboBox()
        self.session_combo.setMinimumContentsLength(18)
        self.session_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.session_combo.addItem('已保存的项目会话', None)
        self.open_saved_button = _button('打开', self._open_catalog_selection)
        self.open_session_button = _button('打开会话目录…', self.open_session)
        self.import_button = _button('导入项目证据…', self.import_bundle)
        self.import_button.setToolTip('校验并解压项目报告 ZIP，只读取保存的证据；不会执行项目代码。')
        self.refresh_sessions_button = _button('刷新', self.refresh_sessions)
        self.new_button = _button('新建项目', self.new_project)
        self.refresh_workspace_button = _button('刷新当前工作区', self.refresh_workspace)
        self.refresh_workspace_button.setToolTip('沿用当前会话的固定基线，另存当前工作区和勾选意图；原会话与报告保留，不自动发现或运行测试。')
        catalog_row = QHBoxLayout()
        catalog_row.addWidget(self.session_combo, 1)
        catalog_row.addWidget(self.open_saved_button)
        self.project_menu_button = QPushButton('项目操作')
        self.project_menu = QMenu(self)
        self.project_menu_actions = []
        for item in (self.new_button, self.open_session_button, self.import_button,
                     self.refresh_sessions_button, self.refresh_workspace_button):
            item.setParent(self)
            item.hide()
            action = self.project_menu.addAction(item.text())
            action.triggered.connect(lambda checked=False, target=item: target.click())
            self.project_menu_actions.append((item, action))
        self.project_menu_button.setMenu(self.project_menu)
        catalog_row.addWidget(self.project_menu_button)
        layout.addLayout(catalog_row)

        self.input_toggle = _button('收起配置', self.toggle_inputs)
        self.input_toggle.setObjectName('quiet')
        self.input_toggle.setToolTip('展开或收起仓库、版本、Python 和问题描述。')
        self.session_label = _CompactLabel('选择可信项目，冻结输入后发现并多选测试。')
        self.session_summary = QWidget()
        summary_row = QHBoxLayout(self.session_summary)
        summary_row.setContentsMargins(0, 0, 0, 0)
        summary_row.addWidget(self.session_label, 1)
        summary_row.addWidget(self.input_toggle)
        layout.addWidget(self.session_summary)

        self.input_panel = QWidget()
        form = QFormLayout(self.input_panel)
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(7)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.repository_edit = QLineEdit()
        self.repository_edit.setPlaceholderText('选择 Git 项目')
        self.repository_button = _button('选择文件夹', self._choose_repository)
        self.commits_button = _button('读取提交', self.read_commits)
        form.addRow('代码仓库', _row(self.repository_edit, self.repository_button, self.commits_button))
        self.before_combo = QComboBox()
        self.after_combo = QComboBox()
        for combo in (self.before_combo, self.after_combo):
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            combo.setMinimumContentsLength(12)
            combo.lineEdit().setPlaceholderText('分支 / 标签 / 提交')
        self.before_combo.setEditText('HEAD')
        self.after_combo.setEditText('HEAD')
        self.workspace_check = QCheckBox('当前工作区')
        self.workspace_check.setChecked(True)
        self.workspace_check.setToolTip('包括已修改文件和未跟踪文件；冻结时读取一次，项目原目录不变。')
        self.after_combo.setEnabled(False)
        form.addRow('前后版本', _row(_label('改动前'), self.before_combo,
                                    _label('改动后'), self.after_combo, self.workspace_check))
        self.version_hint = _CompactLabel('默认比较最近一次提交（HEAD）与当前未提交改动；比较历史修复时，请选择对应的前后提交。')
        self.version_hint.setStyleSheet('color: #5a6b82; font-size: 12px;')
        self.version_hint.setParent(self); self.version_hint.hide()
        self.before_combo.setToolTip(self.version_hint.text())
        self.after_combo.setToolTip(self.version_hint.text())
        self.python_combo = QComboBox()
        self.python_combo.setEditable(True)
        self.python_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.python_combo.addItem('')
        self.python_edit = self.python_combo.lineEdit()
        self.python_edit.setText(str(self.store.settings.get('python') or ''))
        self.python_edit.setPlaceholderText('内置 Python')
        self.python_edit.setToolTip('测试将在所选 Python 环境中运行，请选择已安装项目依赖的环境。')
        self.python_button = _button('选择环境', self._choose_python)
        self.environment_button = _button('检查环境…', self.inspect_environment)
        self.python_reset_button = _button('内置环境', self.python_edit.clear)
        python_row = _row(self.python_combo, self.python_button, self.environment_button, self.python_reset_button)
        python_row.layout().setStretch(0, 1)
        for button in (self.python_button, self.environment_button, self.python_reset_button):
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        form.addRow('Python 环境', python_row)
        self.environment_hint = _CompactLabel('选择已经安装项目依赖的环境。读取项目时会列出 .venv、venv 和 env，不会自动运行它们。')
        self.environment_hint.setStyleSheet('color: #5a6b82; font-size: 12px;')
        self.saved_options_button = _button('使用上次环境与时限', self.use_saved_options)
        self.saved_options_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        form.addRow('', _row(self.environment_hint, self.saved_options_button))
        self.input_section = QWidget()
        self.input_section.setObjectName('card')
        input_layout = QVBoxLayout(self.input_section)
        input_layout.setContentsMargins(18, 18, 18, 16)
        input_layout.setSpacing(10)
        input_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        input_layout.addWidget(self.input_panel)
        self.details_toggle = _button('标题与说明 ▾', self._toggle_details)
        self.details_panel = QWidget()
        details_form = QFormLayout(self.details_panel)
        details_form.setContentsMargins(0, 0, 0, 0)
        details_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText('标题（可选）')
        details_form.addRow('标题', self.title_edit)
        self.issue_edit = QPlainTextEdit()
        self.issue_edit.setPlaceholderText('问题与预期行为（可选）')
        self.issue_edit.setMaximumHeight(64)
        details_form.addRow('问题描述', self.issue_edit)
        self.details_panel.hide()
        input_layout.addWidget(self.details_toggle)
        input_layout.addWidget(self.details_panel)
        self.prepare_button = _button('仅冻结版本', self.prepare)
        self.prepare_discover_button = _button('准备并发现测试', lambda: self.prepare(discover_tests=True), True)
        self.prepare_discover_button.setToolTip('保存独立源码快照并运行可信项目的测试收集；完成后核对所选范围，再明确运行两版。')
        self.prepare_button.setToolTip('从当前输入重新冻结两版源码；原会话和报告继续保留。')
        self.discover_button = _button('发现测试', self.discover, True)
        self.discover_button.setToolTip('在固定的变更后快照运行 pytest --collect-only；会执行项目的测试收集代码。')
        self.run_button = _button('运行所选测试', self.run_selected, True)
        self.ai_button = _button('准备 AI 检查计划…', self.prepare_ai)
        self.ai_button.setParent(self)
        self.ai_button.hide()
        action = self.project_menu.addAction(self.ai_button.text())
        action.triggered.connect(lambda: self.ai_button.click())
        self.project_menu_actions.append((self.ai_button, action))
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(.1, 3600)
        self.timeout_spin.setDecimals(1)
        self.timeout_spin.setValue(120)
        self.timeout_spin.setSuffix(' 秒 / 版')
        self.timeout_spin.setMaximumWidth(140)
        self.timeout_spin.setToolTip('每次测试发现，以及每个版本的全部所选测试，分别共用此超时；范围 0.1–3600 秒。')
        self.next_step_label = _label()
        self.next_step_label.setStyleSheet('color: #5a6b82; font-size: 12px;')
        self.workflow_actions = QWidget()
        actions = QHBoxLayout(self.workflow_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        self.timeout_label = _label('每版限时')
        actions.addStretch(1)
        actions.addWidget(self.timeout_label)
        actions.addWidget(self.timeout_spin)
        for item in (self.prepare_button, self.prepare_discover_button, self.discover_button, self.run_button):
            actions.addWidget(item)
        layout.addWidget(self.workflow_actions)
        self.collection_path_edit = QLineEdit()
        self.collection_path_edit.setMaxLength(4000)
        self.collection_path_edit.setPlaceholderText('例如 tests/unit，留空使用 pytest 默认范围')
        self.collection_path_edit.setToolTip('填写固定快照内的相对测试文件或目录，在发现测试时生效。')
        self.collection_path_edit.textChanged.connect(self._collection_path_changed)
        self.collection_path_row = _row(_label('发现范围'), self.collection_path_edit)
        self.collection_path_row.layout().setStretch(1, 1)
        layout.addWidget(self.collection_path_row)
        layout.addWidget(self.next_step_label)

        self.problem_panel = QWidget()
        problem_layout = QVBoxLayout(self.problem_panel)
        problem_layout.setContentsMargins(0, 0, 0, 0)
        self.problem_details = _text_view()
        self.problem_details.setMaximumHeight(64)
        self.problem_details.setStyleSheet('background: #fff5f3; border-color: #e4b8b1;')
        problem_layout.addWidget(self.problem_details, 1)
        self.problem_copy_button = _button('复制错误详情', self._copy_problem)
        self.problem_environment_button = _button('检查项目环境', lambda: self.inspect_environment(continue_collection=True))
        self.problem_retry_button = _button('重试发现测试', self.discover)
        self.problem_refresh_button = _button('更新源码并发现', lambda: self.refresh_workspace(discover_tests=True))
        self.problem_refresh_button.setToolTip('已修改原项目？沿用固定基线另存新快照并发现测试；保留旧证据，核对范围后再运行。')
        recovery = QHBoxLayout()
        for button in (self.problem_environment_button, self.problem_retry_button, self.problem_refresh_button, self.problem_copy_button):
            recovery.addWidget(button)
        recovery.addStretch(1)
        problem_layout.addLayout(recovery)
        layout.addWidget(self.problem_panel)
        self.problem_panel.hide()

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs, 1)
        self._build_changes_tab()
        self._build_tests_tab()
        self._build_results_tab()
        self._build_history_tab()

        self.status_label = _CompactLabel()
        self.cancel_button = _button('取消当前操作', self.cancel_operation)
        footer = QHBoxLayout()
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.cancel_button)
        layout.addLayout(footer)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(5)
        layout.addWidget(self.progress_bar)
        self.tabs.currentChanged.connect(self._sync_stage_actions)

    def _sync_stage_actions(self, *_):
        stage = self.tabs.currentIndex()
        if stage == 1 and self.task is None and self.collection and self.problem_panel.isHidden():
            diagnosis = collection_diagnosis(self.collection)
            if diagnosis['code'] != 'collected':
                self._show_problem(diagnosis['summary'])
        self.prepare_button.setVisible(stage == 0)
        self.prepare_button.setText('重新冻结版本' if self.state else '仅冻结版本')
        self.prepare_discover_button.setVisible(stage == 0 and self.state is None)
        self.discover_button.setVisible(stage < 2 and self.state is not None)
        self.discover_button.setText('重新发现' if self.collection else '发现测试')
        self.run_button.setVisible(stage == 1)
        self.timeout_label.setVisible(stage < 2)
        self.timeout_spin.setVisible(stage < 2)
        self.workflow_actions.setVisible(stage < 2)
        self.collection_path_row.setVisible(stage < 2)
        self._update_next_step()
        self.session_summary.setVisible(self.state is not None)
        self.change_panel.setVisible(self.state is not None)
        self.result_metrics.setVisible(self.report is not None and not self.report.get('review_source'))
        self.selection_review_panel.setVisible(self._pending_selection is not None)

    def _build_changes_tab(self):
        page = QWidget()
        body = QVBoxLayout(page)
        body.setContentsMargins(0, 14, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 8, 0)
        column.setSpacing(16)
        column.addWidget(self.input_section)
        self.change_panel = QWidget()
        changes = QVBoxLayout(self.change_panel)
        changes.setContentsMargins(0, 0, 0, 0)
        changes.setSpacing(10)
        self.change_search = QLineEdit()
        self.change_search.setPlaceholderText('搜索改动文件…')
        self.change_count = _CompactLabel('尚未冻结版本')
        self.change_filter = QComboBox()
        for title, value in (('全部改动', 'all'), ('测试相关', 'tests'), ('Python 源码', 'python'),
                            ('配置与依赖', 'config'), ('文档', 'docs')):
            self.change_filter.addItem(title, value)
        changes.addWidget(_row(self.change_search, self.change_filter, self.change_count))
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.changes_tree = _tree(['改动文件', '类型'])
        self.diff_view = _text_view('选择改动文件，查看冻结版本的代码差异。')
        splitter.addWidget(self.changes_tree)
        splitter.addWidget(self.diff_view)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setMinimumHeight(240)
        changes.addWidget(splitter, 1)
        column.addWidget(self.change_panel, 1)
        column.addStretch(0)
        self.scroll.setWidget(content)
        body.addWidget(self.scroll)
        self.tabs.addTab(page, '01  项目与版本')
        self.change_search.textChanged.connect(self._filter_changes)
        self.change_filter.currentIndexChanged.connect(self._filter_changes)
        self.changes_tree.currentItemChanged.connect(self._show_diff)

    def _build_tests_tab(self):
        page = QWidget()
        body = QVBoxLayout(page)
        self.test_search = QLineEdit()
        self.test_search.setPlaceholderText('搜索完整测试 ID、文件、类名或参数…')
        self.test_filter = QComboBox()
        for text, value in (('全部测试', 'all'), ('与改动相关', 'recommended'), ('已选择', 'selected')):
            self.test_filter.addItem(text, value)
        self.select_visible_button = _button('选中可见', self.select_visible_tests)
        self.only_visible_button = _button('仅选可见', self.select_only_visible_tests)
        self.select_recommended_button = _button('只选推荐', self.select_recommended_tests)
        self.clear_tests_button = _button('清空', self.clear_selected_tests)
        self.test_scope = QComboBox()
        self.test_scope.addItem('全部文件与目录', '')
        self.test_scope.setMinimumContentsLength(18)
        self.test_scope.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        body.addWidget(_row(self.test_search, self.test_scope, self.test_filter))
        self.selection_more_button = QPushButton('选择操作')
        selection_menu = QMenu(self)
        for text, callback in (('仅选当前可见项', self.select_only_visible_tests),
                               ('仅选与改动相关的测试', self.select_recommended_tests),
                               ('反选当前可见项', self.invert_visible_tests),
                               ('复制已勾选测试 ID', self.copy_test_selection),
                               ('导出选择清单…', self.export_test_selection),
                               ('导入选择清单…', self.import_test_selection)):
            selection_menu.addAction(text).triggered.connect(lambda checked=False, fn=callback: fn())
        self.selection_more_button.setMenu(selection_menu)
        for item in (self.only_visible_button, self.select_recommended_button):
            item.setParent(self)
            item.hide()
        selection_menu.addSeparator()
        selection_menu.addAction('管理常用选择方案').triggered.connect(
            lambda: self.preset_panel.setVisible(self.preset_panel.isHidden()))
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumContentsLength(18)
        self.preset_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.preset_combo.addItem('此项目的常用选择方案', None)
        self.preset_apply_button = _button('应用方案', self.apply_selection_preset)
        self.preset_save_button = _button('保存当前选择…', self.save_selection_preset)
        self.preset_delete_button = _button('删除方案…', self.delete_selection_preset)
        self.preset_panel = _row(self.preset_combo, self.preset_apply_button,
                                  self.preset_save_button, self.preset_delete_button)
        self.preset_panel.hide()
        body.addWidget(self.preset_panel)
        self.test_count_label = _CompactLabel('发现测试后，选择本次要检查的范围。')
        selection_actions = QHBoxLayout()
        selection_actions.addWidget(self.test_count_label, 1)
        selection_actions.addWidget(self.select_visible_button)
        selection_actions.addWidget(self.clear_tests_button)
        selection_actions.addWidget(self.selection_more_button)
        body.addLayout(selection_actions)
        self.selection_review_panel = QWidget()
        review_row = QHBoxLayout(self.selection_review_panel)
        review_row.setContentsMargins(0, 0, 0, 0)
        self.selection_review_label = _label()
        self.selection_review_button = _button('核对带入的选择…', self.review_inherited_selection)
        review_row.addWidget(self.selection_review_label, 1)
        review_row.addWidget(self.selection_review_button)
        self.selection_review_panel.hide()
        body.addWidget(self.selection_review_panel)
        self.tests_tree = _tree(['完整测试 ID（含参数）', '推荐原因'])
        self.tests_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        body.addWidget(self.tests_tree, 1)
        self.collection_log = _text_view('测试发现的日志和收集错误会显示在这里。')
        self.collection_log.setMaximumHeight(180)
        self.collection_log.hide()
        self.collection_log_button = _button('展开测试发现日志', self._toggle_collection_log)
        self.collection_log_button.setObjectName('quiet')
        body.addWidget(self.collection_log_button, 0, Qt.AlignmentFlag.AlignLeft)
        body.addWidget(self.collection_log)
        self.tabs.addTab(page, '02  测试范围')
        self.test_search_timer = QTimer(self)
        self.test_search_timer.setSingleShot(True)
        self.test_search_timer.setInterval(120)
        self.test_search_timer.timeout.connect(self._filter_tests)
        self.test_search.textChanged.connect(lambda: self.test_search_timer.start())
        self.test_search.textChanged.connect(self._schedule_view_save)
        self.test_scope.currentIndexChanged.connect(self._filter_tests)
        self.test_scope.currentIndexChanged.connect(self._schedule_view_save)
        self.test_filter.currentIndexChanged.connect(self._filter_tests)
        self.test_filter.currentIndexChanged.connect(self._schedule_view_save)
        self.tests_tree.itemChanged.connect(self._test_checked)
        self.preset_combo.currentIndexChanged.connect(self._sync_controls)

    def _build_results_tab(self):
        page = QWidget()
        body = QVBoxLayout(page)
        self.result_summary = _label('尚无项目回归报告')
        body.setContentsMargins(0, 14, 0, 0)
        body.setSpacing(12)
        self.result_summary.setStyleSheet('font-size: 18px; font-weight: 600;')
        self.result_summary.setMaximumHeight(28)
        self.result_summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        body.addWidget(self.result_summary)
        self.review_panel = QWidget()
        review_layout = QVBoxLayout(self.review_panel)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.setSpacing(4)
        self.review_summary = _label('')
        self.review_summary.setStyleSheet('font-size: 14px; font-weight: 600;')
        self.review_summary.setToolTip('只比较报告绑定的来源运行。同条件转绿要求共享测试及已记录条件一致；不代表完整环境冻结或新的严格修复验收。')
        self.review_scope = _label('')
        self.review_scope.setStyleSheet('color: #5a6b82;')
        self.compare_source_button = _button('查看逐项变化', self._show_review_details)
        self.review_retry_button = _button('重新核对', lambda: self.compare_source_report(show_dialog=False))
        self.review_relocate_button = _button('重新定位来源', self.relocate_source_report)
        review_row = _row(self.review_summary, self.compare_source_button,
                          self.review_retry_button, self.review_relocate_button)
        review_row.layout().setStretch(0, 1)
        for button in (self.compare_source_button, self.review_retry_button, self.review_relocate_button):
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        review_layout.addWidget(review_row)
        review_layout.addWidget(self.review_scope)
        self.review_panel.hide()
        body.addWidget(self.review_panel)
        self.run_progress_panel = QWidget()
        progress_layout = QVBoxLayout(self.run_progress_panel)
        progress_layout.setContentsMargins(12, 8, 12, 8)
        progress_layout.setSpacing(3)
        self.run_progress_label = _label('')
        self.run_current_test = _CompactLabel('')
        self.run_current_test.setStyleSheet('color: #717781; font-size: 12px;')
        progress_layout.addWidget(self.run_progress_label)
        progress_layout.addWidget(self.run_current_test)
        self.run_progress_panel.setStyleSheet('background: #f0f3f7; border-radius: 6px;')
        self.run_progress_panel.hide()
        body.addWidget(self.run_progress_panel)
        self.result_metrics = QWidget()
        metrics = QHBoxLayout(self.result_metrics)
        metrics.setContentsMargins(0, 0, 0, 0)
        metrics.setSpacing(10)
        self.result_metric_buttons = {}
        for key, title in (('regression_detected', '新增失败'), ('baseline_failure', '两版均失败'),
                           ('inconclusive', '受阻'), ('improved', '已改善'), ('regression_passed', '两版通过')):
            metric = QPushButton('0\n' + title)
            metric.setObjectName('metric')
            metric.clicked.connect(lambda checked=False, value=key: self.result_filter.setCurrentIndex(
                self.result_filter.findData(value)))
            metrics.addWidget(metric, 1)
            self.result_metric_buttons[key] = (metric, title)
        body.addWidget(self.result_metrics)
        self.result_search = QLineEdit()
        self.result_search.setPlaceholderText('搜索测试 ID、状态或异常…')
        self.result_filter = QComboBox()
        self.result_filter.addItem('全部结果', 'all')
        self.result_filter.addItem('需要处理的结果', 'attention')
        for status in ('regression_detected', 'baseline_failure', 'inconclusive',
                       'improved', 'regression_passed'):
            self.result_filter.addItem(_STATUS[status], status)
        self.retry_failed_button = _button('固定快照：重跑失败／受阻', self.rerun_failed)
        self.retry_failed_button.setToolTip('使用当前会话的固定源码；修改原项目源码后，请先刷新当前工作区。')
        self.retry_selected_button = _button('固定快照：重跑所选行', self.rerun_selected)
        self.retry_selected_button.setToolTip('使用当前会话的固定源码；修改原项目源码后，请先刷新当前工作区。')
        self.attention_button = _button('需处理', self.show_attention)
        self.result_more_button = QPushButton('更多操作')
        result_menu = QMenu(self)
        self.result_actions = {}
        for text, callback in (('固定快照：重跑失败／受阻', self.rerun_failed),
                               ('固定快照：重跑所选行', self.rerun_selected),
                               ('重开已保存的修复准备（不执行）…', self.reopen_repair_preparation),
                               ('恢复修复说明草稿…', self.reopen_repair_brief),
                               ('重新定位当前报告…', self.relocate_current_report),
                               ('复制当前筛选结果摘要', self.copy_result_summary),
                               ('复制选中行的测试 ID', self.copy_result_ids),
                               ('复制此项诊断与建议', self.copy_entry_diagnosis),
                               ('查看所选项的实际测试代码', self.preview_selected_test),
                               ('检查此会话的项目环境…', self.inspect_result_environment),
                               ('导出当前筛选结果 CSV…', self.export_results_csv)):
            action = result_menu.addAction(text)
            action.triggered.connect(lambda checked=False, fn=callback: fn())
            self.result_actions[callback.__name__] = action
        self.result_more_button.setMenu(result_menu)
        self.clear_result_filters_button = _button('清除筛选', self.clear_result_filters)
        self.clear_result_filters_button.setMinimumHeight(28)
        self.clear_result_filters_button.setObjectName('quiet')
        self.copy_selected_id_button = _button('复制 ID', self.copy_result_ids)
        self.copy_selected_id_button.setObjectName('quiet')
        self.report_context_label = _CompactLabel('打开报告后可复查当前修改。')
        self.report_context_label.setStyleSheet('color: #8a8f98; font-size: 11px;')
        self.report_context_label.setParent(self)
        self.report_context_label.hide()
        self.recheck_scope = QComboBox()
        self.recheck_scope.addItem('来源报告全部实际选择（0 项）', 'all')
        self.recheck_scope.addItem('仅新增失败／原有失败／受阻（0 项）', 'attention')
        self.recheck_button = _button('用当前工作区复查（0 项）', self.recheck_report)
        self.recheck_button.setToolTip('沿用原固定基线。可直接复查并运行，也可只准备新会话；缺失测试或检查内容变化时会停下供你核对。')
        self.recheck_scope.setParent(self)
        self.recheck_scope.hide()
        self.recheck_button.clicked.disconnect()
        recheck_menu = QMenu(self)
        self.quick_recheck_all_action = recheck_menu.addAction('复查并运行全部已选测试')
        self.quick_recheck_attention_action = recheck_menu.addAction('复查并运行需处理项')
        self.quick_recheck_all_action.triggered.connect(lambda: self.quick_recheck('all'))
        self.quick_recheck_attention_action.triggered.connect(lambda: self.quick_recheck('attention'))
        recheck_menu.addSeparator()
        self.recheck_all_action = recheck_menu.addAction('复查全部已选测试')
        self.recheck_attention_action = recheck_menu.addAction('仅复查需要处理的测试')
        self.recheck_all_action.triggered.connect(lambda: self._recheck_with_scope('all'))
        self.recheck_attention_action.triggered.connect(lambda: self._recheck_with_scope('attention'))
        self.recheck_button.setMenu(recheck_menu)
        self.processing_history_button = _button('处理记录', self.show_processing_history)
        body.addWidget(_row(self.result_search, self.result_filter, self.attention_button,
                            self.recheck_button, self.result_more_button))
        self.diagnostic_panel = QWidget()
        diagnostic_layout = QVBoxLayout(self.diagnostic_panel)
        diagnostic_layout.setContentsMargins(0, 0, 0, 0)
        diagnostic_layout.setSpacing(4)
        self.diagnostic_selector = QComboBox()
        self.diagnostic_selector.setMinimumContentsLength(20)
        self.diagnostic_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.diagnostic_selector.addItem('按全部现象查看', None)
        self.copy_diagnostic_button = _button('复制本组诊断', self.copy_diagnostic_group)
        self.diagnostic_action_button = _button('查看代表项', self.open_diagnostic_action)
        self.recheck_diagnostic_button = _button('复查本组', self.recheck_diagnostic_group)
        diagnostic_row = QHBoxLayout()
        diagnostic_row.addWidget(self.diagnostic_selector, 1)
        for button in (self.copy_diagnostic_button, self.diagnostic_action_button, self.recheck_diagnostic_button):
            diagnostic_row.addWidget(button)
        diagnostic_layout.addLayout(diagnostic_row)
        self.diagnostic_notice = _CompactLabel('')
        self.diagnostic_notice.setStyleSheet('color: #717781; font-size: 12px;')
        diagnostic_layout.addWidget(self.diagnostic_notice)
        body.addWidget(self.diagnostic_panel)
        self.diagnostic_panel.hide()
        self.result_count_label = _CompactLabel('尚无结果')
        result_count_row = QHBoxLayout()
        result_count_row.addWidget(self.result_count_label, 1)
        result_count_row.addWidget(self.clear_result_filters_button)
        body.addLayout(result_count_row)
        self.diagnostic_selector.currentIndexChanged.connect(self._diagnostic_changed)
        self.recheck_scope.currentIndexChanged.connect(self._sync_controls)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.results_tree = _tree(['测试 ID', '回归结果', '基线', '变更后', '耗时（前 / 后）'])
        self.results_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.results_tree.header().setMinimumSectionSize(64)
        self.results_tree.setColumnHidden(2, True)
        self.results_tree.setColumnHidden(3, True)
        self.results_tree.setUniformRowHeights(False)
        self.results_tree.setHeaderLabels(['测试', '结果', '基线', '变更后', '耗时'])
        self.details_tabs = QTabWidget()
        self.result_details = _text_view('选择一个测试，查看两版异常、阶段和耗时。')
        self.version_logs = _text_view('完整日志、事件和快照保存在报告材料中。')
        self.test_code_view = _text_view('从“结果操作…”选择查看实际测试代码，只读取报告保存的共享测试，不执行代码。')
        self.details_tabs.addTab(self.result_details, '异常与阶段')
        self.details_tabs.addTab(self.version_logs, '两版日志')
        self.details_tabs.addTab(self.test_code_view, '测试代码')
        detail_panel = QWidget()
        detail_panel.setObjectName('detailPanel')
        detail_body = QVBoxLayout(detail_panel)
        detail_body.setContentsMargins(14, 14, 14, 14)
        detail_body.setSpacing(10)
        detail_header = QHBoxLayout()
        detail_title = _label('当前测试')
        detail_title.setStyleSheet('font-weight: 600;')
        detail_header.addWidget(detail_title, 1)
        detail_header.addWidget(self.copy_selected_id_button)
        detail_body.addLayout(detail_header)
        self.selected_result_summary = _text_view('选择一个失败，查看实际状态和下一步。')
        self.selected_result_summary.setFixedHeight(100)
        self.selected_result_summary.setStyleSheet('border: none; background: transparent; padding: 0;')
        self.result_next_button = _button('查看结果详情', self.open_result_action, True)
        self.result_next_notice = _label('请选择一个结果行。')
        self.result_next_notice.setStyleSheet('color: #717781; font-size: 12px;')
        detail_body.addWidget(self.result_next_button)
        detail_body.addWidget(self.result_next_notice)
        detail_body.addWidget(self.selected_result_summary)
        self.repair_brief_button = _button('准备修复说明', self.prepare_repair_brief)
        self.repair_case_button = _button('验收当前修复', self.prepare_result_repair)
        self.result_environment_button = _button('检查项目环境…', self.inspect_result_environment)
        detail_body.addWidget(self.repair_brief_button)
        detail_body.addWidget(self.repair_case_button)
        self.repair_reason = _label('请先选择一个结果行。')
        self.repair_reason.setStyleSheet('color: #8a8f98; font-size: 12px;')
        detail_body.addWidget(self.repair_reason)
        detail_body.addWidget(self.result_environment_button)
        detail_body.addWidget(self.details_tabs, 1)
        splitter.addWidget(self.results_tree)
        self.result_detail_scroll = QScrollArea()
        self.result_detail_scroll.setWidgetResizable(True)
        self.result_detail_scroll.setMinimumWidth(295)
        self.result_detail_scroll.setWidget(detail_panel)
        splitter.addWidget(self.result_detail_scroll)
        self.results_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results_tree.customContextMenuRequested.connect(self._result_context_menu)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setMinimumHeight(190)
        body.addWidget(splitter, 1)
        self.open_report_button = _button('完整报告', self.open_report)
        self.open_evidence_button = _button('打开证据目录', self.open_evidence)
        self.check_report_button = _button('检查证据', self.check_report)
        self.export_button = _button('导出报告 ZIP…', self.export_report)
        self.replay_button = _button('重跑这份固定快照…', self.replay_report)
        self.report_more_button = QPushButton('导出与证据')
        report_menu = QMenu(self)
        for text, callback in (('打开证据目录', self.open_evidence),
                               ('导出完整报告与证据 ZIP…', self.export_report),
                               ('重跑这份固定快照（需明确开始）…', self.replay_report),
                               ('打开复查来源报告', self.open_source_report),
                               ('重新定位复查来源报告…', self.relocate_source_report)):
            action = report_menu.addAction(text)
            action.triggered.connect(lambda checked=False, fn=callback: fn())
            if callback == self.open_source_report:
                self.open_source_button = action
            elif callback == self.relocate_source_report:
                self.relocate_source_button = action
        self.export_sources_action = report_menu.addAction('导出本次与复查来源 ZIP…')
        self.export_sources_action.triggered.connect(lambda: self.export_report(include_sources=True))
        self.report_more_button.setMenu(report_menu)
        report_actions = QHBoxLayout()
        report_actions.addWidget(self.open_report_button)
        report_actions.addWidget(self.check_report_button)
        report_actions.addWidget(self.processing_history_button)
        report_actions.addStretch(1)
        report_actions.addWidget(self.report_more_button)
        body.addLayout(report_actions)
        self.results_scroll = QScrollArea()
        self.results_scroll.setWidgetResizable(True)
        self.results_scroll.setStyleSheet('QScrollArea { border: none; background: transparent; }')
        self.results_scroll.setWidget(page)
        self.tabs.addTab(self.results_scroll, '03  检查结果')
        self.result_search.textChanged.connect(self._filter_results)
        self.result_filter.currentIndexChanged.connect(self._filter_results)
        self.results_tree.currentItemChanged.connect(self._show_result_detail)
        self.results_tree.itemSelectionChanged.connect(self._sync_controls)

    def _build_history_tab(self):
        page = QWidget()
        body = QVBoxLayout(page)
        self.history_label = _CompactLabel('项目会话保存输入、已发现测试和每次运行记录；重开后可以继续。')
        body.addWidget(self.history_label)
        self.history_tree = _tree(['运行', '结果', '保存时间', '报告目录'], 3)
        self.history_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        body.addWidget(self.history_tree, 1)
        self.open_run_button = _button('查看所选运行', self.open_selected_run)
        self.open_session_folder_button = _button('打开会话保存目录', self.open_session_folder)
        self.compare_runs_button = _button('对比所选两次运行', self.compare_selected_runs)
        body.addWidget(_row(self.open_run_button, self.compare_runs_button, self.open_session_folder_button))
        self.tabs.addTab(page, '历史记录')
        self.history_tree.itemDoubleClicked.connect(lambda *_: self.open_selected_run())
        self.history_tree.itemSelectionChanged.connect(self._sync_controls)

    def _connect_inputs(self):
        self.repository_edit.textChanged.connect(self._repository_changed)
        self.title_edit.textChanged.connect(self._invalidate)
        self.python_edit.textChanged.connect(self._python_changed)
        self.before_combo.currentTextChanged.connect(self._invalidate)
        self.after_combo.currentTextChanged.connect(self._invalidate)
        self.workspace_check.toggled.connect(self._workspace_changed)
        self.issue_edit.textChanged.connect(self._invalidate)
        self.timeout_spin.valueChanged.connect(self._schedule_view_save)

    def _repository_changed(self, *_):
        if self._loading_inputs:
            return
        self._set_python_candidates([])
        self.environment_hint.setText('选择已安装项目依赖的 Python；点击“读取提交”可查找常见项目环境。')
        self._invalidate()

    def focus_search(self):
        searches = (self.change_search, self.test_search, self.result_search)
        index = self.tabs.currentIndex()
        if index < len(searches):
            edit = searches[index]
            if index == 0:
                self.scroll.ensureWidgetVisible(edit)
            edit.setFocus()
            edit.selectAll()

    def toggle_inputs(self):
        if self.tabs.currentIndex() != 0:
            self.tabs.setCurrentIndex(0)
            self._set_inputs_expanded(True)
        else:
            self._set_inputs_expanded(self.input_panel.isHidden())

    def _set_inputs_expanded(self, expanded):
        self.input_section.setVisible(bool(expanded))
        self.input_panel.setVisible(bool(expanded))
        self.details_toggle.setVisible(bool(expanded))
        if not expanded:
            self.details_panel.hide()
            self.details_toggle.setText('标题与说明 ▾')
        self.input_toggle.setText('收起配置' if expanded else '版本与环境')

    def _toggle_details(self):
        expanded = self.details_panel.isHidden()
        self.details_panel.setVisible(expanded)
        self.details_toggle.setText('标题与说明 ▴' if expanded else '标题与说明 ▾')

    def _toggle_collection_log(self):
        self._set_collection_log_visible(self.collection_log.isHidden())

    def _set_collection_log_visible(self, visible):
        self.collection_log.setVisible(visible)
        self.collection_log_button.setText('收起测试发现日志' if visible else '展开测试发现日志')

    def _copy_problem(self):
        text = self.problem_details.toPlainText()
        if self.collection and text == collection_diagnosis(self.collection)['summary']:
            text = self.collection_log.toPlainText()
        QApplication.clipboard().setText(text)

    def _schedule_view_save(self, *_):
        if (self._loading_inputs or self._restoring_view or not self.state or not self.collection
                or self.collection.get('status') != 'collected'):
            return
        self._view_dirty = True
        self.view_save_timer.start()

    def _save_view_state(self):
        self.view_save_timer.stop()
        if not self._view_dirty or not self.state or not self.collection:
            return
        if len(self.selected_test_ids()) > _MAX_SELECTED:
            self.status_label.setText('勾选超过 2000 项，当前选择偏好尚未保存；请缩小范围。')
            return
        data = {'schema_version': 1, 'project_id': self.state['project_id'],
                'source_sha256': self.state['source_sha256'],
                'runtime': self.collection.get('runtime', {}).get('executable'),
                'selected_tests': self.selected_test_ids(), 'pending_tests': self._pending_selection,
                'search': self.test_search.text(),
                'scope': self.test_scope.currentData() or '', 'filter': self.test_filter.currentData(),
                'timeout': self.timeout_spin.value()}
        try:
            path = Path(self.state['directory']) / 'desktop-view.json'
            if path.is_symlink():
                raise ValueError('偏好文件不能是符号链接。')
            save_json(path, data)
            self._view_dirty = False
        except (OSError, ValueError) as exc:
            self.status_label.setText('测试选择偏好未能保存：' + str(exc))

    def _restore_view_state(self):
        self._has_restored_selection = False
        if not self.state or not self.collection:
            return
        path = Path(self.state['directory']) / 'desktop-view.json'
        if not path.exists():
            return
        try:
            if path.is_symlink():
                raise ValueError('偏好文件不能是符号链接。')
            data = read_json(path, limit=16 * 1024 * 1024 + 65536)
            if (data.get('schema_version') != 1 or data.get('project_id') != self.state['project_id']
                    or data.get('source_sha256') != self.state['source_sha256']
                    or data.get('runtime') != self.collection.get('runtime', {}).get('executable')):
                return
            selected = data.get('selected_tests')
            if (not isinstance(selected, list) or len(selected) > _MAX_SELECTED
                    or any(not isinstance(value, str) or len(value) > 4000 for value in selected)):
                raise ValueError('保存的勾选列表无效。')
            self._restoring_view = True
            pending = data.get('pending_tests')
            requested = validate_selection(pending, allow_empty=True) if pending is not None else validate_selection(selected, allow_empty=True)
            review = reconcile_selection(requested, self.collection.get('tests') or [],
                collection_complete=self.collection.get('status') == 'collected'
                and not (self.collection.get('partial') or self.collection.get('truncated')))
            self._selection_intent = requested
            self._pending_selection = requested if review['requires_confirmation'] else None
            self._check_ids(set() if self._pending_selection is not None else set(review['matched_tests']), explicit=False)
            self._set_selection_review(requested, '保存的待核对范围' if pending is not None else '保存的选择')
            search = data.get('search', '')
            if isinstance(search, str):
                self.test_search.setText(search[:4000])
            for widget, value in ((self.test_scope, data.get('scope')), (self.test_filter, data.get('filter'))):
                index = widget.findData(value)
                widget.setCurrentIndex(max(0, index))
            timeout = data.get('timeout')
            if type(timeout) in (int, float) and .1 <= timeout <= 3600:
                self.timeout_spin.setValue(timeout)
            self._has_restored_selection = True
        except (OSError, ValueError, TypeError) as exc:
            self.status_label.setText('保存的选择偏好无法恢复，当前发现清单仍可手动选择：' + str(exc))
        finally:
            self._restoring_view = False

    def _show_problem(self, message):
        self.problem_details.setPlainText(str(message))
        self.problem_panel.show()

    def _clear_problem(self):
        self.problem_details.clear()
        self.problem_panel.hide()

    def _set_python_candidates(self, candidates):
        selected = self.python_edit.text()
        self.python_combo.blockSignals(True)
        self.python_edit.blockSignals(True)
        try:
            self.python_combo.clear()
            self.python_combo.addItem('')
            for path in dict.fromkeys([*candidates, *([selected] if selected else [])]):
                self.python_combo.addItem(path)
            self.python_edit.setText(selected)
        finally:
            self.python_combo.blockSignals(False)
            self.python_edit.blockSignals(False)
        self.python_combo.setToolTip('候选环境只列出路径；选择后，在发现和运行时使用。')
        self.environment_hint.setText(
            f'发现 {len(candidates)} 个项目环境，可在下拉列表中选择；不会自动切换或执行。'
            if candidates else '未发现常见项目环境。可选择已有 Python，或点击“检查环境”准备项目外的新环境。')

    def _update_next_step(self):
        stage = self.tabs.currentIndex()
        warning = False
        if self.task is not None:
            message = {'prepare': '正在冻结输入，完成后可浏览改动并发现测试。',
                       'prepare_collect': '正在准备快照并发现测试，完成后核对范围；尚不运行测试主体。',
                       'refresh': '正在另存当前工作区。基线提交保持固定，旧会话和报告继续保留。',
                       'recheck': '正在核对指定来源报告并准备新快照，发现和运行仍需分别发起。',
                       'compare_source': '正在核对来源身份和两份执行证据，再比较测试范围与条件。',
                       'collect': '正在发现实际测试。完成后先核对勾选范围，再运行两版。',
                       'run': '正在对比两版。取消会保留已完成记录，未完成项不会算作通过。'}.get(
                           self._operation, '正在处理当前操作，请等待完成。')
        elif stage == 1 and self._collection_path_pending():
            message = '发现范围已修改，请重新发现测试。'
            warning = True
        elif stage == 1 and self._pending_selection is not None:
            message = '原测试范围等待核对。请查看原 ID、采用可用项或手动勾选。'
            warning = True
        elif stage == 1 and self.collection and self.collection.get('tests'):
            count = len(self.selected_test_ids())
            hidden = sum(self.tests_tree.topLevelItem(i).isHidden()
                         and self.tests_tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked
                         for i in range(self.tests_tree.topLevelItemCount()))
            if count == 0:
                message = '当前选择为空。请勾选测试，或点击“选中可见”后核对范围。'
            elif count > _MAX_SELECTED:
                message = f'已选 {count} 项，超过单次 {_MAX_SELECTED} 项上限。请缩小勾选范围后运行。'
            elif hidden:
                message = f'{hidden} 项被当前筛选隐藏，运行仍包含这些勾选项。'
            else:
                message = ''
            warning = bool(message)
        elif not self.problem_panel.isHidden():
            if self._operation in ('recheck', 'compare_source', 'relocate_report'):
                message = ('报告操作已停止。来源本次未找到时使用“重新定位来源报告”；当前报告移动时从“复制／导出…”重新定位。'
                           '身份或证据不一致时先核对完整原报告，不会改用最近一次运行。')
            else:
                message = ('测试发现未完成：可直接检查项目环境并重试，固定源码保留。'
                           if self.state is not None and (self.collection is None or self.collection.get('status') != 'collected') else
                           '操作未完成：请查看下方错误详情。需要调整解释器时，可回到“项目与版本”。')
        elif self.state is None:
            message = '第 1 步：选择项目与环境，点击“准备并发现测试”。收集会加载可信项目代码，随后核对测试范围。'
        elif self._collection_path_pending():
            message = '发现范围已修改，请重新发现测试。'
        elif self.collection is None:
            message = '第 2 步：输入已冻结，点击“发现测试”。自己的项目应先选好已安装依赖的 Python 环境。'
            if self.state.get('refresh_origin'):
                message += '当前为刷新后的新会话，测试选择尚待核对。'
        elif self.collection.get('status') != 'collected':
            message = '测试发现未完成：先查看测试发现日志，处理环境或收集错误，再点击“发现测试”重试。'
        elif self.report is not None:
            status = self.report.get('status')
            message = {
                'regression_detected': '发现新增失败：点击“定位需处理项”查看原因。修改原仓库后，点击“刷新当前工作区”，重新发现并核对选择后运行。',
                'attention': '还有原有失败或受阻项：点击“定位需处理项”查看原因。修改源码后须刷新工作区；只调整环境时可直接重新发现或重跑。',
                'cancelled': '运行已取消：已完成证据保留。检查未完成项后，可以只重跑失败／受阻项。',
            }.get(status, '所选回归已完成，可打开报告或导出证据。需要验收一个目标修复时，请使用“案例验收”。')
        else:
            message = ''
        self.next_step_label.setText(message)
        self.next_step_label.setVisible(warning)
        self.tabs.tabBar().setTabToolTip(0, '选择项目与版本，准备固定快照；随后发现并核对测试范围。')
        self.tabs.tabBar().setTabToolTip(1, '勾选的全部测试都会执行，包括被筛选隐藏的项。')

    def _invalidate(self, *_):
        if self._loading_inputs:
            return
        self._save_view_state()
        self._view_dirty = False
        self._generation += 1
        self._clear_problem()
        had_session = self.state is not None
        self.state = self.collection = self.report = None
        self._ai_plan_path = None
        self._clear_content()
        self._set_inputs_expanded(True)
        if had_session:
            self.session_label.setText('输入已改变，请重新冻结版本；原会话和历史报告仍保存在磁盘。')
            self.status_label.setText('请重新准备项目，避免使用上一组版本或环境的测试和结果。')
        self._sync_controls()

    def _python_changed(self, *_):
        if self._loading_inputs:
            return
        self._clear_problem()
        self._save_view_state()
        self._view_dirty = False
        if self.state is None:
            self._invalidate()
            return
        self._generation += 1
        self.state = dict(self.state)
        self.state['collection'] = None
        if self.collection and self.collection.get('status') == 'collected' and self._pending_selection is None:
            self._selection_intent = self.selected_test_ids()
        self.collection = self.report = None
        self._selection_review = None
        self.selection_review_panel.hide()
        self._ai_plan_path = None
        self._live_entries = {}
        self._report_items = {}
        self._changing_checks = True
        self.tests_tree.clear()
        self.results_tree.clear()
        self._changing_checks = False
        for view in (self.collection_log, self.result_details, self.version_logs):
            view.clear()
        self.test_search.clear()
        self.result_search.clear()
        self.test_filter.setCurrentIndex(0)
        self.result_filter.setCurrentIndex(0)
        self.test_count_label.setText('Python 已改变；请重新实际发现测试，取得此环境下的完整测试 ID。')
        self.result_summary.setText('Python 已改变 · 尚无当前环境的回归结果')
        self._clear_diagnostics()
        self.ai_button.setText('AI 验收计划…')
        self.session_label.setText('固定源码已保留 · Python 已改变，请重新发现测试')
        self.session_label.setToolTip('当前 Python：' + (self.python_edit.text().strip() or sys.executable)
                                      + '\n固定会话：' + str(self.state.get('directory') or ''))
        self.status_label.setText('环境已改变，请重新实际发现测试。两版固定源码、改动和历史运行保留，无需重新复制项目。')
        if self._selection_intent is not None:
            self._pending_selection = list(self._selection_intent) if self._selection_intent else None
            self._set_selection_review(self._selection_intent, '切换环境前的范围（待发现）')
        self._sync_controls()

    def _clear_content(self):
        self._clear_diagnostics()
        self._changing_checks = True
        for tree in (self.changes_tree, self.tests_tree, self.results_tree, self.history_tree):
            tree.clear()
        self._changing_checks = False
        for view in (self.diff_view, self.collection_log, self.result_details, self.version_logs):
            view.clear()
        self._change_by_path = {}
        self._report_items = {}
        self._presets = {}
        self._selection_review = None
        self._pending_selection = None
        self._selection_intent = None
        self._report_reference = None
        self.review_progress = None
        self.review_panel.hide()
        self.report_context_label.setText('尚无当前报告；普通刷新继续按当前勾选工作。')
        self.selection_review_panel.hide()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem('此项目的常用选择方案', None)
        self.preset_combo.blockSignals(False)
        self.test_code_view.clear()
        self._live_entries = {}
        for search in (self.change_search, self.test_search, self.result_search):
            search.clear()
        self.test_filter.setCurrentIndex(0)
        self.change_filter.setCurrentIndex(0)
        self.test_scope.blockSignals(True)
        self.test_scope.clear()
        self.test_scope.addItem('全部文件与目录', '')
        self.test_scope.blockSignals(False)
        self.result_filter.setCurrentIndex(0)
        self.change_count.setText('尚未冻结版本')
        self.test_count_label.setText('尚未实际发现测试；请先准备项目。')
        self.result_summary.setText('尚无项目回归报告')
        self.result_count_label.setText('尚无检查结果')
        self.history_label.setText('项目会话保存输入、已发现测试和每次运行记录；重开后可以继续。')
        self.ai_button.setText('AI 验收计划…')
        self._set_collection_log_visible(False)

    def _workspace_changed(self, *_):
        if not self.workspace_check.isChecked() and _ref_value(self.after_combo) == WORKTREE:
            self.after_combo.setEditText('HEAD')
        self.after_combo.setEnabled(not self.workspace_check.isChecked() and self.task is None)
        self._invalidate()

    def _collection_path_pending(self):
        return (self.state is not None
                and self.collection_path_edit.text().strip() != self.state.get('collection_path', ''))

    def _collection_path_changed(self):
        if not self._loading_inputs:
            self._sync_controls()

    def _sync_controls(self, *_):
        idle = self.task is None and not self._closing
        self.input_panel.setEnabled(idle)
        self.details_panel.setEnabled(idle)
        self.details_toggle.setEnabled(idle)
        self.after_combo.setEnabled(idle and not self.workspace_check.isChecked())
        for widget in (self.prepare_button, self.open_session_button, self.import_button,
                       self.refresh_sessions_button, self.new_button, self.session_combo,
                       self.timeout_spin, self.collection_path_edit):
            widget.setEnabled(idle)
        self.open_saved_button.setEnabled(idle and bool(self.session_combo.currentData()))
        self.refresh_workspace_button.setEnabled(idle and self.state is not None)
        self.selection_review_button.setEnabled(idle and self._selection_review is not None)
        self.discover_button.setEnabled(idle and self.state is not None)
        self.ai_button.setEnabled(idle and self.state is not None)
        self.cancel_button.setText('停止核对' if self._operation == 'review_progress' else '取消当前操作')
        self.cancel_button.setEnabled(self.task is not None)
        self.cancel_button.setVisible(self.task is not None)
        interrupted = self.report and any((self.report.get(side) or {}).get('status') in ('timed_out', 'cancelled', 'worker_error') for side in ('before', 'after'))
        self.progress_bar.setVisible(self.task is not None or bool(interrupted))
        self.tests_tree.setEnabled(idle)
        selected_count = len(self.selected_test_ids())
        self.prepare_button.setEnabled(idle and bool(self.repository_edit.text().strip())
                                       and bool(_ref_value(self.before_combo))
                                       and (self.workspace_check.isChecked() or bool(_ref_value(self.after_combo))))
        self.prepare_discover_button.setEnabled(self.prepare_button.isEnabled())
        saved = self.store.project_defaults(self.repository_edit.text())
        self.environment_hint.setVisible(bool(self.repository_edit.text().strip()))
        self.saved_options_button.setVisible(saved is not None and self.state is None)
        self.saved_options_button.setEnabled(idle)
        if saved is not None:
            self.saved_options_button.setToolTip('采用此项目上次使用的环境：' + (saved['python'] or '应用内置环境')
                + f"；每版／发现限时 {saved['timeout']:g} 秒。采用设置本身不执行代码。")
        diagnosis = collection_diagnosis(self.collection or {})
        recovery = self.state is not None and diagnosis['code'] != 'collected'
        for button in (self.problem_environment_button, self.problem_retry_button, self.problem_refresh_button):
            visible = recovery and (button is not self.problem_environment_button or diagnosis['code'] not in ('empty', 'timed_out', 'truncated'))
            button.setVisible(visible)
            button.setEnabled(idle and visible)
        self.run_button.setEnabled(idle and self.state is not None and self._pending_selection is None
                                   and not self._collection_path_pending()
                                   and 1 <= selected_count <= _MAX_SELECTED)
        self.run_button.setText(f'运行所选 {selected_count} 项' if selected_count else '运行所选测试')
        for widget in (self.select_visible_button, self.select_recommended_button, self.clear_tests_button):
            widget.setEnabled(idle and self.tests_tree.topLevelItemCount() > 0)
        for widget in (self.only_visible_button, self.selection_more_button, self.test_scope):
            widget.setEnabled(idle and self.tests_tree.topLevelItemCount() > 0)
        has_report = idle and self.report is not None
        all_count = len((self.report or {}).get('selected_tests') or [])
        attention_count = len(self._failed_ids())
        self.recheck_scope.setItemText(0, f'全部已选测试（{all_count} 项）')
        self.recheck_scope.setItemText(1, f'仅需处理项（{attention_count} 项）')
        recheck_count = all_count if self.recheck_scope.currentData() == 'all' else attention_count
        self.recheck_button.setText(f'复查修改（{recheck_count} 项）')
        self.quick_recheck_all_action.setText(f'复查并运行全部（{all_count} 项）')
        self.quick_recheck_attention_action.setText(f'复查并运行需处理项（{attention_count} 项）')
        self.quick_recheck_all_action.setEnabled(bool(has_report and self._report_reference and all_count))
        self.quick_recheck_attention_action.setEnabled(bool(has_report and self._report_reference and attention_count))
        self.recheck_all_action.setText(f'仅准备全部测试的复查（{all_count} 项）')
        self.recheck_attention_action.setText(f'仅准备需处理项的复查（{attention_count} 项）')
        self.recheck_all_action.setEnabled(bool(has_report and all_count))
        self.recheck_attention_action.setEnabled(bool(has_report and attention_count))
        self.recheck_button.setEnabled(bool(has_report and self._report_reference and all_count))
        self.recheck_scope.setEnabled(has_report)
        self.processing_history_button.setEnabled(bool(has_report and self._report_reference))
        has_source = bool((self.report or {}).get('review_source'))
        self.review_panel.setVisible(has_source and not self._live_entries)
        review_status = (self.review_progress or {}).get('status')
        self.review_retry_button.setVisible(review_status in ('unavailable', 'cancelled', 'pending'))
        self.review_retry_button.setEnabled(bool(has_report))
        self.review_relocate_button.setVisible(review_status == 'unavailable')
        self.review_relocate_button.setEnabled(bool(has_report))
        for button in (self.open_source_button, self.compare_source_button, self.relocate_source_button,
                       self.export_sources_action):
            button.setVisible(has_source)
            button.setEnabled(has_report and has_source)
        self.compare_source_button.setVisible(has_source and review_status == 'compared')
        for widget in (self.open_report_button, self.open_evidence_button,
                       self.check_report_button, self.export_button, self.replay_button, self.report_more_button):
            widget.setEnabled(has_report)
        same_project = bool(self.state and self.collection and self.report and
                            self.report.get('project_id') == self.state.get('project_id'))
        self.retry_failed_button.setEnabled(bool(has_report and same_project and self._failed_ids()))
        self.attention_button.setEnabled(bool(has_report and self._failed_ids()))
        self.retry_selected_button.setEnabled(bool(has_report and same_project and
                                                  any(not item.isHidden() for item in self.results_tree.selectedItems())))
        self.open_run_button.setEnabled(idle and bool(self.history_tree.currentItem()))
        self.open_session_folder_button.setEnabled(idle and self.state is not None)
        self.compare_runs_button.setEnabled(idle and len(self.history_tree.selectedItems()) == 2)
        has_preset = bool(self.preset_combo.currentData())
        self.preset_combo.setEnabled(idle and self.state is not None)
        self.preset_apply_button.setEnabled(idle and has_preset and bool(self.collection and self.collection.get('status') == 'collected'))
        self.preset_save_button.setEnabled(idle and self.state is not None and 1 <= selected_count <= _MAX_SELECTED)
        self.preset_delete_button.setEnabled(idle and self.state is not None and has_preset)
        self.result_more_button.setEnabled(idle)
        for name, action in self.result_actions.items():
            allowed = bool(has_report)
            if name in ('reopen_repair_preparation', 'reopen_repair_brief'):
                allowed = idle
            elif name == 'rerun_failed':
                allowed = self.retry_failed_button.isEnabled()
            elif name == 'rerun_selected':
                allowed = self.retry_selected_button.isEnabled()
            elif name in ('copy_result_ids', 'copy_entry_diagnosis', 'preview_selected_test'):
                allowed = bool(has_report and self._selected_result())
            elif name == 'inspect_result_environment':
                allowed = bool(has_report and self.state and self.report.get('project_id') == self.state.get('project_id'))
            action.setEnabled(allowed)
            action.setToolTip('请先打开对应的完整报告／会话并选择可用项。' if not allowed else action.text())
        self._sync_failure_actions()
        self._sync_diagnostic_actions()
        self._sync_stage_actions()
        self.project_menu_button.setEnabled(idle)
        for item, action in self.project_menu_actions:
            action.setEnabled(item.isEnabled())

    def _start(self, action, callback, message, *, operation='', runtime_repository=None, runtime_values=None):
        if self.busy or self._closing:
            return False
        self._save_view_state()
        generation = self._generation
        self._operation = operation
        if operation != 'review_progress':
            self._clear_problem()
            self.run_progress_panel.hide()
        self._after_finished = None
        self._last_progress = []
        self.status_label.setText(message)
        repository = runtime_repository or (self.state or {}).get('repository') or self.repository_edit.text()
        task = self.task = Task(action, self, runtime_values=runtime_values if runtime_values is not None else self.store.local_runtime_values(repository))

        def succeeded(result):
            if generation != self._generation:
                self.status_label.setText('操作已保存，但输入已改变。请重开对应会话查看，或重新准备当前输入。')
                return
            try:
                callback(result)
            except Exception as exc:
                self._failed(str(exc) or type(exc).__name__)

        task.succeeded.connect(succeeded, Qt.ConnectionType.QueuedConnection)
        task.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        task.cancelled.connect(self._cancelled, Qt.ConnectionType.QueuedConnection)
        task.progress.connect(self._progress, Qt.ConnectionType.QueuedConnection)
        task.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        self.progress_bar.setRange(0, 0)
        self._sync_controls()
        self.busyChanged.emit(True)
        task.start()
        return True

    def _finished(self):
        # Keep this local alive through cleanup.  ``succeeded`` can run a modal
        # dialog/event loop, so ownership must never end in that signal handler.
        task = self.task
        if task is None:
            return
        self.task = None
        task.deleteLater()
        total = len((self.report or {}).get('selected_tests') or [])
        interrupted = self.report and any((self.report.get(side) or {}).get('status') in ('timed_out', 'cancelled', 'worker_error') for side in ('before', 'after'))
        if interrupted and total:
            self.progress_bar.setRange(0, total * 2)
            self.progress_bar.setValue(sum(((self.report.get(side) or {}).get('progress') or {}).get('completed', 0) for side in ('before', 'after')))
        else:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1)
        self._sync_controls()
        after = self._after_finished
        self._after_finished = None
        if after is not None and not self._closing:
            QTimer.singleShot(0, self, after)
        self.busyChanged.emit(self.busy)

    def _failed(self, error):
        self.status_label.setText('未完成：' + _short(error, 3000))
        self.run_progress_panel.hide()
        self._show_problem(error)
        if self._live_entries:
            for entry in self._live_entries.values():
                entry['status'] = 'inconclusive'
            self._render_results(list(self._live_entries.values()))
            self.result_summary.setText('运行未完成 · 请查看错误，不能视为回归通过')
            self.version_logs.setPlainText(_short(error) + '\n\n' + '\n'.join(self._last_progress))
        self._update_next_step()

    def _cancelled(self):
        self._refresh_catalog()
        self.status_label.setText('已取消；已保存的会话、历史报告和运行材料仍可查看。')
        if self._live_entries:
            for entry in self._live_entries.values():
                entry['status'] = 'inconclusive'
            self._render_results(list(self._live_entries.values()))
            self.result_summary.setText('已取消 · 未产生新的完整回归结论')

    def _progress(self, phase, payload):
        if phase == 'project_before' and isinstance(payload, dict) and payload.get('selected_tests') and not self._live_entries:
            self.report = None
            self._report_reference = None
            self._live_entries = {identity: {'test_id': identity, 'status': 'running',
                'before': {'status': 'not_run'}, 'after': {'status': 'not_run'}} for identity in payload['selected_tests']}
            self._render_results(list(self._live_entries.values()), live=True)
            self.tabs.setCurrentIndex(2)
        if phase == 'project_progress' and isinstance(payload, dict):
            self._show_live_progress(payload)
            return
        names = {'history_round': '正在核对多轮来源…', 'project_refresh': '正在准备当前修改的复查…', 'project_collect': '正在从固定快照实际发现测试…',
                 'project_before': '正在运行基线版本的所选测试…',
                 'project_after': '正在运行变更后版本的同一组选定测试…',
                 'project_done': '运行结束，正在保存逐测试结果和报告…',
                 'before': '正在运行基线版本…', 'after': '正在运行变更后版本…'}
        text = names.get(str(phase), '正在处理项目：' + str(phase))
        if isinstance(payload, dict):
            note = payload.get('message') or payload.get('notice')
            if isinstance(note, str) and note:
                text += ' ' + _short(note, 260)
            total, completed = payload.get('total'), payload.get('completed')
            if type(total) is int and type(completed) is int and total > 0:
                self.progress_bar.setRange(0, total)
                self.progress_bar.setValue(max(0, min(completed, total)))
            side = ('before' if 'before' in str(phase) else
                    'after' if 'after' in str(phase) else None)
            if side and isinstance(payload.get('tests'), dict) and self._live_entries:
                for test_id, outcome in payload['tests'].items():
                    if test_id in self._live_entries and isinstance(outcome, dict):
                        self._live_entries[test_id][side] = outcome
                self._render_results(list(self._live_entries.values()), live=True)
        self._last_progress.append(text)
        self._last_progress = self._last_progress[-100:]
        self.status_label.setText(text)

    def _show_live_progress(self, payload):
        text = progress_text(payload)
        self.run_progress_label.setText(text)
        self.run_current_test.setText(payload.get('current_test') or ('正在保存本版结果…' if payload.get('session_finished') else '等待下一条测试事件…'))
        self.run_progress_panel.show()
        self.status_label.setText(text)
        side, total = payload.get('side'), payload.get('total', 0)
        if side in ('before', 'after') and total:
            self.result_summary.setText('正在检查 · 正式结论将在两版结束后生成')
            self.progress_bar.setRange(0, total * 2)
            self.progress_bar.setValue((total if side == 'after' else 0) + payload.get('completed', 0))
            if not self._live_entries:
                return
            self.results_tree.setUpdatesEnabled(False)
            for identity, outcome in payload.get('tests', {}).items():
                entry = self._live_entries.get(identity)
                item = self._report_items.get(identity)
                if entry is None or item is None:
                    continue
                entry[side] = outcome
                item.setData(0, _ROLE, entry)
                item.setText(2 if side == 'before' else 3, _STATUS.get(outcome.get('status'), '运行中'))
                item.setText(4, _duration(entry['before'].get('duration_seconds')) + ' / ' + _duration(entry['after'].get('duration_seconds')))
                item.setText(1, ('基线' if side == 'before' else '候选') + '已记录')
            current = self._report_items.get(payload.get('current_test'))
            if current is not None:
                current.setText(1, ('基线' if side == 'before' else '候选') + ' · ' + PHASE_LABELS.get(payload.get('current_phase'), '运行中'))
            self.results_tree.setUpdatesEnabled(True)
            self._filter_results()
            if self.results_tree.currentItem() is not None:
                self._show_result_detail(self.results_tree.currentItem())

    def cancel_operation(self):
        if self.task is not None:
            accepted = self.task.control.cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText('正在停止核对复查进展；本次执行报告保留。' if accepted and self._operation == 'review_progress' else
                                      '正在取消并清理测试进程，请等待操作结束…' if accepted else
                                      '测试已经结束，正在保存结果；请等待保存完成。')

    def request_close(self):
        self._save_view_state()
        """Return false while a worker is alive, requesting cancellation first."""
        if self.task is not None:
            self._closing = True
            self.cancel_operation()
            return False
        for dialog in (self.ai_dialog, self.environment_dialog, self.failure_dialog, self.processing_dialog):
            if dialog is not None and dialog.isVisible():
                if dialog is self.processing_dialog:
                    dialog.reject()
                    if dialog.isVisible():
                        self._closing = dialog._closing
                        return False
                    continue
                for child in reversed(dialog.findChildren(QDialog)):
                    task = getattr(child, 'task', None)
                    if task is not None:
                        task.control.cancel()
                    if child.isVisible():
                        child.reject()
                task = getattr(dialog, 'task', None)
                if task is not None:
                    task.control.cancel()
                dialog.reject()
                if dialog.isVisible() or self.busy:
                    self._closing = True
                    return False
        return not self.busy

    def closeEvent(self, event):
        if self.request_close():
            event.accept()
        else:
            event.ignore()

    def new_project(self):
        if self.busy:
            return
        self._loading_inputs = True
        self.repository_edit.clear()
        self.before_combo.clear()
        self.before_combo.setEditText('HEAD')
        self.after_combo.clear()
        self.after_combo.setEditText('HEAD')
        self.workspace_check.setChecked(True)
        self.title_edit.clear()
        self.issue_edit.clear()
        self.collection_path_edit.clear()
        self.python_edit.setText(str(self.store.settings.get('python') or ''))
        self._set_python_candidates([])
        self.timeout_spin.setValue(120)
        self._loading_inputs = False
        self._invalidate()
        self.tabs.setCurrentIndex(0)
        self.session_combo.setCurrentIndex(0)
        self.session_label.setText('新项目：选择仓库、版本和环境，再准备测试清单。')
        self.status_label.clear()
        self.repository_edit.setFocus()

    def new_demo(self):
        if self.busy:
            return False
        from .project_demo import create_project_demo
        self._invalidate()
        def action(control):
            state = create_project_demo(self.project_root, control=control)
            control.check()
            return state
        def shown(state):
            self._prepared(state)
            self.status_label.setText('购物车教学项目已创建，尚未执行测试。点击“发现测试”后选择运行，可查看通过、改善、回归和受阻结果。')
        return self._start(action, shown, '正在创建独立的购物车教学项目与固定会话…', operation='prepare')

    def _choose_repository(self):
        path = QFileDialog.getExistingDirectory(self, '选择本地 Git 项目', self.repository_edit.text())
        if path:
            self.repository_edit.setText(path)
            self.read_commits()

    def _choose_python(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择可信的项目 Python 解释器',
                    self.python_edit.text(), options=QFileDialog.Option.DontResolveSymlinks)
        if path:
            self.python_edit.setText(path)

    def inspect_environment(self, *, continue_collection=False):
        if self.busy or not self.repository_edit.text().strip():
            self.status_label.setText('请先选择本地项目目录。')
            return
        from .desktop_environment import ProjectEnvironmentDialog
        try:
            project = (Path(self.state['directory']) / 'inputs/after' if self.state else
                       Path(self.repository_edit.text().strip()))
            repository = str(Path(self.repository_edit.text().strip()).expanduser().resolve())
            preferences = dict(self.store.settings.get('project_environment_groups') or {})
            self.environment_dialog = ProjectEnvironmentDialog(
                project, self.python_edit.text().strip(),
                work_root=self.project_root.parent, parent=self, selected_groups=preferences.get(repository, []),
                repository=repository, can_refresh=bool(self.state), local_values=self.store.local_runtime_values(repository),
                can_collect=bool(continue_collection and self.state))
            self.environment_dialog.exec()
            self.store.set_runtime_values(repository, self.environment_dialog.runtime_values)
            chosen = self.environment_dialog.result_data.get('python')
            if chosen:
                self.python_edit.setText('' if chosen == sys.executable else str(chosen))
            selected_groups = self.environment_dialog.selected_groups
            if preferences.get(repository) != selected_groups:
                preferences[repository] = selected_groups
                self.store.settings['project_environment_groups'] = dict(list(preferences.items())[-200:])
                self.store.save()
            if self.environment_dialog.refresh_requested:
                QTimer.singleShot(0, self, self.refresh_workspace)
            elif self.environment_dialog.result_data.get('collect_requested'):
                QTimer.singleShot(0, self, self.discover)
            elif self.environment_dialog.configuration_changed:
                message = ('原项目配置已更新；当前会话仍保留旧快照。刷新当前工作区后用于新的检查。'
                           if self.environment_dialog.configuration_requires_refresh else '配置操作已完成，当前检查来源的配置与原项目一致。')
                self.status_label.setText(self.environment_dialog.configuration_warning or message)
        except (OSError, ValueError) as exc:
            self._failed(str(exc))

    def read_commits(self):
        if self.busy:
            return
        repository = self.repository_edit.text().strip()
        if not repository:
            self.status_label.setText('请先选择本地 Git 仓库。')
            return
        from .desktop_creation import _commits
        from .project_environment import candidate_pythons
        def action(control):
            root, commits = _commits(repository)
            control.check()
            return root, commits, candidate_pythons(root)
        def shown(result):
            root, commits, candidates = result
            self._loading_inputs = True
            self.repository_edit.setText(root)
            for combo in (self.before_combo, self.after_combo):
                original = _ref_value(combo)
                combo.clear()
                for commit, date, subject in commits:
                    combo.addItem(f'{commit[:10]} · {date} · {subject[:65]}', commit)
                    combo.setItemData(combo.count() - 1, f'{commit[:12]} · {date} · {subject}', Qt.ItemDataRole.ToolTipRole)
                combo.setEditText(original or commits[0][0])
            self._set_python_candidates(candidates)
            self._loading_inputs = False
            self._invalidate()
            self.status_label.setText(f'已读取 {len(commits)} 个提交')
        self._start(action, shown, '正在读取本地提交历史与候选环境路径…')

    def use_saved_options(self):
        if self.busy or self.state is not None:
            return
        saved = self.store.project_defaults(self.repository_edit.text())
        if saved is None:
            return
        self.python_edit.setText(saved['python'])
        self.timeout_spin.setValue(saved['timeout'])
        self.status_label.setText('已采用此项目上次的环境与时限；点击“准备并发现测试”后才执行收集。')

    def _remember_project_options(self):
        if self.state is None:
            return
        try:
            self.store.remember_project_defaults(self.state['repository'], self.python_edit.text().strip(), self.timeout_spin.value())
        except OSError as exc:
            self.status_label.setText(self.status_label.text() + '\n本次操作已完成，但项目偏好未能保存：' + str(exc))

    def prepare(self, *, discover_tests=False):
        if self.busy:
            return False
        repository = self.repository_edit.text().strip()
        before = _ref_value(self.before_combo)
        after = WORKTREE if self.workspace_check.isChecked() else _ref_value(self.after_combo)
        if not repository or not before or not after:
            self.status_label.setText('请选择 Git 项目，并填写基线和变更后版本。')
            return False
        python = self.python_edit.text().strip() or sys.executable
        title, issue = self.title_edit.text().strip() or None, self.issue_edit.toPlainText().strip()
        output = self.project_root
        timeout = self.timeout_spin.value()
        collection_path = self.collection_path_edit.text().strip()
        self._invalidate()
        def action(control):
            control.check()
            state = prepare_project(repository, before, after, output, python_executable=python,
                                    title=title, issue=issue, collection_path=collection_path)
            if not discover_tests:
                control.check()
                return {'state': state}
            return self._collect_prepared(state, python, timeout, control)
        def shown(result):
            self._prepared(result['state'])
            if discover_tests:
                self._show_prepared_collection(result)
            self._remember_project_options()
        return self._start(action, shown, '正在冻结两版代码并发现测试…' if discover_tests else
                           '正在冻结两版代码与环境选择，并整理全部改动…',
                           operation='prepare_collect' if discover_tests else 'prepare')

    @staticmethod
    def _collect_prepared(state, python, timeout, control, *, collection_path=None):
        try:
            control.check()
            collect_project(state['directory'], python_executable=python, timeout=timeout, control=control,
                            collection_path=collection_path)
            return {'state': read_project(state['directory'])}
        except RunCancelled:
            return {'state': state, 'cancelled': True}
        except (OSError, ValueError) as exc:
            return {'state': state, 'error': str(exc)}

    def _show_prepared_collection(self, result):
        self.tabs.setCurrentIndex(1)
        if result.get('cancelled'):
            self.status_label.setText('测试发现已取消；冻结快照已保留，可直接重试发现，无需重新冻结。')
        elif result.get('error'):
            self._failed(result['error'])
        else:
            self.status_label.setText(collection_diagnosis(self.collection or {})['summary'])

    def _prepared(self, state):
        self._apply_state(state)
        self.tabs.setCurrentIndex(0)
        self.status_label.setText('版本已准备')
        self._refresh_catalog()

    def refresh_workspace(self, *, discover_tests=False):
        if self.busy or not self.state:
            return False
        directory = self.state['directory']
        selected = (list(self._pending_selection) if self._pending_selection is not None else
                    self.selected_test_ids() if self.collection and self.collection.get('status') == 'collected'
                    else self._selection_intent)
        python = self.python_edit.text().strip() or sys.executable
        output = self.project_root
        reference = dict(self._report_reference) if (self._report_reference and self.report
            and self.report.get('project_id') == self.state.get('project_id')) else None
        timeout = self.timeout_spin.value()
        collection_path = self.collection_path_edit.text().strip()
        def action(control):
            refresh_control = control.child(progress=control.progress) if discover_tests else control
            if reference:
                state = refresh_from_displayed_report(directory, output, reference, selected_ids=selected,
                    python_executable=python, control=refresh_control)
            else:
                state = refresh_project(directory, output, selected_ids=selected,
                                        python_executable=python, control=refresh_control)
            if discover_tests:
                return self._collect_prepared(state, python, timeout, control, collection_path=collection_path)
            return {'state': state}
        def shown(result):
            self._prepared(result['state'])
            if discover_tests:
                self._show_prepared_collection(result)
            else:
                self.status_label.setText('当前工作区已另存为新会话，基线保持原提交。点击“发现测试”后核对带入的选择；旧会话和报告仍可重开。')
        return self._start(action, shown, '正在按原固定基线另存当前工作区…', operation='refresh')

    def _recheck_with_scope(self, scope):
        self.recheck_scope.setCurrentIndex(self.recheck_scope.findData(scope))
        self.recheck_report()

    def recheck_report(self):
        if self.busy or not self.report or not self._report_reference:
            return False
        reference = dict(self._report_reference)
        scope = self.recheck_scope.currentData()
        python = self.python_edit.text().strip() or (self.report.get('runtime') or {}).get('executable')
        output = self.project_root
        def action(control):
            return refresh_from_report(reference['report_dir'], output, scope=scope,
                                       python_executable=python, expected_reference=reference, control=control)
        def shown(state):
            self._prepared(state)
            source = state['review_source']
            self.status_label.setText(f'已从指定运行 {source["run_id"]} 准备复查会话，带入 {len(source["selected_tests"])} 项。'
                                      '基线保持报告保存的提交；请分别点击发现测试和正式运行。原报告保留。')
        return self._start(action, shown, '正在核对这份报告的身份和证据，并准备当前工作区复查…', operation='recheck')

    def quick_recheck(self, scope='all', selected_ids=None):
        if self.busy or not self.report or not self._report_reference:
            return False
        from .project_refresh import recheck_and_run
        reference = dict(self._report_reference)
        python = self.python_edit.text().strip() or (self.report.get('runtime') or {}).get('executable')
        timeout = self.timeout_spin.value()
        def action(control):
            return recheck_and_run(reference['report_dir'], self.project_root, self.report_root,
                scope=scope, python_executable=python, timeout=timeout, selected_ids=selected_ids,
                expected_reference=reference, control=control)
        def shown(result):
            self._apply_state(result['state'])
            if result['report'] is not None:
                self._apply_report(result['report'])
                self._show_runs()
                self.tabs.setCurrentIndex(2)
                self.status_label.setText(result['notice'])
            else:
                self.tabs.setCurrentIndex(1)
                self.status_label.setText(result['notice'])
                if (result.get('collection') or {}).get('status') == 'collected':
                    self.selection_review_label.setText(result['notice'])
                    self.selection_review_panel.show()
                else:
                    self._show_problem(result['notice'])
            self._refresh_catalog()
        return self._start(action, shown,
            '正在冻结当前修改、发现并核对原测试范围；范围完整且检查内容未变时继续执行…', operation='recheck_run')

    def _queue_review_progress(self):
        self.review_progress = None
        if not self.report or not self.report.get('review_source'):
            self.review_panel.hide()
            return
        self.review_summary.setText('正在核对本轮相对来源报告的变化…')
        self.review_scope.setText('')
        self.review_panel.show()
        expected = dict(self._report_reference)
        def load():
            if self._closing or self._report_reference != expected:
                return
            if self.busy:
                self._show_review_progress({'status': 'pending', 'comparison': None,
                    'summary': '复查进展尚未核对，可在当前操作结束后重新核对。', 'scope_summary': ''})
                return
            self.compare_source_report(show_dialog=False)
        if self.task is not None:
            self._after_finished = load
        else:
            QTimer.singleShot(0, self, load)

    def _show_review_progress(self, result):
        self.review_progress = result
        self.review_summary.setText(('与复查前相比：' if result['status'] == 'compared' else '') + result['summary'])
        self.review_scope.setText(result['scope_summary'])
        comparison = result.get('comparison')
        if comparison:
            self.review_scope.setToolTip('来源运行：' + comparison['before']['run_id']
                + '\n本轮运行：' + comparison['after']['run_id'])
        else:
            self.review_scope.setToolTip('')
        self._sync_controls()

    def _show_review_details(self):
        if self.busy:
            return
        comparison = (self.review_progress or {}).get('comparison')
        if comparison:
            self._show_comparison(comparison)
        else:
            self.compare_source_report()

    def compare_source_report(self, source_directory=None, *, show_dialog=True):
        if self.busy or not self.report or not self.report.get('review_source'):
            return False
        if isinstance(source_directory, bool):
            source_directory = None
        reference = dict(self.report['review_source'])
        directory = self.report['report_dir']
        expected = dict(self._report_reference)
        locations = self.store.settings.get('project_report_locations') or {}
        chosen = source_directory or source_report_directory(self.report, reference, locations.get(reference['report_sha256']))
        self.review_progress = None
        self.review_summary.setText('正在核对本轮相对来源报告的变化…')
        self.review_scope.setText('')
        def action(control):
            return read_recheck_progress(directory, source_directory=chosen, expected_report=expected, control=control)
        def shown(result):
            if self._report_reference != expected:
                return
            self._show_review_progress(result)
            warning = ''
            if result['status'] == 'compared' and source_directory:
                saved = dict(self.store.settings.get('project_report_locations') or {})
                saved[reference['report_sha256']] = str(Path(chosen).resolve())
                self.store.settings['project_report_locations'] = dict(list(saved.items())[-200:])
                try:
                    self.store.save()
                except OSError as exc:
                    warning = '比较完成，但来源位置偏好保存失败：' + str(exc)
            if result['status'] == 'compared' and show_dialog:
                self._show_comparison(result['comparison'])
            self.status_label.setText(warning or '本次执行报告已保留。' + result['summary'])
        return self._start(action, shown, '正在核对复查来源与执行证据，不重新运行测试…', operation='review_progress')

    def open_source_report(self):
        if self.busy or not self.report or not self.report.get('review_source'):
            return False
        reference = dict(self.report['review_source'])
        locations = self.store.settings.get('project_report_locations') or {}
        location = source_report_directory(self.report, reference, locations.get(reference['report_sha256']))
        def action(control):
            control.check()
            return resolve_report_reference(reference, location)
        return self._start(action, lambda source: self._open_local(Path(source['report_dir']) / 'report.html'),
                           '正在核对原来源报告的身份和证据…', operation='compare_source')

    def relocate_source_report(self):
        if self.busy or not self.report or not self.report.get('review_source'):
            return False
        path = QFileDialog.getExistingDirectory(self, '重新定位原来源报告（须匹配会话、运行 ID 和报告指纹）', str(self.report_root))
        return self.compare_source_report(path, show_dialog=False) if path else False

    def relocate_current_report(self):
        if self.busy or not self._report_reference:
            return False
        reference = dict(self._report_reference)
        path = QFileDialog.getExistingDirectory(self, '重新定位当前所选报告（须匹配同一运行与报告指纹）', str(self.report_root))
        if not path:
            return False
        def action(control):
            control.check()
            return resolve_report_reference(reference, path)
        def shown(report):
            self._apply_report(report)
            locations = dict(self.store.settings.get('project_report_locations') or {})
            locations[reference['report_sha256']] = report['report_dir']
            self.store.settings['project_report_locations'] = dict(list(locations.items())[-200:])
            try:
                self.store.save()
            except OSError as exc:
                self.status_label.setText('报告已核对并打开，但位置偏好保存失败：' + str(exc))
        return self._start(action, shown, '正在核对移动后报告的原身份与证据…', operation='relocate_report')

    def _apply_state(self, state):
        if not isinstance(state, dict) or state.get('kind') != 'project_session':
            raise ValueError('引擎返回的项目会话无效。')
        self._save_view_state()
        self._view_dirty = False
        self._restoring_view = True
        self._loading_inputs = True
        try:
            self.repository_edit.setText(str(state.get('repository') or ''))
            self.before_combo.setEditText(str(state.get('before_commit') or ''))
            self.after_combo.setEditText('HEAD' if state.get('after_commit') == WORKTREE
                                         else str(state.get('after_commit') or ''))
            self.workspace_check.setChecked(state.get('after_commit') == WORKTREE)
            runtime = state.get('runtime') or {}
            executable = runtime.get('executable') or ''
            self.python_edit.setText('' if executable == sys.executable else str(executable))
            self.title_edit.setText(str(state.get('title') or ''))
            self.issue_edit.setPlainText(str(state.get('issue') or ''))
            self.collection_path_edit.setText(state.get('collection_path', ''))
        finally:
            self._loading_inputs = False
        self._clear_content()
        self.state = state
        self.report = None
        source_run = state.get('review_source') or {}
        if source_run:
            self.report_context_label.setText('本会话复查来源：' + str(source_run.get('run_id'))
                + ' · 来源会话：' + str(source_run.get('project_id')) + '；尚未生成本轮报告。')
        self.collection = state.get('collection') if isinstance(state.get('collection'), dict) else None
        self._ai_plan_path = None
        self._update_session_label()
        self._show_changes()
        if self.collection is not None:
            self._show_collection(self.collection)
        else:
            static = state.get('static_tests') or []
            self.test_count_label.setText(f'静态候选 {len(static)} 个；点击“实际发现测试”取得完整参数化 ID 后选择。')
            requested = requested_selection(state)
            self._selection_intent = requested
            if requested is not None:
                self._pending_selection = list(requested) if requested else None
                self._set_selection_review(requested, '待发现的原范围')
            else:
                self.selection_review_label.setText('未指定继承范围；发现后可按推荐或手动勾选，不等同于主动清空。')
                self.selection_review_panel.show()
        self._show_runs()
        self._refresh_presets()
        self._restore_view_state()
        self._restoring_view = False
        self._set_inputs_expanded(False)
        self._sync_controls()
        self.store.settings['last_project'] = state['directory']
        self.store.settings['last_project_title'] = state.get('title', '')
        try:
            self.store.save()
        except OSError as exc:
            self.status_label.setText('项目已打开，但最近项目记录未能保存：' + str(exc))
        self.projectOpened.emit(state['directory'], state.get('title', ''))

    def _update_session_label(self):
        state = self.state
        runtime = state.get('runtime') or {}
        before = str(state.get('before_commit') or '')[:12]
        after = ('当前工作区快照' if state.get('after_commit') == WORKTREE
                 else str(state.get('after_commit') or '')[:12])
        self.session_label.setText(f'固定输入：{before} → {after} · Python {runtime.get("python_version") or "已选定"}')
        if state.get('refresh_origin'):
            self.session_label.setText(self.session_label.text() + ' · 从旧会话刷新')
        self.session_label.setToolTip(_format_json({key: state.get(key) for key in
                                    ('directory', 'project_id', 'source_sha256', 'source_mode_sha256', 'runtime', 'refresh_origin', 'review_source')}, 5000))

    def _show_changes(self):
        self.changes_tree.clear()
        changes = self.state.get('changes') or {}
        self._change_by_path = {str(item.get('path')): item for item in changes.get('files', [])
                                if isinstance(item, dict)}
        files = self.state.get('changed_files') or []
        for change in files:
            change = {'path': change, 'kind': 'modified'} if isinstance(change, str) else change
            if not isinstance(change, dict) or not change.get('path'):
                continue
            path, kind = str(change['path']), str(change.get('kind') or 'modified')
            item = QTreeWidgetItem([path, _KIND.get(kind, kind)])
            item.setData(0, _ROLE, {'path': path, 'kind': kind})
            item.setToolTip(0, path)
            self.changes_tree.addTopLevelItem(item)
        self.change_count.setText(f'全部 {self.changes_tree.topLevelItemCount()} 个改动文件 · 差异预览有界')
        self._filter_changes()
        if self.changes_tree.topLevelItemCount():
            self.changes_tree.setCurrentItem(self.changes_tree.topLevelItem(0))
        else:
            self.diff_view.setPlainText('两版文件内容和普通权限没有差异。仍可发现并运行项目测试。')

    def _filter_changes(self, *_):
        needle = self.change_search.text().strip().casefold()
        mode = self.change_filter.currentData()
        visible = 0
        for index in range(self.changes_tree.topLevelItemCount()):
            item = self.changes_tree.topLevelItem(index)
            path = PurePosixPath(item.text(0))
            is_test = (path.name == 'conftest.py' or path.name.startswith('test_')
                       or path.name.endswith('_test.py') or any(part in ('test', 'tests') for part in path.parts))
            config = path.suffix in ('.toml', '.ini', '.cfg', '.yaml', '.yml', '.json') or path.name.startswith('requirements')
            matches = (mode == 'all' or (mode == 'tests' and is_test)
                       or (mode == 'python' and path.suffix == '.py' and not is_test)
                       or (mode == 'config' and config)
                       or (mode == 'docs' and path.suffix in ('.md', '.rst', '.txt') and not config))
            show = matches and needle in item.text(0).casefold()
            item.setHidden(not show)
            visible += show
        if self.state:
            self.change_count.setText(f'可见 {visible} / {self.changes_tree.topLevelItemCount()} 个改动文件')

    def _show_diff(self, item, *_):
        if not item or not self.state:
            return
        data = item.data(0, _ROLE)
        path = data['path']
        preview = self._change_by_path.get(path)
        if preview is None:
            try:
                preview = self._snapshot_diff(path)
            except (OSError, ValueError) as exc:
                preview = {'path': path, 'diff': '', 'note': '无法读取固定快照：' + str(exc)}
            self._change_by_path[path] = preview
        lines = [path + ' · ' + _KIND.get(data['kind'], data['kind'])]
        if preview.get('note'):
            lines.extend(['', str(preview['note'])])
        lines.extend(['', str(preview.get('diff') or '没有可显示的文本差异。')])
        self.diff_view.setPlainText('\n'.join(lines))

    def _snapshot_diff(self, name):
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise ValueError('快照内文件路径无效。')
        root = Path(self.state['directory']) / 'inputs'
        files, modes = [], []
        for side in ('before', 'after'):
            base = (root / side).resolve()
            source = base.joinpath(*relative.parts)
            if not source.resolve().is_relative_to(base) or source.is_symlink():
                raise ValueError('文件超出固定快照或是符号链接。')
            if source.exists() and not source.is_file():
                raise ValueError('改动路径不是普通文件。')
            files.append(source if source.is_file() else None)
            modes.append(stat.S_IMODE(source.stat().st_mode) & 0o777 if source.is_file() else None)
        notes = []
        if all(mode is not None for mode in modes) and modes[0] != modes[1]:
            notes.append(f'文件权限变化：{modes[0]:04o} → {modes[1]:04o}。')
        if any(source and source.stat().st_size > 128 * 1024 for source in files):
            return {'note': ' '.join(notes + ['文件超过 128 KiB，省略文本预览；完整文件在固定快照中。']), 'diff': ''}
        try:
            texts = [source.read_text(encoding='utf-8') if source else '' for source in files]
            if any('\x00' in text for text in texts):
                raise ValueError('binary data')
        except (UnicodeError, ValueError):
            return {'note': ' '.join(notes + ['二进制或非 UTF-8 文件，省略文本预览。']), 'diff': ''}
        lines = list(islice(difflib.unified_diff(texts[0].splitlines(), texts[1].splitlines(),
                     fromfile='before/' + name, tofile='after/' + name, lineterm=''), 201))
        diff = '\n'.join(lines[:200])
        if len(lines) > 200 or len(diff) > 20000:
            notes.append('差异预览已截断；完整文件在固定快照中。')
        return {'note': ' '.join(notes), 'diff': diff[:20000]}

    def discover(self):
        if self.busy or not self.state:
            if not self.state:
                self.status_label.setText('请先冻结项目版本。')
            return False
        directory = self.state['directory']
        python, timeout = self.python_edit.text().strip() or sys.executable, self.timeout_spin.value()
        collection_path = self.collection_path_edit.text().strip()
        def action(control):
            collection = collect_project(directory, python_executable=python, timeout=timeout,
                                         control=control, collection_path=collection_path)
            return {'collection': collection, 'state': read_project(directory)}
        def shown(result):
            self.state = result['state']
            self.collection = result['collection']
            self.collection_path_edit.setText(self.state.get('collection_path', ''))
            self._update_session_label()
            self._show_collection(self.collection)
            self._restore_view_state()
            self.tabs.setCurrentIndex(1)
            self.status_label.setText(collection_diagnosis(self.collection)['summary'])
            self._remember_project_options()
        return self._start(action, shown, '正在实际发现测试（会执行可信项目的测试收集代码）…', operation='collect')

    def _recommendation(self, test_id):
        sources = ((self.collection or {}).get('recommendations') or {},
                   (self.state or {}).get('recommendations') or {})
        for mapping in sources:
            if not isinstance(mapping, dict):
                continue
            for candidate in (test_id, test_id.split('[', 1)[0], test_id.split('::', 1)[0]):
                if mapping.get(candidate):
                    return str(mapping[candidate])
        return ''

    def _show_collection(self, collection):
        had_tests = self.tests_tree.topLevelItemCount() > 0
        prior = list(self._pending_selection) if self._pending_selection is not None else self.selected_test_ids()
        tests = collection.get('tests') or []
        tests = list(dict.fromkeys(str(test) for test in tests if isinstance(test, str)))[:10000]
        recommended = {test for test in tests if self._recommendation(test)}
        requested = prior if had_tests else self._selection_intent
        if requested is None:
            requested = requested_selection(self.state)
        if self._pending_selection is not None:
            requested = list(self._pending_selection)
        if requested is not None:
            review = reconcile_selection(requested, tests, collection_complete=collection.get('status') == 'collected'
                and not (collection.get('partial') or collection.get('truncated')))
            self._selection_intent = requested
            self._pending_selection = requested if review['requires_confirmation'] else None
            selected = set() if self._pending_selection is not None else set(review['matched_tests'])
        else:
            selected = (set() if collection.get('partial') or collection.get('truncated') else
                        recommended if 0 < len(recommended) <= _MAX_SELECTED else
                        set(tests) if len(tests) <= _MAX_SELECTED else set())
        previous_scope = self.test_scope.currentData()
        self.test_scope.blockSignals(True)
        self.test_scope.clear()
        self.test_scope.addItem('全部文件与目录', '')
        files = Counter(test.split('::', 1)[0] for test in tests)
        directories = Counter()
        for name, count in files.items():
            for directory in PurePosixPath(name).parents:
                if str(directory) != '.':
                    directories[str(directory) + '/'] += count
        for name, count in sorted(directories.items()):
            self.test_scope.addItem(f'目录 · {name}（{count}）', 'dir:' + name)
        for name, count in sorted(files.items()):
            self.test_scope.addItem(f'文件 · {name}（{count}）', 'file:' + name)
        self.test_scope.setCurrentIndex(max(0, self.test_scope.findData(previous_scope)))
        self.test_scope.blockSignals(False)
        self._changing_checks = True
        self.tests_tree.setUpdatesEnabled(False)
        self.tests_tree.clear()
        for test_id in tests:
            reason = self._recommendation(test_id)
            item = QTreeWidgetItem([test_id, reason])
            item.setData(0, _ROLE, test_id)
            item.setData(1, _ROLE, bool(reason))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if test_id in selected else Qt.CheckState.Unchecked)
            item.setToolTip(0, test_id)
            item.setToolTip(1, reason or '没有直接文件关联；仍可选择运行。')
            self.tests_tree.addTopLevelItem(item)
        self.tests_tree.setUpdatesEnabled(True)
        self._changing_checks = False
        errors = collection.get('collection_errors') or []
        from .pytest_profile import describe_pytest_profile
        diagnosis = collection_diagnosis(collection)
        pieces = [diagnosis['summary'],
                  describe_pytest_profile(collection.get('pytest_profile'))]
        if collection.get('truncated'):
            pieces.append('发现列表已达 10000 个目标的上限。请检查收集范围。')
        if errors:
            pieces.extend(['收集错误', _format_json(errors, 12000)])
        pieces.append(_short(collection.get('log_excerpt'), 30000))
        self.collection_log.setPlainText('\n\n'.join(part for part in pieces if part))
        self._set_collection_log_visible(bool(errors or collection.get('status') != 'collected'))
        if diagnosis['code'] != 'collected':
            self._show_problem(diagnosis['summary'])
        else:
            self._clear_problem()
        if requested is not None:
            self._set_selection_review(requested, '重新发现前的选择' if had_tests else '刷新时带入的选择')
        else:
            self.selection_review_label.setText('未指定继承范围；当前为默认推荐／可用勾选，可手动调整。')
            self.selection_review_panel.show()
        self._filter_tests()
        self._schedule_view_save()

    def selected_test_ids(self):
        return [self.tests_tree.topLevelItem(index).data(0, _ROLE)
                for index in range(self.tests_tree.topLevelItemCount())
                if self.tests_tree.topLevelItem(index).checkState(0) == Qt.CheckState.Checked]

    def _test_checked(self, item, column):
        if self._changing_checks or column != 0:
            return
        selected = self.selected_test_ids()
        if len(selected) > _MAX_SELECTED:
            self._changing_checks = True
            try:
                item.setCheckState(0, Qt.CheckState.Unchecked)
            finally:
                self._changing_checks = False
            self.status_label.setText(f'单次最多选择 {_MAX_SELECTED} 项，已保留原有勾选。')
            return
        self._pending_selection = None
        self._selection_intent = selected
        if self._selection_review is not None:
            self._set_selection_review(self._selection_review['requested_tests'], '原带入范围（当前勾选已明确）')
        else:
            self._set_selection_review(selected, '当前明确选择')
        self._filter_tests()
        self._schedule_view_save()

    def _filter_tests(self, *_):
        self.test_search_timer.stop()
        needle, mode = self.test_search.text().strip().casefold(), self.test_filter.currentData()
        scope = self.test_scope.currentData() or ''
        visible = selected = 0
        total = self.tests_tree.topLevelItemCount()
        for index in range(total):
            item = self.tests_tree.topLevelItem(index)
            checked = item.checkState(0) == Qt.CheckState.Checked
            selected += checked
            show = (not needle or needle in (item.text(0) + ' ' + item.text(1)).casefold())
            path = str(item.data(0, _ROLE)).split('::', 1)[0]
            show = show and (not scope or scope == 'file:' + path
                             or (scope.startswith('dir:') and path.startswith(scope[4:])))
            show = show and (mode != 'recommended' or bool(item.data(1, _ROLE)))
            show = show and (mode != 'selected' or checked)
            if item.isHidden() == show:
                item.setHidden(not show)
            visible += show
        if self.collection is not None:
            text = f'已选 {selected} / {total} 个 · 当前可见 {visible} 个 · 单次上限 {_MAX_SELECTED} 个'
            if selected > _MAX_SELECTED:
                text += ' · 请减少选择后运行'
            self.test_count_label.setText(text)
        self._sync_controls()

    def _check_ids(self, selected, *, explicit=True):
        if len(selected) > _MAX_SELECTED:
            self.status_label.setText(f'这次选择有 {len(selected)} 个测试，超过单次 2000 个。请用搜索或推荐筛选缩小范围。')
            return False
        self._changing_checks = True
        for index in range(self.tests_tree.topLevelItemCount()):
            item = self.tests_tree.topLevelItem(index)
            item.setCheckState(0, Qt.CheckState.Checked if item.data(0, _ROLE) in selected
                               else Qt.CheckState.Unchecked)
        self._changing_checks = False
        if explicit:
            self._pending_selection = None
            self._selection_intent = self.selected_test_ids()
            if self._selection_review is not None:
                self._set_selection_review(self._selection_review['requested_tests'], '原带入范围（当前勾选已明确）')
            else:
                self._set_selection_review(self._selection_intent, '当前明确选择')
        self._filter_tests()
        self._schedule_view_save()
        return True

    def _visible_test_ids(self):
        if self.test_search_timer.isActive():
            self._filter_tests()
        return {self.tests_tree.topLevelItem(i).data(0, _ROLE)
                for i in range(self.tests_tree.topLevelItemCount())
                if not self.tests_tree.topLevelItem(i).isHidden()}

    def select_only_visible_tests(self):
        self._check_ids(self._visible_test_ids())

    def invert_visible_tests(self):
        self._check_ids(set(self.selected_test_ids()) ^ self._visible_test_ids())

    def copy_test_selection(self):
        selected = self.selected_test_ids()
        if not selected:
            self.status_label.setText('请先勾选要复制的测试。')
            return
        QApplication.clipboard().setText('\n'.join(selected))
        self.status_label.setText(f'已复制 {len(selected)} 个测试 ID；未执行测试。')

    def export_test_selection(self):
        selected = self.selected_test_ids()
        target, _ = QFileDialog.getSaveFileName(self, '导出可复用的测试选择清单',
                    str(self.project_root.parent / 'test-selection.json'), 'JSON 清单 (*.json);;逐行文本 (*.txt)')
        if not target:
            return
        try:
            write_selection(target, selected, title=(self.state or {}).get('title', ''), allow_empty=True)
            self.status_label.setText(f'已导出 {len(selected)} 项选择；可在桌面导入，或用于 CLI --selection-file。')
        except (OSError, ValueError) as exc:
            self._failed(str(exc))

    def import_test_selection(self):
        target, _ = QFileDialog.getOpenFileName(self, '导入测试选择清单',
                    str(self.project_root.parent), '测试清单 (*.json *.txt)')
        if not target:
            return
        try:
            selected = read_selection(target, allow_empty=True)
            self._review_and_apply_selection(selected, '核对导入清单')
        except (OSError, ValueError) as exc:
            self._failed(str(exc))

    def select_visible_tests(self):
        self._check_ids(set(self.selected_test_ids()) | self._visible_test_ids())

    def select_recommended_tests(self):
        selected = {self.tests_tree.topLevelItem(index).data(0, _ROLE)
                    for index in range(self.tests_tree.topLevelItemCount())
                    if self.tests_tree.topLevelItem(index).data(1, _ROLE)}
        if self._check_ids(selected) and not selected:
            self.status_label.setText('没有直接关联的推荐测试。可搜索并手动选择，或选中全部可见测试。')

    def clear_selected_tests(self):
        self._check_ids(set())

    def _set_selection_review(self, selected, title):
        collection = self.collection or {}
        self._selection_review = reconcile_selection(selected, collection.get('tests') or [],
            collection_complete=collection.get('status') == 'collected' and not (collection.get('partial') or collection.get('truncated')))
        result = self._selection_review
        if self.collection is None:
            message = f'{title}：{result["requested_count"]} 项，尚未发现。可查看原 ID；不会自动运行。'
        else:
            message = (f'{title}：{result["requested_count"]} 项 · 匹配 {result["matched_count"]} 项'
                       f' · 本次未找到 {result["missing_count"]} 项。'
                       + ('正在等待核对，原 ID 保留。' if self._pending_selection is not None else f'范围已明确，当前勾选 {len(self.selected_test_ids())} 项。')
                       + ('发现列表不完整，未找到不代表已删除。' if not result['collection_complete'] else ''))
        self.selection_review_label.setText(message)
        self.selection_review_panel.show()

    def _review_and_apply_selection(self, selected, title):
        if self.busy:
            return False
        collection = self.collection or {}
        self.selection_dialog = SelectionReviewDialog(selected, collection.get('tests') or [],
            title=title, parent=self, discovery_status=collection.get('status', 'uncollected'),
            collection_complete=not (collection.get('partial') or collection.get('truncated')))
        if self.selection_dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        result = self.selection_dialog.review
        self.test_search.clear()
        self.test_scope.setCurrentIndex(0)
        self._check_ids(set(result['matched_tests']))
        self.test_filter.setCurrentIndex(self.test_filter.findData('selected'))
        self._set_selection_review(selected, title)
        self.status_label.setText(f'已明确采用可用 {result["matched_count"]} 项，未找到 {result["missing_count"]} 项；尚未执行测试。')
        return True

    def review_inherited_selection(self):
        if self._selection_review is not None:
            self._review_and_apply_selection(self._selection_review['requested_tests'], '核对带入的选择')

    def _refresh_presets(self, selected_id=None):
        if not self.state:
            return
        try:
            result = list_presets(self.preset_root, self.state['repository'])
            selected_id = selected_id or self.preset_combo.currentData()
            self._presets = {item['preset_id']: item for item in result['presets']}
            self.preset_combo.blockSignals(True)
            self.preset_combo.clear()
            self.preset_combo.addItem('此项目的常用选择方案', None)
            for identity, item in self._presets.items():
                self.preset_combo.addItem(f'{item["title"]} · {item["count"]} 项', identity)
                self.preset_combo.setItemData(self.preset_combo.count() - 1, item['title'], Qt.ItemDataRole.ToolTipRole)
            self.preset_combo.setCurrentIndex(max(0, self.preset_combo.findData(selected_id)))
            self.preset_combo.blockSignals(False)
            if result['issues']:
                self.status_label.setText('部分选择方案无法读取：' + '\n'.join(result['issues'][:5]))
        except (OSError, ValueError) as exc:
            self._failed(str(exc))
        self._sync_controls()

    def _confirm_choice(self, title, text):
        dialog = QMessageBox(QMessageBox.Icon.Question, title, text,
                             QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        return dialog.exec() == QMessageBox.StandardButton.Yes

    def save_selection_preset(self):
        if self.busy or not self.state:
            return
        selected = self.selected_test_ids()
        current = self._presets.get(self.preset_combo.currentData(), {})
        title, accepted = QInputDialog.getText(self, '保存常用选择方案', '方案名称（例如：冒烟检查、支付相关）',
                                               QLineEdit.EchoMode.Normal, current.get('title', '常用检查'))
        if not accepted:
            return
        existing = next((item for item in self._presets.values()
                         if item['title'].casefold() == title.strip().casefold()), None)
        if existing and not self._confirm_choice('更新选择方案', f'用当前 {len(selected)} 项选择更新“{existing["title"]}”？'):
            return
        try:
            saved = save_preset(self.preset_root, self.state['repository'], title, selected,
                                preset_id=existing['preset_id'] if existing else None)
            self._refresh_presets(saved['preset_id'])
            self.status_label.setText('选择方案已保存，可在此仓库的新会话中复用；未执行测试。')
        except (OSError, ValueError) as exc:
            self._failed(str(exc))

    def apply_selection_preset(self):
        identity = self.preset_combo.currentData()
        if self.busy or not self.state or not self.collection or not identity:
            return
        try:
            preset = read_preset(self.preset_root, self.state['repository'], identity)
            self._review_and_apply_selection(preset['selected_tests'], f'核对方案：{preset["title"]}')
        except (OSError, ValueError) as exc:
            self._failed(str(exc))

    def delete_selection_preset(self):
        identity = self.preset_combo.currentData()
        if self.busy or not self.state or not identity:
            return
        title = self._presets.get(identity, {}).get('title', '所选方案')
        if not self._confirm_choice('删除选择方案', f'删除“{title}”？已保存的源码、报告和当前勾选都会保留。'):
            return
        try:
            delete_preset(self.preset_root, self.state['repository'], identity)
            self._refresh_presets()
            self.status_label.setText('选择方案已删除。')
        except (OSError, ValueError) as exc:
            self._failed(str(exc))

    def run_selected(self, selected_ids=None):
        if self.busy or not self.state:
            return False
        if self._collection_path_pending():
            self.status_label.setText('发现范围已修改，请重新发现测试。')
            return False
        if self._pending_selection is not None and (selected_ids is None or isinstance(selected_ids, bool)):
            self.status_label.setText('原范围仍在等待核对，请先采用可用项或手动选择；未开始运行。')
            return False
        if self.test_search_timer.isActive():
            self._filter_tests()
        selected = self.selected_test_ids() if selected_ids is None or isinstance(selected_ids, bool) else list(selected_ids)
        if any(not isinstance(test, str) for test in selected):
            self.status_label.setText('所选测试 ID 无效，请重新发现测试。')
            return False
        selected = list(dict.fromkeys(selected))
        if not 1 <= len(selected) <= _MAX_SELECTED:
            self.status_label.setText('请选择 1–2000 个实际发现的测试，再运行前后版本。')
            return False
        available = set((self.collection or {}).get('tests') or [])
        if not self.collection or not set(selected).issubset(available):
            self.status_label.setText('请先在当前 Python 环境实际发现测试，并只运行已发现的完整测试 ID。')
            return False
        directory, output = self.state['directory'], self.report_root
        python, timeout = self.python_edit.text().strip() or sys.executable, self.timeout_spin.value()
        self.report = None
        self.result_details.clear()
        self.version_logs.clear()
        self.result_filter.setCurrentIndex(0)
        self._live_entries = {test: {'test_id': test, 'status': 'running',
                              'before': {'status': 'not_run'}, 'after': {'status': 'not_run'}}
                              for test in selected}
        self._render_results(list(self._live_entries.values()), live=True)
        self.result_summary.setText(f'正在运行 {len(selected)} 个测试 · 前后版本各一个进程')
        self.tabs.setCurrentIndex(2)
        def action(control):
            report = run_project(directory, output, selected_ids=selected,
                                 python_executable=python, timeout=timeout,
                                 confirmed=True, control=control)
            try:
                state = read_project(directory)
            except (OSError, ValueError):
                state = None
            return {'report': report, 'state': state}
        def shown(result):
            if result['state'] is not None:
                self.state = result['state']
            self._apply_report(result['report'])
            self._remember_project_options()
            self._show_runs()
            self._refresh_catalog()
        return self._start(action, shown,
                           f'正在运行 {len(selected)} 个可信测试；先基线，再变更后版本，每版限时 {timeout:g} 秒…', operation='run')

    def _render_results(self, entries, live=False):
        self.result_metrics.setVisible(not live)
        if live or self.report is None:
            self._clear_diagnostics()
        previous = self.results_tree.currentItem()
        previous_id = previous.data(0, _ROLE).get('test_id') if previous else None
        selected = {item.data(0, _ROLE).get('test_id') for item in self.results_tree.selectedItems()}
        self.results_tree.setUpdatesEnabled(False)
        self.results_tree.blockSignals(True)
        self.results_tree.clear()
        self._report_items = {}
        priority = {'regression_detected': 0, 'inconclusive': 1, 'baseline_failure': 2, 'improved': 3, 'regression_passed': 4}
        displayed = entries if live else sorted(entries, key=lambda entry: priority.get(entry.get('status'), 5) if isinstance(entry, dict) else 5)
        for entry in displayed:
            if not isinstance(entry, dict):
                continue
            before, after = entry.get('before') or {}, entry.get('after') or {}
            status = str(entry.get('status') or 'inconclusive')
            test_id = str(entry.get('test_id') or '')
            filename, separator, target = test_id.partition('::')
            display = target + '\n' + filename if separator else test_id
            item = QTreeWidgetItem([display, '等待执行' if live else _STATUS.get(status, status),
                    _STATUS.get(before.get('status'), str(before.get('status') or '未执行')),
                    _STATUS.get(after.get('status'), str(after.get('status') or '未执行')),
                    _duration(before.get('duration_seconds')) + ' / ' + _duration(after.get('duration_seconds'))])
            item.setData(0, _ROLE, entry)
            item.setToolTip(0, test_id)
            if status in _COLOR:
                item.setForeground(1, QColor(_COLOR[status]))
            self.results_tree.addTopLevelItem(item)
            self._report_items[test_id] = item
            if test_id in selected:
                item.setSelected(True)
        if previous_id in self._report_items:
            self.results_tree.setCurrentItem(self._report_items[previous_id])
        elif self.results_tree.topLevelItemCount() and not live:
            self.results_tree.setCurrentItem(self._first_attention_item() or self.results_tree.topLevelItem(0))
        self.results_tree.blockSignals(False)
        self.results_tree.setUpdatesEnabled(True)
        self._filter_results()
        self._show_result_detail(self.results_tree.currentItem())

    def _apply_report(self, report):
        if not isinstance(report, dict) or report.get('kind') != 'project_regression':
            raise ValueError('返回的项目回归报告无效。')
        history_warning = report.get('history_warning', '')
        report = _read_report(report['report_dir'])
        self._report_reference = report_reference(report)
        self.report = report
        origin = report.get('review_source') or {}
        self.report_context_label.setText('当前报告：' + str(report.get('run_id'))
            + ' · 会话：' + str(report.get('project_id'))
            + ('\n复查来源：' + str(origin.get('run_id')) + '；可点击“与来源报告比较”。' if origin else ''))
        views = dict(self.store.settings.get('project_report_views') or {})
        views[report['project_id']] = dict(self._report_reference)
        self.store.settings['project_report_views'] = dict(list(views.items())[-200:])
        view_warning = history_warning
        try:
            self.store.save()
        except OSError as exc:
            view_warning += ('\n' if view_warning else '') + '所选运行的恢复记录保存失败：' + str(exc)
        self._set_inputs_expanded(False)
        self._live_entries = {}
        self.results_scroll.verticalScrollBar().setValue(0)
        entries = report.get('entries') or []
        self._load_diagnostics(report)
        self.result_search.clear()
        self.result_filter.setCurrentIndex(0)
        counts = Counter(entry.get('status', 'inconclusive') for entry in entries if isinstance(entry, dict))
        status = report.get('status', 'attention')
        headline = _RUN_STATUS.get(status, _STATUS.get(status, status))
        if (report.get('collection') or {}).get('partial') or (report.get('collection') or {}).get('truncated'):
            headline += ' · 发现范围不完整，仅覆盖所选项'
        self.result_summary.setText(headline)
        interrupted = [(label, report.get(side) or {}) for side, label in (('before', '基线'), ('after', '候选'))
                       if (report.get(side) or {}).get('progress') and (report.get(side) or {}).get('status') in ('timed_out', 'cancelled', 'worker_error')]
        self.run_progress_panel.setVisible(bool(interrupted))
        if interrupted:
            self.run_progress_label.setText('；'.join(label + '已保存 ' + str(run['progress']['completed']) + '/' + str(run['progress']['total']) + ' 项完整阶段观察' for label, run in interrupted)
                                           + '。本轮未完整执行，不计为正式通过。')
            self.run_current_test.setText('；'.join(label + '最后记录：' + str(run['progress'].get('current_test') or '无正在执行的测试')
                + ' · ' + PHASE_LABELS.get(run['progress'].get('current_phase'), '未知阶段') for label, run in interrupted))
        self.result_summary.setToolTip(self.report_context_label.text())
        for key, (metric, title) in self.result_metric_buttons.items():
            metric.setText(str(counts[key]) + '\n' + title)
            metric.setProperty('attention', bool(counts[key] and key in ('regression_detected', 'baseline_failure', 'inconclusive')))
            metric.style().unpolish(metric)
            metric.style().polish(metric)
        self._render_results(entries)
        attention = self._first_attention_item()
        if attention is not None:
            self.results_tree.setCurrentItem(attention)
            self.results_tree.scrollToItem(attention)
        self.version_logs.setPlainText(self._report_logs(report))
        self.status_label.setText(f'已保存本次检查 · {len(entries)} 项测试' + '\n' + str(report.get('notice') or headline) + '\n' + str(report.get('report_dir') or ''))
        if view_warning:
            self.status_label.setText(self.status_label.text() + '\n' + view_warning)
        self._queue_review_progress()
        self._sync_controls()

    def _filter_results(self, *_):
        needle, mode = self.result_search.text().strip().casefold(), self.result_filter.currentData()
        group = self._selected_diagnostic_group()
        members = set(group['test_ids']) if group else None
        visible = 0
        for index in range(self.results_tree.topLevelItemCount()):
            item = self.results_tree.topLevelItem(index)
            entry = item.data(0, _ROLE)
            # Include exception text so a repeated failure can be found by its message.
            searchable = str(entry.get('test_id') or '') + ' ' + ' '.join(item.text(column) for column in range(item.columnCount()))
            for side in ('before', 'after'):
                error = (entry.get(side) or {}).get('exception')
                if error:
                    searchable += ' ' + _format_json(error, 12000)
            matches = (mode == 'all' or entry.get('status') == mode
                       or (mode == 'attention' and entry.get('status') in
                           {'regression_detected', 'baseline_failure', 'inconclusive'}))
            show = matches and (members is None or entry.get('test_id') in members) and (not needle or needle in searchable.casefold())
            item.setHidden(not show)
            visible += int(show)
        current = self.results_tree.currentItem()
        if current is not None and current.isHidden():
            chosen = self._first_attention_item()
            if chosen is None:
                chosen = next((self.results_tree.topLevelItem(i) for i in range(self.results_tree.topLevelItemCount())
                               if not self.results_tree.topLevelItem(i).isHidden()), None)
            self.results_tree.setCurrentItem(chosen)
        total = self.results_tree.topLevelItemCount()
        self.result_count_label.setText(f'显示 {visible} / {total} 项' + (' · 需处理项优先' if self.report is not None and not self._live_entries else '') + (f' · 本组共 {group["count"]} 项' if group else '')
            + (' · 没有匹配结果，可清除筛选' if total and not visible else ''))
        self._sync_diagnostic_actions()

    def _first_attention_item(self):
        for status in ('regression_detected', 'inconclusive', 'baseline_failure'):
            for index in range(self.results_tree.topLevelItemCount()):
                item = self.results_tree.topLevelItem(index)
                if not item.isHidden() and item.data(0, _ROLE).get('status') == status:
                    return item
        return None

    def show_attention(self):
        self.diagnostic_selector.setCurrentIndex(0)
        self.tabs.setCurrentIndex(2)
        self.result_search.clear()
        self.result_filter.setCurrentIndex(self.result_filter.findData('attention'))
        item = self._first_attention_item()
        if item is not None:
            self.results_tree.setCurrentItem(item)
            self.results_tree.scrollToItem(item)

    def _clear_diagnostics(self):
        self._diagnostic_by_id = {}
        self._diagnostic_summary = None
        self.diagnostic_selector.blockSignals(True)
        self.diagnostic_selector.clear()
        self.diagnostic_selector.addItem('按全部现象查看', None)
        self.diagnostic_selector.blockSignals(False)
        self.diagnostic_panel.hide()

    def _load_diagnostics(self, report):
        self._clear_diagnostics()
        summary = group_project_failures(report)
        self._diagnostic_summary = summary
        self._diagnostic_by_id = {group['id']: group for group in summary['groups']}
        self.diagnostic_selector.blockSignals(True)
        for group in summary['groups']:
            preview = ' '.join(group['message'].split())[:80]
            self.diagnostic_selector.addItem(group['label'] + (' · ' + preview if preview else ''), group['id'])
            self.diagnostic_selector.setItemData(self.diagnostic_selector.count() - 1,
                group['label'] + '\n' + group['message'] + '\n' + group['basis'], Qt.ItemDataRole.ToolTipRole)
        self.diagnostic_selector.blockSignals(False)
        self.diagnostic_panel.setVisible(bool(summary['groups']))

    def _selected_diagnostic_group(self):
        return self._diagnostic_by_id.get(self.diagnostic_selector.currentData())

    def _sync_diagnostic_actions(self):
        idle = self.task is None and not self._closing
        available = bool(self.report and not self._live_entries and self._diagnostic_by_id)
        group = self._selected_diagnostic_group() if available else None
        self.diagnostic_panel.setVisible(available)
        self.diagnostic_selector.setEnabled(idle and available)
        self.copy_diagnostic_button.setEnabled(bool(idle and group))
        self.recheck_diagnostic_button.setEnabled(bool(idle and group and group['count'] and self._report_reference))
        self.recheck_diagnostic_button.setText(f"复查本组（{group['count']} 项）" if group else '复查本组')
        self.recheck_diagnostic_button.setToolTip('冻结当前修改并运行本组的全部测试，包含被搜索隐藏的项；测试或范围变化时会停下核对。')
        same_project = bool(self.state and self.report and self.state.get('project_id') == self.report.get('project_id'))
        self.diagnostic_action_button.setEnabled(bool(idle and group and (group['action'] != 'environment' or same_project)))
        self.diagnostic_action_button.setText({'environment': '检查项目环境', 'logs': '查看两版日志',
                                               'representative': '定位代表项'}.get((group or {}).get('action'), '查看代表项'))
        self.diagnostic_action_button.setToolTip('请先打开对应项目会话，再检查其环境。' if group and group['action'] == 'environment' and not same_project
                                                 else str((group or {}).get('advice') or '选择一个现象组查看建议。'))
        if group:
            self.diagnostic_notice.setText(group['advice'])
            self.diagnostic_notice.setToolTip(group['basis'] + '\n' + group['advice'])
        elif self._diagnostic_summary:
            summary = self._diagnostic_summary
            self.diagnostic_notice.setText(f"{summary['attention_test_count']} 项需处理 · {summary['group_count']} 组现象。相同异常不等于共同根因；一项可属于多个组。")
        self.clear_result_filters_button.setEnabled(bool(idle and self.results_tree.topLevelItemCount()))

    def _diagnostic_changed(self, *_):
        # Selecting a group starts with its full range, independent of earlier filters.
        self.result_search.blockSignals(True)
        self.result_filter.blockSignals(True)
        self.result_search.clear()
        self.result_filter.setCurrentIndex(0)
        self.result_search.blockSignals(False)
        self.result_filter.blockSignals(False)
        self._filter_results()
        self._focus_diagnostic_group()

    def _focus_diagnostic_group(self):
        group = self._selected_diagnostic_group()
        if not group:
            return
        item = self._first_attention_item()
        if item is not None:
            self.results_tree.setCurrentItem(item)
            self.results_tree.scrollToItem(item)

    def clear_result_filters(self):
        self.diagnostic_selector.blockSignals(True)
        self.diagnostic_selector.setCurrentIndex(0)
        self.diagnostic_selector.blockSignals(False)
        self._diagnostic_changed()

    def copy_diagnostic_group(self):
        group = self._selected_diagnostic_group()
        if group and self.report:
            QApplication.clipboard().setText(format_diagnostic_group(self.report, group))
            self.status_label.setText(f"已复制本组 {group['count']} 项的完整范围、归类依据和建议；没有发送到外部服务。")

    def open_diagnostic_action(self):
        group = self._selected_diagnostic_group()
        if not group or self.busy:
            return
        self._diagnostic_changed()
        if group['action'] == 'environment':
            self.inspect_result_environment()
        else:
            self.details_tabs.setCurrentIndex(1 if group['action'] == 'logs' else 0)
            self.result_detail_scroll.ensureWidgetVisible(self.details_tabs)
            self.results_scroll.ensureWidgetVisible(self.result_detail_scroll)

    def recheck_diagnostic_group(self):
        group = self._selected_diagnostic_group()
        if not group or not group['test_ids'] or self.busy:
            return False
        return self.quick_recheck(selected_ids=list(group['test_ids']))

    def _visible_results(self):
        return [self.results_tree.topLevelItem(i).data(0, _ROLE)
                for i in range(self.results_tree.topLevelItemCount())
                if not self.results_tree.topLevelItem(i).isHidden()]

    def copy_result_ids(self):
        selected = [item.data(0, _ROLE).get('test_id', '')
                    for item in self.results_tree.selectedItems() if not item.isHidden()]
        if not selected:
            self.status_label.setText('请先选中要复制的结果行。')
            return
        QApplication.clipboard().setText('\n'.join(selected))
        self.status_label.setText(f'已复制 {len(selected)} 个结果行的测试 ID。')

    def copy_result_summary(self):
        entries = self._visible_results()
        if not self.report or not entries:
            self.status_label.setText('当前筛选没有可复制的结果。')
            return
        report = self.report
        before = str(report.get('before_commit') or '')[:12]
        after = ('当前工作区快照' if report.get('after_commit') == WORKTREE else
                 str(report.get('after_commit') or '')[:12])
        lines = ['ReproLab · ' + str(report.get('title') or '项目回归'),
                 '生成此报告的工具版本：' + str(report.get('reprolab_version') or '未记录'),
                 f'版本：{before} → {after}',
                 f'范围：当前筛选 {len(entries)} 项 / 本次所选 {len(report.get("selected_tests", []))} 项',
                 '结论仅覆盖所选测试；以下为结果摘要，完整证据见报告。', '']
        for entry in entries:
            lines.append('- ' + str(entry.get('test_id') or '') + ' · '
                         + _STATUS.get(entry.get('status'), str(entry.get('status') or '')))
            for side, label in (('before', '基线'), ('after', '变更后')):
                outcome = entry.get(side) or {}
                error = outcome.get('exception') or {}
                text = label + '：' + _STATUS.get(outcome.get('status'), str(outcome.get('status') or '未执行'))
                if isinstance(error, dict) and error:
                    text += ' · ' + str(error.get('type') or '') + ': ' + str(error.get('message') or '')
                lines.append('  ' + _short(text, 2000))
        QApplication.clipboard().setText(_short('\n'.join(lines), 200000))
        self.status_label.setText(f'已复制当前筛选的 {len(entries)} 项摘要；可粘贴到问题记录中，没有发送到外部服务。')

    def export_results_csv(self):
        entries = self._visible_results()
        if not entries:
            self.status_label.setText('当前筛选没有可导出的结果。')
            return
        target, _ = QFileDialog.getSaveFileName(self, '导出当前筛选结果摘要',
                    str(self.report_root / 'project-results.csv'), 'CSV 表格 (*.csv)')
        if not target:
            return
        if not target.lower().endswith('.csv'):
            target += '.csv'
        def cell(value):
            text = '' if value is None else str(value)
            return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['测试 ID', '回归结果', '基线状态', '变更后状态',
                         '基线异常类型', '基线异常消息', '变更后异常类型', '变更后异常消息',
                         '基线耗时（秒）', '变更后耗时（秒）'])
        for entry in entries:
            before, after = entry.get('before') or {}, entry.get('after') or {}
            left = before.get('exception') if isinstance(before.get('exception'), dict) else {}
            right = after.get('exception') if isinstance(after.get('exception'), dict) else {}
            row = [entry.get('test_id'), _STATUS.get(entry.get('status'), entry.get('status')),
                   _STATUS.get(before.get('status'), before.get('status')),
                   _STATUS.get(after.get('status'), after.get('status')),
                   left.get('type'), left.get('message'), right.get('type'), right.get('message'),
                   before.get('duration_seconds'), after.get('duration_seconds')]
            writer.writerow([cell(value) for value in row])
        try:
            atomic_write(target, b'\xef\xbb\xbf' + output.getvalue().encode('utf-8'))
            self.status_label.setText(f'已导出当前筛选的 {len(entries)} 项结果摘要；完整证据仍保存在原报告中。')
        except OSError as exc:
            self._failed(str(exc))

    def _show_result_detail(self, item, *_):
        self.test_code_view.clear()
        if item is None:
            self.result_details.clear()
            self.selected_result_summary.clear()
            self._sync_failure_actions()
            return
        entry = item.data(0, _ROLE)
        summary = [str(entry.get('test_id') or ''),
            '实际两版：基线 ' + _STATUS.get((entry.get('before') or {}).get('status'), '未执行')
            + ' → 变更后 ' + _STATUS.get((entry.get('after') or {}).get('status'), '未执行')]
        error = (entry.get('after') or {}).get('exception') or (entry.get('before') or {}).get('exception') or {}
        if isinstance(error, dict) and error:
            summary.extend([str(error.get('type') or ''), str(error.get('message') or '')])
        self.selected_result_summary.setPlainText('\n'.join(summary))
        self._sync_failure_actions()
        lines = [str(entry.get('test_id') or ''),
                 '回归结果：' + _STATUS.get(entry.get('status'), str(entry.get('status') or '')), '']
        lines.extend(['建议下一步：', *('• ' + hint for hint in entry_guidance(entry, self.report or {})), ''])
        for side, title in (('before', '基线版本'), ('after', '变更后版本')):
            outcome = entry.get(side) or {}
            lines.extend([title, '状态：' + _STATUS.get(outcome.get('status'), str(outcome.get('status') or '未执行')),
                          '耗时：' + _duration(outcome.get('duration_seconds'))])
            if 'observed_status' in outcome:
                lines.append('中断前观察：' + OBSERVATION_LABELS.get(outcome['observed_status'], '阶段未完整结束') + '；本版未完整结束，不计为正式通过。')
            error = outcome.get('exception')
            if isinstance(error, dict):
                lines.extend(['异常：' + str(error.get('type') or ''), _short(error.get('message'), 16000)])
            elif error:
                lines.append(_short(error, 16000))
            phases = outcome.get('phases') or []
            if phases:
                lines.extend(['pytest 阶段：', _format_json(phases, 22000)])
            lines.append('')
        self.result_details.setPlainText('\n'.join(lines))

    def _selected_result(self):
        item = self.results_tree.currentItem()
        return item.data(0, _ROLE) if item is not None and not item.isHidden() else None

    def _sync_failure_actions(self):
        entry = self._selected_result()
        available = bool(self.task is None and self.report and self._report_reference and entry and not self._live_entries)
        ready, reason = repair_eligibility(entry or {}, self.report or {}) if entry else (False, '请先选择一个结果行。')
        self.repair_case_button.setEnabled(available and ready)
        self.repair_case_button.setToolTip(reason)
        cleared = bool(entry and entry.get('status') in ('regression_passed', 'improved')
                       and (entry.get('after') or {}).get('status') == 'passed')
        self.repair_brief_button.setVisible(not cleared)
        self.repair_case_button.setVisible(not cleared)
        self.repair_reason.setText('本项在变更后版本已通过。可以查看测试代码、完整报告或与来源报告比较。' if cleared
                                   else ('严格修复案例：' + reason) if entry else reason)
        problem = bool(not cleared and entry and (entry.get('status') == 'inconclusive' or
                       any((entry.get(side) or {}).get('status') == 'failed' for side in ('before', 'after'))))
        self.repair_brief_button.setEnabled(available and problem)
        self.repair_brief_button.setToolTip('读取当前所选报告中的历史材料，生成本地可编辑预览；不发送。' if problem else '请选择失败或受阻的结果行。')
        self.copy_selected_id_button.setEnabled(available)
        error_type = (((entry or {}).get('after') or {}).get('exception') or {}).get('type')
        blocked = bool(entry and not ready and (entry.get('status') == 'inconclusive'
                       or error_type in ('ModuleNotFoundError', 'ImportError')))
        self.result_environment_button.setVisible(blocked)
        same_project = bool(self.report and self.state and self.report.get('project_id') == self.state.get('project_id'))
        self.result_environment_button.setEnabled(available and same_project)
        self.result_environment_button.setToolTip('检查当前会话的项目解释器与依赖。' if same_project else '请先打开这份报告对应的会话，再检查项目环境。')
        action, label, advice = entry_action(entry, self.report or {})
        self.result_next_button.setText(label)
        self.result_next_button.setEnabled(available and (action != 'environment' or same_project))
        self.result_next_button.setToolTip(advice if action != 'environment' or same_project else '请先打开这份报告对应的会话，再检查项目环境。')
        self.result_next_notice.setText(advice)
        self.repair_brief_button.setVisible(bool(entry) and not cleared and action != 'repair')
        self.repair_brief_button.setText('整理诊断说明' if action in ('environment', 'logs', 'details') else '准备修复说明')
        self.repair_case_button.setVisible(not cleared and ready)
        self.result_environment_button.setVisible(blocked and action != 'environment')

    def open_result_action(self):
        if self.task is not None or not self.report or not self._selected_result():
            return
        action, _, _ = entry_action(self._selected_result(), self.report)
        if action == 'environment':
            self.inspect_result_environment()
        elif action == 'repair':
            self.prepare_repair_brief()
        elif action == 'test':
            self.preview_selected_test()
        else:
            self.details_tabs.setCurrentWidget(self.version_logs if action == 'logs' else self.result_details)
            self.result_detail_scroll.ensureWidgetVisible(self.details_tabs)

    def _failure_context(self):
        entry = self._selected_result()
        if self.busy or not self.report or not self._report_reference or not entry or self._live_entries:
            self.status_label.setText('请先打开完整项目报告并选择一个结果行。')
            return None
        reference, identity = dict(self._report_reference), entry['test_id']
        def is_current():
            current = self._selected_result()
            return bool(self._report_reference == reference and current and current.get('test_id') == identity)
        return reference, identity, is_current

    def _show_failure_dialog(self, dialog):
        self.failure_dialog = dialog
        dialog.busyChanged.connect(lambda _: self.busyChanged.emit(self.busy))
        def closed(*_):
            if getattr(dialog, 'saved_case', None):
                self.status_label.setText('修复案例已保存，尚未执行：' + dialog.saved_case)
            elif getattr(dialog, 'plan', None):
                self.status_label.setText('修复准备材料已保存，尚未执行。可从“结果操作… → 重开已保存的修复准备”继续：\n' + dialog.plan['directory'])
            self.failure_dialog = None
            self.busyChanged.emit(self.busy)
            dialog.deleteLater()
        dialog.finished.connect(closed)
        dialog.show()

    def prepare_repair_brief(self):
        context = self._failure_context()
        if context is None:
            return
        reference, identity, is_current = context
        def loaded(material):
            if not is_current():
                self.status_label.setText('报告或所选测试已改变；请重新从当前所选项准备修复说明。')
                return
            self._after_finished = lambda: self._show_failure_dialog(RepairBriefDialog(material, self.report_root.parent / 'repair-notes', self,
                draft_root=self.report_root.parent / 'repair-notes/drafts')) if is_current() else None
        self._start(lambda control: load_repair_material(reference['report_dir'], identity,
            expected_reference=reference, control=control), loaded,
            '正在核对所选报告并读取历史材料，不读取当前源码、不执行或发送…', operation='repair_material')

    def reopen_repair_brief(self):
        from .repair_drafts import read_draft, recovery_material
        root = self.report_root.parent / 'repair-notes/drafts'
        picker = RepairDraftPicker(root, self)
        picker.exec()
        path = picker.selected_path
        picker.deleteLater()
        if not path:
            return
        try:
            draft = read_draft(path)
            if draft is None:
                raise ValueError('草稿已不存在。')
        except (OSError, ValueError) as exc:
            self.status_label.setText('无法恢复草稿：' + str(exc)); return
        def action(control):
            binding = draft['identity']
            try:
                material = load_repair_material(draft['report_dir'], binding['test_id'],
                    expected_reference={'schema_version': 1, 'kind': 'project_report_reference', 'report_dir': draft['report_dir'],
                        **{k: binding[k] for k in ('project_id', 'run_id', 'report_sha256')}}, control=control)
                return material, ''
            except (OSError, ValueError) as exc:
                return recovery_material(draft), str(exc)
        def shown(result):
            material, error = result
            def open_draft():
                dialog = RepairBriefDialog(material, self.report_root.parent / 'repair-notes', self,
                    draft_root=Path(path).parent, draft=draft, source_available=not error)
                if error:
                    dialog.status.setText('来源核对未完成：' + error)
                self._show_failure_dialog(dialog)
            self._after_finished = open_draft
        self._start(action, shown, '正在核对草稿来源，未执行测试…', operation='repair_material')

    def prepare_result_repair(self):
        context = self._failure_context()
        if context is None:
            return
        ready, reason = repair_eligibility(self._selected_result(), self.report)
        if not ready:
            self.status_label.setText(reason)
            return
        reference, identity, is_current = context
        from .repair_drafts import draft_path, read_draft
        binding = {'repository': self.report.get('repository', ''), 'project_id': reference['project_id'],
                   'run_id': reference['run_id'], 'report_sha256': reference['report_sha256'], 'test_id': identity,
                   'test_sha256': self.report.get('shared_tests', {}).get(identity.split('::', 1)[0], '')}
        try:
            brief_draft = read_draft(draft_path(self.report_root.parent / 'repair-notes/drafts', binding))
        except (OSError, ValueError) as exc:
            brief_draft = None
            self.status_label.setText('修复说明草稿未能预填：' + str(exc))
        dialog = ReportRepairDialog(self.report, reference, identity, dict(self.store.settings),
            self.project_root.parent / 'repair-preparations', self, is_current=is_current, brief_draft=brief_draft)
        dialog.caseReady.connect(self.casePrepared.emit)
        self._show_failure_dialog(dialog)

    def reopen_repair_preparation(self):
        if self.busy:
            return
        dialog = ReportRepairDialog(None, None, None, dict(self.store.settings),
            self.project_root.parent / 'repair-preparations', self)
        dialog.caseReady.connect(self.casePrepared.emit)
        self._show_failure_dialog(dialog)
        QTimer.singleShot(0, dialog, dialog.reopen)

    def _result_context_menu(self, point):
        item = self.results_tree.itemAt(point)
        if item is None:
            return
        self.results_tree.setCurrentItem(item)
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        for button, callback in ((self.repair_brief_button, self.prepare_repair_brief),
                                 (self.repair_case_button, self.prepare_result_repair),
                                 (self.copy_selected_id_button, self.copy_result_ids)):
            action = menu.addAction(button.text())
            action.setEnabled(button.isEnabled())
            action.setToolTip(button.toolTip())
            action.triggered.connect(lambda checked=False, fn=callback: fn())
        menu.exec(self.results_tree.viewport().mapToGlobal(point))

    def copy_entry_diagnosis(self):
        text = self.result_details.toPlainText()
        if not text:
            self.status_label.setText('请先选择一个结果行。')
            return
        QApplication.clipboard().setText(text)
        self.status_label.setText('已复制此项实际异常、阶段和处理建议；没有发送到外部服务。')

    def inspect_result_environment(self):
        if (not self.state or not self.report
                or self.state.get('project_id') != self.report.get('project_id')):
            self.status_label.setText('请先打开这份报告对应的项目会话，再检查或更换其环境。')
            return
        self._set_inputs_expanded(True)
        self.inspect_environment()

    def preview_selected_test(self):
        item = self.results_tree.currentItem()
        if self.busy or not self.report or item is None or item.isHidden():
            return
        identity = item.data(0, _ROLE).get('test_id')
        name = parse_test_target(identity)
        report = self.report
        directory = Path(report['report_dir']).resolve()
        def action(control):
            control.check()
            root = directory / 'evidence/project/inputs/after'
            source = root / name
            if (not root.resolve().is_relative_to(directory) or not source.resolve().is_relative_to(root.resolve())
                    or source.is_symlink() or not source.is_file() or source.stat().st_size > 100 * 1024 * 1024):
                raise ValueError('报告中的测试文件不存在、越界或超过读取上限。')
            with source.open('rb') as stream:
                raw = stream.read(128 * 1024 + 1)
                hasher = hashlib.sha256(raw)
                size = len(raw)
                while chunk := stream.read(1024 * 1024):
                    control.check()
                    size += len(chunk)
                    if size > 100 * 1024 * 1024:
                        raise ValueError('测试文件超过读取上限。')
                    hasher.update(chunk)
                digest = hasher.hexdigest()
            if digest != (report.get('shared_tests') or {}).get(name):
                raise ValueError('保存的共享测试内容已变化，未作为实际测试代码显示。')
            control.check()
            encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
            text = raw[:128 * 1024].decode(encoding, 'replace')
            return f'{name}\n两版使用报告保存的同一份变更后测试文件；内容指纹已核对。\n\n' + text + (
                '\n\n…文件超过 128 KiB，仅显示前部内容。' if len(raw) > 128 * 1024 else '')
        def shown(text):
            current = self.results_tree.currentItem()
            if (not current or current.data(0, _ROLE).get('test_id') != identity
                    or not self.report or report_reference(self.report) != report_reference(report)):
                self.status_label.setText('结果选择已改变，请重新选择查看对应测试代码。')
                return
            self.test_code_view.setPlainText(text)
            self.details_tabs.setCurrentWidget(self.test_code_view)
        self._start(action, shown, '正在读取并核对报告保存的测试代码，不执行项目…')

    @staticmethod
    def _report_logs(report):
        lines = [str(report.get('title') or ''), str(report.get('report_dir') or ''), '']
        for side, title in (('before', '基线版本'), ('after', '变更后版本')):
            version = report.get(side) or {}
            lines.extend([title, '状态：' + str(version.get('status') or '未知'),
                          f'退出码：{version.get("exit_code")} · 超时：{bool(version.get("timed_out"))}',
                          '事件记录完整：' + str(version.get('worker_record_complete', '未知')),
                          f'Python {version.get("python_version") or "未知"} · pytest {version.get("pytest_version") or "未知"}'])
            if version.get('collection_errors'):
                lines.extend(['收集错误', _format_json(version['collection_errors'], 16000)])
            lines.extend(['日志', _short(version.get('log_excerpt'), 45000), ''])
        return '\n'.join(lines)

    def _failed_ids(self):
        if not self.report:
            return []
        return [entry['test_id'] for entry in self.report.get('entries', [])
                if isinstance(entry, dict) and entry.get('test_id') and
                entry.get('status') in {'regression_detected', 'baseline_failure', 'inconclusive'}]

    def rerun_failed(self):
        return self.run_selected(self._failed_ids())

    def rerun_selected(self):
        ids = [item.data(0, _ROLE)['test_id'] for item in self.results_tree.selectedItems() if not item.isHidden()]
        return self.run_selected(ids)

    def refresh_sessions(self):
        if self.busy or self._closing:
            return
        self._start(lambda control: list_projects(self.project_root), self._show_catalog,
                    '正在读取已保存的项目会话…')

    def _refresh_catalog(self):
        # This small metadata-only refresh also runs after a worker result; it
        # must not start a second Task before that worker's ``finished`` signal.
        try:
            self._show_catalog(list_projects(self.project_root))
        except (OSError, ValueError) as exc:
            self.status_label.setText(self.status_label.text() + '\n项目目录列表未刷新：' + str(exc))

    def _show_catalog(self, projects):
        if isinstance(projects, dict):
            projects = projects.get('projects') or []
        current = self.state.get('directory') if self.state else self.session_combo.currentData()
        self.session_combo.clear()
        self.session_combo.addItem('选择已保存的项目会话…', None)
        for project in projects or []:
            if not isinstance(project, dict) or not project.get('directory'):
                continue
            directory = str(project['directory'])
            name = str(project.get('title') or project.get('project_id') or Path(directory).name)
            runs = project.get('runs')
            suffix = f' · {len(runs)} 次运行' if isinstance(runs, list) else ''
            identity = str(project.get('project_id') or Path(directory).name)
            self.session_combo.addItem(name + suffix + ' · ' + identity[-8:], directory)
            self.session_combo.setItemData(self.session_combo.count() - 1, directory, Qt.ItemDataRole.ToolTipRole)
        selected = self.session_combo.findData(current)
        if selected >= 0:
            self.session_combo.setCurrentIndex(selected)
        self._sync_controls()
        if not self.state and not self._closing:
            self.status_label.setText(f'{self.session_combo.count() - 1} 个已保存项目' if self.session_combo.count() > 1 else '')

    def _open_catalog_selection(self):
        path = self.session_combo.currentData()
        if path:
            self.open_session(path)

    def open_session(self, path=None):
        if self.busy or self._closing:
            return False
        if path is None or isinstance(path, bool):
            path = QFileDialog.getExistingDirectory(self, '选择已保存的项目会话目录', str(self.project_root))
        if not path:
            return False
        source = Path(path).expanduser()
        if source.is_file():
            source = source.parent
        self._generation += 1
        preferred = (self.store.settings.get('project_report_views') or {}).copy()
        locations = (self.store.settings.get('project_report_locations') or {}).copy()
        def action(control):
            control.check()
            state = read_project(source)
            report, warning = None, ''
            runs = state.get('runs') or []
            reference = preferred.get(state['project_id'])
            if reference is not None:
                try:
                    validate_report_reference(reference)
                    report = resolve_report_reference(reference, locations.get(reference['report_sha256']))
                    if report['project_id'] != state['project_id']:
                        raise ValueError('保存的运行身份不属于当前会话。')
                except (OSError, ValueError, TypeError) as exc:
                    warning = '之前明确选中的运行无法打开，未改为最近一次：' + str(exc)
            for run in ([] if reference is not None else reversed(runs)):
                if not isinstance(run, dict) or not run.get('report_dir'):
                    continue
                try:
                    candidate = _read_report(run['report_dir'])
                    if candidate.get('project_id') != state.get('project_id'):
                        raise ValueError('运行记录与所选项目不匹配。')
                    report = candidate
                except (OSError, ValueError, TypeError) as exc:
                    warning = '最近报告无法读取：' + str(exc)
                break
            control.check()
            return {'state': state, 'report': report, 'warning': warning}
        def shown(result):
            self._apply_state(result['state'])
            if result['report'] is not None:
                self._apply_report(result['report'])
                available = set(self.collection.get('tests') or []) if self.collection else set()
                if not self._has_restored_selection and requested_selection(self.state) is None and self._pending_selection is None:
                    self._check_ids(set(result['report'].get('selected_tests') or []) & available, explicit=False)
                self.tabs.setCurrentIndex(2)
            else:
                self.tabs.setCurrentIndex(1 if self.collection else 0)
                self.status_label.setText('已重开固定项目会话；可继续发现或选择测试运行。')
            if result['warning']:
                self.status_label.setText(self.status_label.text() + '\n' + result['warning'])
                self._show_problem(result['warning'])
            self._refresh_catalog()
        return self._start(action, shown, '正在读取保存的固定输入、测试选择和运行记录…')

    def _show_runs(self):
        previous = self.report.get('report_dir') if self.report else None
        self.history_tree.clear()
        runs = (self.state.get('runs') or []) if self.state else []
        for run in reversed(runs):
            if not isinstance(run, dict):
                continue
            directory = str(run.get('report_dir') or '')
            status = str(run.get('status') or '')
            item = QTreeWidgetItem([str(run.get('run_id') or Path(directory).name),
                                   _RUN_STATUS.get(status, _STATUS.get(status, status)),
                                   str(run.get('created_at') or ''), directory])
            item.setData(0, _ROLE, run)
            item.setToolTip(3, directory)
            self.history_tree.addTopLevelItem(item)
            if previous and directory == previous:
                self.history_tree.setCurrentItem(item)
        self.history_label.setText(f'共 {len(runs)} 次运行 · ' + str(self.state.get('directory') or '') if self.state else '尚未打开项目会话')
        self._sync_controls()

    def open_selected_run(self):
        if self.busy or not self.state:
            return
        item = self.history_tree.currentItem()
        run = item.data(0, _ROLE) if item else None
        if not run or not run.get('report_dir'):
            return
        project_id = self.state.get('project_id')
        def action(control):
            control.check()
            report = _read_report(run['report_dir'])
            if report.get('project_id') != project_id:
                raise ValueError('这份报告不属于当前项目会话。')
            return report
        def shown(report):
            self._apply_report(report)
            self.tabs.setCurrentIndex(2)
        self._start(action, shown, '正在读取所选项目回归报告…')

    def compare_selected_runs(self):
        selected = [item.data(0, _ROLE) for item in self.history_tree.selectedItems()]
        if self.busy or len(selected) != 2:
            return
        selected.sort(key=lambda item: str(item.get('created_at') or ''))
        from .project_history import compare_project_reports
        def action(control):
            control.check()
            result = compare_project_reports(selected[0]['report_dir'], selected[1]['report_dir'])
            control.check()
            return result
        def shown(result):
            self._show_comparison(result)
        self._start(action, shown, '正在核对并对比所选两次运行的证据与测试范围…')

    def show_processing_history(self):
        if self.busy or not self.report or not self._report_reference:
            return False
        from .processing_history import read_processing_history
        from .desktop_processing_history import ProcessingHistoryDialog
        reference = dict(self._report_reference)
        locations = dict(self.store.settings.get('project_report_locations') or {})
        notes_root = self.project_root.parent / 'run-notes'
        def shown(data):
            dialog = self.processing_dialog = ProcessingHistoryDialog(data, notes_root, locations, self)
            dialog.setModal(True)
            dialog.busyChanged.connect(lambda _: self.busyChanged.emit(self.busy))
            def remember(fingerprint, path):
                saved = dict(self.store.settings.get('project_report_locations') or {})
                saved[fingerprint] = path
                self.store.settings['project_report_locations'] = dict(list(saved.items())[-200:])
                try:
                    self.store.save()
                except OSError as exc:
                    dialog.status_label.setText('来源已核对，但本机定位偏好保存失败：' + str(exc))
            dialog.locationChanged.connect(remember)
            def closed(*_):
                if self.processing_dialog is dialog:
                    self.processing_dialog = None
                dialog.deleteLater()
                self.busyChanged.emit(self.busy)
            dialog.finished.connect(closed)
            dialog.show()
        return self._start(lambda control: read_processing_history(reference['report_dir'], expected_report=reference,
            locations=locations, notes_root=notes_root, control=control), shown,
            '正在按保存的来源核对各轮报告和未复查项，不运行测试…', operation='processing_history')

    def _show_comparison(self, result):
        from .desktop_history import RunComparisonDialog
        self.comparison_dialog = RunComparisonDialog(result, self)
        self.comparison_dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.comparison_dialog.setModal(True)
        self.comparison_dialog.show()
        self.status_label.setText('已核对来源并比较保存的两次运行；没有重新执行测试。')

    def _open_local(self, path):
        try:
            path = Path(path).expanduser().absolute()
            if not path.exists():
                raise ValueError('文件或目录不存在：' + str(path))
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                raise ValueError('系统未能打开，请从保存目录手动查看：' + str(path))
            self.status_label.setText('已交给系统打开：' + str(path))
        except (OSError, ValueError) as exc:
            self._failed(str(exc))

    def open_report(self):
        if self.report:
            self._open_local(Path(self.report['report_dir']) / 'report.html')

    def open_evidence(self):
        if self.report:
            directory = Path(self.report['report_dir'])
            self._open_local(directory / 'evidence' if (directory / 'evidence').is_dir() else directory)

    def open_session_folder(self):
        if self.state:
            self._open_local(self.state['directory'])

    def check_report(self):
        if self.busy or not self.report:
            return
        directory = self.report['report_dir']
        def shown(result):
            status = result.get('status') if isinstance(result, dict) else None
            self.status_label.setText(('证据内容一致。' if status == 'consistent' else '证据检查发现问题，请查看详情。') + '\n' + directory)
            self.version_logs.setPlainText(_format_json(result, 50000) + '\n\n' + self._report_logs(self.report))
            self.details_tabs.setCurrentIndex(1)
        self._start(lambda control: check_project_report(directory), shown, '正在核对固定输入、所选清单与报告证据…')

    def export_report(self, *, include_sources=False):
        if self.busy or not self.report:
            return
        directory = self.report['report_dir']
        expected = dict(self._report_reference)
        locations = dict(self.store.settings.get('project_report_locations') or {})
        title = '导出本次运行及全部复查来源' if include_sources else '导出本次运行'
        suffix = '-history.zip' if include_sources else '.zip'
        destination, _ = QFileDialog.getSaveFileName(self, title,
                    str(Path(directory).parent / (Path(directory).name + suffix)), 'ZIP 证据包 (*.zip)')
        if not destination:
            return
        if not destination.lower().endswith('.zip'):
            destination += '.zip'
        def shown(result):
            self.status_label.setText(f'已导出 {result["report_count"]} 份项目报告：{destination}')
        return self._start(lambda control: pack_project_report(directory, destination,
            include_sources=include_sources, locations=locations, expected_report=expected, control=control), shown,
            '正在校验证据并导出项目报告 ZIP…')

    def import_bundle(self, path=None):
        """Verify and open a ZIP as historical evidence without executing code."""
        if self.busy or self._closing:
            return False
        if path is None or isinstance(path, bool):
            path, _ = QFileDialog.getOpenFileName(self, '导入项目报告 ZIP 证据包',
                        str(self.report_root), 'ZIP 证据包 (*.zip)')
        if not path:
            return False
        bundle = Path(path).expanduser()
        root = Path(self.store.settings.get('imports') or
                    (self.project_root.parent / 'project-imports')).expanduser()
        destination = root / ('project-import-' + uuid.uuid4().hex[:12])
        def action(control):
            control.check()
            imported = unpack_project_report(bundle, destination, control=control)
            control.check()
            directory = imported.get('report_dir') or imported.get('directory')
            if not directory:
                raise ValueError('导入结果缺少项目报告目录。')
            report = _read_report(directory)
            control.check()
            return {'imported': imported, 'report': report}
        def shown(result):
            report = result['report']
            if report.get('runner') in ('vitest', 'go'):
                self._open_local(Path(report['report_dir']) / 'report.html')
                runner = {'vitest': 'Vitest', 'go': 'Go'}[report['runner']]
                self.status_label.setText(f'已校验并导入 {runner} 报告。可从命令行使用 project replay 重放。')
                return
            self._apply_report(report)
            self.tabs.setCurrentIndex(2)
            notice = result['imported'].get('notice') or '项目证据已校验并导入。'
            self.status_label.setText(f'已导入 {result["imported"]["report_count"]} 份项目报告。' + str(notice)
                                      + '\n' + str(result['report']['report_dir']))
        return self._start(action, shown, '正在校验并导入项目报告 ZIP，只读取证据，不执行代码…')

    def replay_report(self):
        if self.busy or not self.report:
            return False
        directory = self.report['report_dir']
        settings = project_replay_settings(self.report, python_executable=self.python_edit.text().strip())
        python = settings['runtime']['executable']
        count = len(self.report.get('selected_tests') or self.report.get('entries') or [])
        source_timeout = settings['source_timeout']
        source_python = settings['source_runtime']['executable'] or '未记录'
        dialog = QDialog(self)
        dialog.setWindowTitle('重放项目报告')
        layout = QFormLayout(dialog)
        layout.addRow(_label(f'重放 {count} 个测试 · {directory}'))
        layout.addRow('原报告 Python', _label(source_python))
        layout.addRow('本次 Python', _label(python))
        layout.addRow('原报告每版限时', _label(f'{source_timeout:g} 秒' if source_timeout is not None else '未记录'))
        timeout_input = QDoubleSpinBox(dialog)
        timeout_input.setDecimals(3)
        timeout_input.setRange(.1, 3600)
        timeout_input.setSuffix(' 秒')
        timeout_input.setValue(settings['timeout'])
        initial_value = timeout_input.value()
        layout.addRow('本次每版限时', timeout_input)
        layout.addRow(_label('确认信任报告中的项目代码后，重放并保存新报告。'))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('重放')
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        timeout = settings['timeout'] if timeout_input.value() == initial_value else timeout_input.value()
        from .pytest_profile import read_pytest_profile
        from .desktop_runtime_environment import LocalValuesDialog
        local_values = self.store.local_runtime_values(self.report.get('repository'))
        try:
            profile = read_pytest_profile(Path(directory) / 'evidence/project/inputs/after')
            if profile.get('pass_env'):
                dialog = LocalValuesDialog(profile['pass_env'], local_values, self)
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return False
                local_values = dialog.values
        except (OSError, ValueError) as exc:
            self._failed(str(exc))
            return False
        output = self.report_root
        def action(control):
            return replay_project_report(directory, output, python_executable=python,
                                         timeout=timeout, confirmed=True, control=control)
        def shown(report):
            self._apply_report(report)
            self.tabs.setCurrentIndex(2)
            self._refresh_catalog()
        return self._start(action, shown, '正在明确重放保存的固定输入，结果将保存为新报告…',
                           runtime_repository=self.report.get('repository', ''), runtime_values=local_values)

    def prepare_ai(self):
        if self.busy or not self.state:
            return False
        if self._ai_plan_path and Path(self._ai_plan_path).is_dir():
            self._open_ai_plan()
            return True
        directory, issue = self.state['directory'], str(self.state.get('issue') or '')
        if not issue.strip():
            self.status_label.setText('请先填写问题描述，再重新冻结项目；AI 计划会使用同一份固定输入。')
            self.issue_edit.setFocus()
            return False
        python = self.python_edit.text().strip() or sys.executable
        output = self.project_root.parent / 'suites'
        def action(control):
            control.check()
            result = prepare_project_ai(directory, issue, output, python_executable=python)
            control.check()
            return result
        def shown(result):
            path = result.get('directory') if isinstance(result, dict) else None
            if not path:
                raise ValueError('AI 计划未返回保存目录。')
            self._ai_plan_path = str(path)
            self.ai_button.setText('打开 AI 验收计划…')
            self.status_label.setText('已从项目固定输入准备 AI 计划。打开后可审阅内容，再决定是否发送到模型。')
            self._after_finished = self._open_ai_plan
        return self._start(action, shown, '正在从同一组固定快照准备 AI 验收计划；不会请求模型…')

    def _open_ai_plan(self):
        if self.busy or self._closing or not self._ai_plan_path:
            return
        from .desktop_suite import AISuiteDialog
        try:
            self.ai_dialog = AISuiteDialog(self.store, self)
            self.ai_dialog.load_plan(self._ai_plan_path)
            self.ai_dialog.exec()
        except (OSError, ValueError) as exc:
            self._failed(str(exc))
