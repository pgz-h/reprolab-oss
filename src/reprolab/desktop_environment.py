"""Inspect selected project Python metadata and preview a new external venv."""
from pathlib import Path
import sys
import uuid

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QListWidget, QListWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget,
                               QHeaderView, QAbstractItemView)

from .desktop_tasks import Task
from .project_environment import inspect_project, plan_environment, prepare_environment, candidate_pythons
from .pytest_profile import describe_pytest_profile, render_pytest_profile, read_pytest_profile
from .execution_settings import describe_execution_settings, inspect_execution_settings
from .project_config import (preview_configuration, apply_configuration, latest_configuration_edit,
                             preview_configuration_undo, undo_configuration, configuration_requires_refresh)



_STATUS = {'satisfied': '满足', 'missing': '缺少', 'incompatible': '版本不符',
           'unknown': '无法确认', 'needs-review': '需人工检查',
           'not-applicable': '当前环境不适用'}


class ConfigurationPreviewDialog(QDialog):
    def __init__(self, plan, parent=None):
        super().__init__(parent)
        self.setWindowTitle('预览原项目配置修改' if not plan.undo_record else '预览撤销配置修改')
        self.resize(850, 610)
        layout = QVBoxLayout(self)
        location = QLabel('将修改原项目文件：' + str(plan.path))
        location.setTextFormat(Qt.TextFormat.PlainText)
        location.setWordWrap(True)
        location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(location)
        self.diff_view = QPlainTextEdit()
        self.diff_view.setReadOnly(True)
        self.diff_view.setPlainText(plan.diff or '原项目配置已经与所选设置一致，无需写入。')
        layout.addWidget(self.diff_view, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('返回')
        self.apply_button = buttons.addButton('应用以上修改' if not plan.undo_record else '撤销以上修改', QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_button.setObjectName('primary')
        self.apply_button.setEnabled(plan.changed)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ProjectEnvironmentDialog(QDialog):
    """Tasks own their lifetime; a close request cancels before destroying Qt."""

    def __init__(self, project, python_executable='', work_root=None, parent=None, *, selected_groups=None, repository=None, can_refresh=False, local_values=None, can_collect=False):
        super().__init__(parent)
        self.project = str(Path(project).expanduser().resolve())
        self.repository = str(Path(repository).expanduser().resolve()) if repository else None
        self.can_refresh = can_refresh
        self.can_collect = can_collect
        self.runtime_values = dict(local_values or {})
        self.configuration_changed = False
        self.configuration_warning = ""
        self.configuration_requires_refresh = False
        self.refresh_requested = False
        self.configuration_preview = None
        self._last_config_record = None
        self._refresh_after_config = False
        self.task = None
        self.inspection = None
        self.plan_data = None
        self.prepared_environment = None
        self.result_data = {}
        self._selected_groups = list(selected_groups or [])
        self._plugin_draft = None
        self._updating_plugins = False
        self._group_timer = QTimer(self)
        self._group_timer.setSingleShot(True)
        self._group_timer.timeout.connect(self.inspect_environment)
        self._closing = False
        self.setWindowTitle('项目环境检查与准备')
        self.resize(960, 780)
        layout = QVBoxLayout(self)
        self.content = QWidget()
        body = QVBoxLayout(self.content)
        body.setContentsMargins(0, 0, 0, 0)
        project_label = QLabel('项目：' + Path(self.repository or self.project).name + (' · 当前快照' if self.can_refresh else ''))
        project_label.setToolTip('检查来源：' + self.project)
        project_label.setTextFormat(Qt.TextFormat.PlainText)
        project_label.setWordWrap(True)
        project_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(project_label)
        form = QFormLayout()
        self.python_combo = QComboBox()
        self.python_combo.setEditable(True)
        self.python_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.python_combo.lineEdit().setPlaceholderText('内置 Python')
        self.python_combo.addItem('')
        candidate_error = ''
        try:
            candidates = candidate_pythons(self.repository or self.project)
        except (OSError, ValueError) as exc:
            candidates = []
            candidate_error = '无法列出原项目环境：' + str(exc)
        self.python_combo.addItems(list(dict.fromkeys([*candidates, *([str(python_executable)] if python_executable else [])])))
        self.python_combo.setEditText(str(python_executable))
        self.python_combo.setToolTip(candidate_error or '候选路径来自原项目；只列出路径，不自动切换或启动。')
        self.python_button = QPushButton('选择 Python…')
        self.inspect_button = QPushButton('检查所选环境')
        form.addRow('Python', self._row(self.python_combo, self.python_button, self.inspect_button))
        body.addLayout(form)
        self.runtime_label = QLabel('正在读取项目环境…')
        self.runtime_label.setWordWrap(True)
        self.runtime_label.setTextFormat(Qt.TextFormat.PlainText)
        body.addWidget(self.runtime_label)
        self.tabs = QTabWidget()
        check_page, plugin_page, creation_page = QWidget(), QWidget(), QWidget()
        checks, plugins, creation = QVBoxLayout(check_page), QVBoxLayout(plugin_page), QVBoxLayout(creation_page)
        self.tabs.addTab(check_page, '检查与使用')
        self.tabs.addTab(plugin_page, 'pytest 插件')
        self.execution_view = QPlainTextEdit()
        self.execution_view.setReadOnly(True)
        from .desktop_runtime_environment import RuntimeVariablesPanel
        self.variables_panel = RuntimeVariablesPanel(self)
        self.tabs.addTab(self.variables_panel, '运行变量')
        self.tabs.addTab(self.execution_view, '运行差异')
        self.tabs.addTab(creation_page, '准备新环境')
        body.addWidget(self.tabs, 1)
        self.groups_panel = QWidget()
        groups = QVBoxLayout(self.groups_panel)
        groups.setContentsMargins(0, 0, 0, 0)
        groups.addWidget(QLabel('依赖组'))
        self.groups_panel.setToolTip('基础依赖始终包含；可额外选择测试或开发依赖组。')
        self.group_list = QListWidget()
        self.group_list.setMaximumHeight(110)
        groups.addWidget(self.group_list)
        self.recommended_groups_button = QPushButton('选择推荐测试组')
        self.group_status = QLabel()
        groups.addWidget(self._row(self.group_status, self.recommended_groups_button))
        checks.addWidget(self.groups_panel)
        self.groups_panel.hide()
        self.dependency_summary = QLabel()
        self.dependencies_filter = QCheckBox('只看需处理项')
        self.copy_dependencies_button = QPushButton('复制环境摘要')
        self.copy_dependencies_button.setEnabled(False)
        checks.addWidget(self._row(self.dependency_summary, self.dependencies_filter, self.copy_dependencies_button))
        self.pytest_profile_label = QLabel()
        self.pytest_profile_label.setWordWrap(True)
        self.pytest_profile_label.setTextFormat(Qt.TextFormat.PlainText)
        self.pytest_profile_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        plugins.addWidget(self.pytest_profile_label)
        self.pytest_profile_label.hide()
        self.dependencies_table = QTableWidget(0, 4)
        self.dependencies_table.setHorizontalHeaderLabels(['依赖', '声明版本', '已安装', '状态'])
        self.dependencies_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.dependencies_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.dependencies_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.dependencies_table.verticalHeader().setVisible(False)
        self.dependencies_table.setMinimumHeight(150)
        checks.addWidget(self.dependencies_table, 1)
        self.issues_label = QLabel()
        self.issues_label.setWordWrap(True)
        self.issues_label.setTextFormat(Qt.TextFormat.PlainText)
        checks.addWidget(self.issues_label)
        self.plugin_tree = QTreeWidget()
        self.plugin_tree.setHeaderLabels(['配置中使用的名称', '安装包', '版本', '当前状态'])
        self.plugin_tree.setRootIsDecorated(False)
        self.plugin_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.plugin_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        plugins.addWidget(self.plugin_tree, 1)
        self.plugin_name = QLineEdit()
        self.plugin_name.setPlaceholderText('插件模块或入口名，例如 pytest_asyncio.plugin')
        self.add_plugin_button = QPushButton('添加插件')
        plugins.addWidget(self._row(self.plugin_name, self.add_plugin_button))
        self.plugin_mode = QComboBox()
        for label, value in (('沿用 pytest 默认', None), ('importlib', 'importlib'),
                             ('prepend', 'prepend'), ('append', 'append')):
            self.plugin_mode.addItem(label, value)
        self.copy_plugins_button = QPushButton('复制所选插件配置')
        self.copy_plugins_button.setEnabled(False)
        plugins.addWidget(self._row(QLabel('导入方式'), self.plugin_mode, self.copy_plugins_button))
        self.plugin_preview = QPlainTextEdit()
        self.plugin_preview.setReadOnly(True)
        self.plugin_preview.setMaximumHeight(110)
        plugins.addWidget(self.plugin_preview)
        self.plugin_notice = QLabel('读取安装元数据后，可以勾选插件生成配置；不会加载插件或修改源码快照。')
        self.plugin_notice.setWordWrap(True)
        self.plugin_notice.setTextFormat(Qt.TextFormat.PlainText)
        plugins.addWidget(self.plugin_notice)
        self.edit_plugins_button = QPushButton('预览并应用到原项目…')
        self.edit_plugins_button.setObjectName('primary')
        self.undo_plugins_button = QPushButton('撤销上次配置修改…')
        plugins.addWidget(self._row(self.edit_plugins_button, self.undo_plugins_button))
        self.configuration_notice = QLabel()
        self.configuration_notice.setTextFormat(Qt.TextFormat.PlainText)
        self.configuration_notice.setWordWrap(True)
        plugins.addWidget(self.configuration_notice)
        creation_note = QLabel('准备新环境：使用上方选定的独立 Python，在项目外创建新目录；先预览，再明确创建。')
        creation_note.setWordWrap(True)
        creation.addWidget(creation_note)
        default_root = Path(work_root).expanduser() if work_root else Path(self.project).parent
        while not default_root.is_dir() and default_root != default_root.parent:
            default_root = default_root.parent
        self.config_history_root = default_root / 'project-config-edits'
        self.destination_edit = QLineEdit(str(default_root / ('reprolab-env-' + uuid.uuid4().hex[:8])))
        self.destination_button = QPushButton('选择父目录…')
        self.preview_button = QPushButton('预览准备计划')
        creation_form = QFormLayout()
        creation_form.addRow('新环境目录', self._row(self.destination_edit, self.destination_button))
        creation_form.addRow('', self.preview_button)
        creation.addLayout(creation_form)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText('预览将显示目标目录、基础 Python、公开索引及完整安装声明。')
        self.preview.setMaximumHeight(170)
        creation.addWidget(self.preview, 1)
        self.create_button = QPushButton('按以上预览创建新环境（联网安装）')
        self.create_button.setEnabled(False)
        creation.addWidget(self.create_button)
        creation.addStretch(1)
        layout.addWidget(self.content, 1)
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.reuse_review = QCheckBox('已核对未确认的依赖声明，继续使用现有环境')
        self.reuse_review.setToolTip('这里只采用所选 Python；未确认项仍保留，发现与测试的实际结果决定能否继续。')
        self.reuse_review.hide()
        layout.addWidget(self.reuse_review)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('关闭')
        self.cancel_button = self.buttons.addButton('取消当前操作', QDialogButtonBox.ButtonRole.ActionRole)
        self.cancel_button.setEnabled(False)
        self.use_button = self.buttons.addButton('使用已检查环境', QDialogButtonBox.ButtonRole.AcceptRole)
        self.use_button.setEnabled(False)
        self.use_collect_button = self.buttons.addButton('使用环境并重新发现', QDialogButtonBox.ButtonRole.ActionRole)
        self.use_collect_button.setVisible(can_collect)
        self.use_collect_button.setEnabled(False)
        self.use_collect_button.setToolTip('沿用当前固定源码，重新执行测试收集；完成后核对范围，不运行测试主体。')
        self.refresh_configuration_button = self.buttons.addButton('刷新工作区使用配置', QDialogButtonBox.ButtonRole.ActionRole)
        self.refresh_configuration_button.setVisible(bool(can_refresh))
        self.refresh_configuration_button.setEnabled(False)
        layout.addWidget(self.buttons)
        self.python_button.clicked.connect(self._choose_python)
        self.python_combo.currentTextChanged.connect(self._python_changed)
        self.inspect_button.clicked.connect(self.inspect_environment)
        self.destination_button.clicked.connect(self._choose_destination)
        self.destination_edit.textChanged.connect(self._invalidate_plan)
        self.preview_button.clicked.connect(self.preview_environment)
        self.create_button.clicked.connect(self.create_environment)
        self.use_button.clicked.connect(self.use_environment)
        self.use_collect_button.clicked.connect(lambda: self.use_environment(collect=True))
        self.reuse_review.toggled.connect(self._sync_use_actions)
        self.cancel_button.clicked.connect(self.cancel_operation)
        self.buttons.rejected.connect(self.reject)
        self.group_list.itemChanged.connect(self._groups_changed)
        self.recommended_groups_button.clicked.connect(self._select_recommended_groups)
        self.dependencies_filter.toggled.connect(self._filter_dependencies)
        self.copy_dependencies_button.clicked.connect(self.copy_environment_summary)
        self.plugin_tree.itemChanged.connect(self._update_plugin_preview)
        self.plugin_mode.currentIndexChanged.connect(self._update_plugin_preview)
        self.copy_plugins_button.clicked.connect(self.copy_plugin_configuration)
        self.add_plugin_button.clicked.connect(self.add_plugin)
        self.edit_plugins_button.clicked.connect(self.edit_plugin_configuration)
        self.undo_plugins_button.clicked.connect(self.undo_plugin_configuration)
        self.refresh_configuration_button.clicked.connect(self.request_configuration_refresh)
        self._sync_configuration_actions()
        QTimer.singleShot(0, self.inspect_environment)

    @property
    def selected_groups(self):
        return list(self._selected_groups)

    @staticmethod
    def _row(*widgets):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in widgets:
            layout.addWidget(widget)
        return row

    def _choose_python(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择可信的 Python 解释器',
                    self.python_combo.currentText(), options=QFileDialog.Option.DontResolveSymlinks)
        if path:
            self.python_combo.setEditText(path)

    def _choose_destination(self):
        parent = QFileDialog.getExistingDirectory(self, '选择新环境的父目录',
                                                   str(Path(self.destination_edit.text()).parent))
        if parent:
            self.destination_edit.setText(str(Path(parent) / ('reprolab-env-' + uuid.uuid4().hex[:8])))

    def _python_changed(self):
        self.inspection = None
        self.prepared_environment = None
        self.use_button.setEnabled(False)
        self.runtime_label.setText('Python 已改变，请重新检查。')
        self._group_timer.stop()
        self.reuse_review.setChecked(False)
        self._sync_use_actions()
        self.plugin_tree.clear()
        self.plugin_preview.clear()
        self.pytest_profile_label.clear()
        self.copy_plugins_button.setEnabled(False)
        self.copy_dependencies_button.setEnabled(False)
        self.dependencies_table.setRowCount(0)
        self.issues_label.clear()
        self._invalidate_plan()
        self._sync_configuration_actions()

    def _invalidate_plan(self):
        self.plan_data = None
        self.create_button.setEnabled(False)
        self.preview.clear()

    def _start(self, action, callback, message, *, deliver_on_close=False):
        if self.task or self._closing:
            return
        self._group_timer.stop()
        self.content.setEnabled(False)
        self.reuse_review.setEnabled(False)
        self.use_collect_button.setEnabled(False)
        self.use_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.refresh_configuration_button.setEnabled(False)
        self.status_label.setText(message)
        task = self.task = Task(action, self)
        def succeeded(result):
            if not self._closing or deliver_on_close:
                callback(result)
        def failed(error):
            if not self._closing:
                self.status_label.setText('未完成：' + error)
        def cancelled():
            if not self._closing:
                self.status_label.setText('已取消；如已创建新目录，其中保留未完成状态，可选择新目录重试。')
        task.succeeded.connect(succeeded, Qt.ConnectionType.QueuedConnection)
        task.failed.connect(failed, Qt.ConnectionType.QueuedConnection)
        task.cancelled.connect(cancelled, Qt.ConnectionType.QueuedConnection)
        task.progress.connect(self._progress, Qt.ConnectionType.QueuedConnection)
        task.finished.connect(self._finished)
        task.start()

    def _progress(self, phase, result):
        if not self._closing:
            self.status_label.setText({'creating': '正在创建新的外部虚拟环境…',
                                      'installing': '正在从公开 PyPI 安装预览中的 wheel 依赖…',
                                      'checking': '正在检查新环境中的依赖版本…'}.get(phase, '正在准备环境…'))

    def _finished(self):
        task = self.task
        self.task = None
        task.deleteLater()
        if self._closing:
            super().reject()
            return
        self.content.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self._sync_use_actions()
        self.create_button.setEnabled(self.plan_data is not None)
        self._sync_configuration_actions()
        if self._refresh_after_config:
            self._refresh_after_config = False
            QTimer.singleShot(0, self.inspect_environment)

    def inspect_environment(self):
        if self.task or self._closing:
            return
        selected = self.python_combo.currentText().strip() or None
        groups = list(self._selected_groups)
        self.inspection = None
        self.reuse_review.setChecked(False)
        self._sync_use_actions()
        self.copy_dependencies_button.setEnabled(False)
        self._invalidate_plan()
        def action(control):
            control.check()
            result = inspect_project(self.project, selected, selected_groups=groups)
            control.check()
            return result
        self._start(action, self._show_inspection, '正在读取静态依赖和所选 Python 元数据…')

    def _show_inspection(self, result):
        self.inspection = result
        self.reuse_review.setVisible(result['reuse']['status'] == 'review')
        selected = self.python_combo.currentText()
        self.python_combo.blockSignals(True)
        existing = {self.python_combo.itemText(i) for i in range(self.python_combo.count())}
        for path in result['candidate_pythons']:
            if path not in existing:
                self.python_combo.addItem(path)
        self.python_combo.setEditText(selected)
        self.python_combo.blockSignals(False)
        runtime = result['runtime']
        self.runtime_label.setText(f"Python {runtime.get('python_version') or '未知'} · 要求 {runtime['specifier']}")
        self.runtime_label.setToolTip(str(runtime.get('executable') or '无法确定解释器'))
        self.pytest_profile_label.setText('当前检查来源：' + describe_pytest_profile(result.get('pytest_profile')))
        self.execution_view.setPlainText(describe_execution_settings(result.get('execution_settings') or inspect_execution_settings(self.project)))
        self._show_groups(result)
        self._show_plugins(result)
        rows = result['dependencies']
        self.dependencies_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, value in enumerate((row['name'], row['specifier'] or '未限定',
                                            row['installed'] or '—', _STATUS[row['status']])):
                cell = QTableWidgetItem(value)
                cell.setData(Qt.ItemDataRole.UserRole, row['status'])
                if row.get('requirement'):
                    cell.setToolTip(row['requirement'])
                self.dependencies_table.setItem(index, column, cell)
        self.issues_label.setText('\n'.join(result['issues']))
        self._filter_dependencies()
        self.copy_dependencies_button.setEnabled(True)
        self.status_label.setToolTip(result['notice'])
        self.status_label.setText('' if result['status'] == 'ready' else
            '部分声明无法静态确认；核对需处理项后，可继续使用现有环境。' if result['reuse']['status'] == 'review' else
            '环境需要处理；请修复已知问题后重新检查。')

    def preview_environment(self):
        if self.task:
            return
        self._invalidate_plan()
        selected = self.python_combo.currentText().strip() or sys.executable
        destination = self.destination_edit.text().strip()
        groups = list(self._selected_groups)
        def action(control):
            control.check()
            result = plan_environment(self.project, destination, selected, selected_groups=groups)
            control.check()
            return result
        self._start(action, self._show_plan, '正在生成环境准备预览；尚未创建目录或安装依赖…')

    def _show_plan(self, plan):
        self.plan_data = plan
        self.preview.setPlainText('\n'.join([
            '新环境目录：' + plan['destination'],
            '基础 Python：' + plan['interpreter'],
            '公开索引：' + plan['index_url'],
            '依赖组：' + ('、'.join(plan.get('selected_groups') or []) or '仅基础依赖'),
            '安装声明：', *('  ' + requirement for requirement in plan['requirements']),
            plan['notice']]))
        self.status_label.setText('请核对以上目录和安装声明；点击“按以上预览创建新环境”后才执行。')

    def create_environment(self):
        if self.task or self.plan_data is None:
            return
        plan = self.plan_data
        self.plan_data = None
        self._start(lambda control: prepare_environment(plan, control=control), self._prepared,
                    '正在重新核对预览并创建全新外部环境…')

    def _prepared(self, record):
        self.prepared_environment = record
        self.python_combo.blockSignals(True)
        self.python_combo.setEditText(record['python'])
        self.python_combo.blockSignals(False)
        self._show_inspection(record['inspection'])
        self.tabs.setCurrentIndex(0)
        self.status_label.setText('新环境已准备并检查完成。点击“使用已检查环境”用于后续验收。')

    def _can_use_environment(self):
        if self.task or self._closing or not self.inspection:
            return False
        status = self.inspection['reuse']['status']
        return status == 'ready' or (status == 'review' and self.reuse_review.isChecked())

    def _sync_use_actions(self, *_):
        usable = self._can_use_environment()
        self.reuse_review.setEnabled(self.task is None and not self._closing)
        self.use_button.setEnabled(usable)
        review = self.inspection and self.inspection['reuse']['status'] == 'review'
        self.use_button.setText('使用已核对环境' if review else '使用已检查环境')
        self.use_collect_button.setEnabled(bool(usable and self.can_collect))

    def use_environment(self, *, collect=False):
        if not self._can_use_environment() or (collect and not self.can_collect):
            return
        if collect:
            try:
                profile = read_pytest_profile(self.project)
                if self.repository and configuration_requires_refresh(self.project, self.repository):
                    raise ValueError('原项目配置已改变，请先刷新工作区，再发现测试。')
                draft = self._plugin_draft or {}
                if any(draft.get(key) != profile.get(key) for key in ('plugins', 'import_mode')):
                    raise ValueError('插件配置草稿尚未应用，请先预览并应用配置，再刷新工作区。')
                public, names, local = self.variables_panel.draft()
                if public != profile.get('environment', {}) or names != profile.get('pass_env', []):
                    raise ValueError('运行变量声明尚未用于当前快照，请先应用声明并刷新工作区。')
                from .runtime_environment import resolve_variables
                resolve_variables(profile, local)
                self.runtime_values = local
            except (OSError, ValueError) as exc:
                self.status_label.setText('尚未重新发现：' + str(exc))
                return
        selected = self.inspection['runtime']['executable']
        self.result_data = {'python': selected, 'python_executable': selected,
                            'environment': self.inspection, 'selected_groups': self.inspection.get('selected_groups', []),
                            'prepared_environment': self.prepared_environment,
                            'review_acknowledged': self.inspection['reuse']['status'] == 'review',
                            'collect_requested': collect}
        self.accept()

    def _show_groups(self, result):
        self._selected_groups = list(result.get('selected_groups') or [])
        choices = result.get('available_groups') or []
        self.group_list.blockSignals(True)
        self.group_list.clear()
        for group in choices:
            kind = '依赖组' if group['kind'] == 'group' else '可选依赖'
            suffix = ' · 未找到' if group.get('missing') else ' · 推荐测试' if group.get('recommended') else ''
            item = QListWidgetItem(f"{group['name']}  ·  {kind}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, group)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if group['id'] in self._selected_groups else Qt.CheckState.Unchecked)
            self.group_list.addItem(item)
        self.group_list.blockSignals(False)
        self.groups_panel.setVisible(bool(choices))
        self.group_status.setText(f'已选 {len(self._selected_groups)} 组；勾选后重新核对，安装前仍会预览。')
        self.recommended_groups_button.setEnabled(any(group.get('recommended') for group in choices))

    def _groups_changed(self, *_):
        self._selected_groups = [self.group_list.item(index).data(Qt.ItemDataRole.UserRole)['id']
            for index in range(self.group_list.count()) if self.group_list.item(index).checkState() == Qt.CheckState.Checked]
        self.inspection = None
        self.reuse_review.setChecked(False)
        self._sync_use_actions()
        self.copy_dependencies_button.setEnabled(False)
        self._invalidate_plan()
        self.group_status.setText(f'已选 {len(self._selected_groups)} 组，正在更新检查…')
        self._group_timer.start(200)

    def _select_recommended_groups(self):
        self.group_list.blockSignals(True)
        for index in range(self.group_list.count()):
            item = self.group_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole).get('recommended'):
                item.setCheckState(Qt.CheckState.Checked)
        self.group_list.blockSignals(False)
        self._groups_changed()

    def _filter_dependencies(self, *_):
        needs = 0
        for index in range(self.dependencies_table.rowCount()):
            item = self.dependencies_table.item(index, 0)
            attention = item.data(Qt.ItemDataRole.UserRole) not in ('satisfied', 'not-applicable')
            needs += int(attention)
            self.dependencies_table.setRowHidden(index, self.dependencies_filter.isChecked() and not attention)
        self.dependency_summary.setText(f'依赖声明 {self.dependencies_table.rowCount()} 条 · 需处理 {needs} 条')

    def copy_environment_summary(self):
        if not self.inspection:
            return
        result = self.inspection
        lines = ['ReproLab 项目环境', 'Python：' + str(result['runtime'].get('executable') or '未知'),
                 '依赖组：' + ('、'.join(result.get('selected_groups') or []) or '仅基础依赖')]
        for row in result['dependencies']:
            if row['status'] not in ('satisfied', 'not-applicable'):
                lines.append(f"{row['name']}{row['specifier']} · {_STATUS[row['status']]} · 已安装 {row['installed'] or '无'}")
        lines.extend(result['issues'])
        lines.append(result['notice'])
        QApplication.clipboard().setText('\n'.join(lines))
        self.status_label.setText('已复制环境摘要，可交给开发者处理缺少或不兼容的依赖。')

    def _show_plugins(self, result):
        self._updating_plugins = True
        self.plugin_tree.clear()
        profile = result.get('pytest_profile') or {'plugins': [], 'import_mode': None}
        original_profile = None
        if self.repository:
            try:
                original_profile = read_pytest_profile(self.repository)
            except (OSError, ValueError):
                pass  # The configuration notice below exposes the original-file error.
        if self._plugin_draft is None and original_profile is not None:
            self._plugin_draft = original_profile
        draft = self._plugin_draft or profile
        inventory = result.get('pytest_plugins') or {}
        available, declared = inventory.get('available') or [], inventory.get('declared') or []
        names = list(dict.fromkeys([*profile['plugins'], *draft['plugins']]))
        rows = []
        for name in names:
            check = next((row for row in declared if row['name'] == name), None)
            match = next((row for row in available if name in (row['name'], row['module'])), {})
            state = (check or {}).get('status', 'installed' if match else 'unknown')
            labels = {'installed': '已安装', 'local': '项目内模块', 'ambiguous': '入口冲突', 'unknown': '待运行确认'}
            rows.append((name, match.get('distribution') or (check or {}).get('distribution') or '—',
                         match.get('version') or (check or {}).get('version') or '—',
                         ('当前已声明 · ' if name in profile['plugins'] else '原项目已保存 · ' if name in (original_profile or {}).get('plugins', []) else '配置草稿 · ') + labels[state], match.get('module', name)))
        for entry in available:
            if not any(name in (entry['name'], entry['module']) for name in names):
                rows.append((entry['name'], entry['distribution'], entry['version'], '已安装，可选', entry['module']))
        for name, distribution, version, status, module in rows:
            item = QTreeWidgetItem([name, distribution, version, status])
            item.setData(0, Qt.ItemDataRole.UserRole, name)
            item.setToolTip(0, '插件模块：' + module)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if name in draft['plugins'] else Qt.CheckState.Unchecked)
            self.plugin_tree.addTopLevelItem(item)
        self.plugin_mode.setCurrentIndex(max(0, self.plugin_mode.findData(draft.get('import_mode'))))
        self.plugin_tree.setToolTip(str(inventory.get('notice') or '') + '\n勾选后预览并应用配置。')
        self.plugin_notice.setText(str(inventory.get('notice') or '') if inventory.get('status') != 'recorded' else '')
        self._updating_plugins = False
        self._update_plugin_preview()

    def _update_plugin_preview(self, *_):
        if self._updating_plugins:
            return
        names = [self.plugin_tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole)
                 for index in range(self.plugin_tree.topLevelItemCount())
                 if self.plugin_tree.topLevelItem(index).checkState(0) == Qt.CheckState.Checked]
        mode = self.plugin_mode.currentData()
        try:
            original = read_pytest_profile(self.repository or self.project)
            text = render_pytest_profile(names, mode, environment=original.get('environment'), pass_env=original.get('pass_env'))
        except ValueError as exc:
            self.plugin_preview.setPlainText(str(exc))
            self.copy_plugins_button.setEnabled(False)
            return
        self._plugin_draft = {'plugins': names, 'import_mode': mode}
        self.plugin_preview.setPlainText(text)
        self.copy_plugins_button.setEnabled(self.inspection is not None)
        self._sync_configuration_actions()

    def _sync_configuration_actions(self):
        idle = self.task is None and not self._closing
        self.edit_plugins_button.setEnabled(bool(idle and self.repository and self._plugin_draft is not None))
        self.undo_plugins_button.setEnabled(False)
        self.refresh_configuration_button.setEnabled(False)
        if not self.repository:
            self.configuration_notice.setText('当前快照只读')
            return
        try:
            self._last_config_record = latest_configuration_edit(self.repository, self.config_history_root)
            self.undo_plugins_button.setEnabled(idle and self._last_config_record is not None)
            original = read_pytest_profile(self.repository)
            current = read_pytest_profile(self.project)
            different = configuration_requires_refresh(self.project, self.repository)
            self.configuration_requires_refresh = bool(self.can_refresh and different)
            self.refresh_configuration_button.setEnabled(idle and self.configuration_requires_refresh)
            self.configuration_notice.setToolTip('修改目标：' + str(Path(self.repository) / 'pyproject.toml'))
            self.configuration_notice.setText('配置已更改，刷新工作区后生效。' if different else '')
        except (OSError, ValueError) as exc:
            self._last_config_record = None
            self.configuration_notice.setText('原项目配置需核对：' + str(exc))
        self.variables_panel.sync_actions()

    def add_plugin(self):
        if self.task:
            return
        name = self.plugin_name.text().strip()
        try:
            names = list((self._plugin_draft or {}).get('plugins') or [])
            render_pytest_profile(list(dict.fromkeys([*names, name])), self.plugin_mode.currentData())
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return
        for index in range(self.plugin_tree.topLevelItemCount()):
            item = self.plugin_tree.topLevelItem(index)
            if item.data(0, Qt.ItemDataRole.UserRole) == name:
                item.setCheckState(0, Qt.CheckState.Checked)
                self.plugin_name.clear()
                return
        item = QTreeWidgetItem([name, '—', '—', '手动添加 · 待运行确认'])
        item.setData(0, Qt.ItemDataRole.UserRole, name)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        self.plugin_tree.addTopLevelItem(item)
        item.setCheckState(0, Qt.CheckState.Checked)
        self.plugin_name.clear()
        self.status_label.setText('已加入配置草稿；尚未加载插件或写入项目。')

    def _review_configuration(self, plan):
        dialog = self.configuration_preview = ConfigurationPreviewDialog(plan, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        self.configuration_preview = None
        dialog.deleteLater()
        return accepted

    def edit_plugin_configuration(self):
        if self.task or not self.repository or self._plugin_draft is None:
            return
        try:
            plan = preview_configuration(self.repository, self._plugin_draft['plugins'], self._plugin_draft['import_mode'])
            if not self._review_configuration(plan):
                self._sync_configuration_actions()
                return
            self._start(lambda control: apply_configuration(plan, self.config_history_root, control=control),
                        self._configuration_applied, '正在核对预览并保存原项目配置…', deliver_on_close=True)
        except (OSError, ValueError) as exc:
            self.status_label.setText('未修改原项目：' + str(exc))

    def undo_plugin_configuration(self):
        if self.task or not self.repository or not self._last_config_record:
            return
        try:
            plan = preview_configuration_undo(self.repository, self._last_config_record)
            if not self._review_configuration(plan):
                return
            self._start(lambda control: undo_configuration(plan, control=control), self._configuration_applied,
                        '正在核对并撤销这次配置修改…', deliver_on_close=True)
        except (OSError, ValueError) as exc:
            self.status_label.setText('未撤销配置：' + str(exc))

    def _configuration_applied(self, result):
        self.variables_panel.reload()
        self.configuration_changed = result['status'] in ('applied', 'undone') or self.configuration_changed
        self.configuration_warning = result.get('warning') or ''
        self._plugin_draft = None
        self._invalidate_plan()
        self._refresh_after_config = self.repository == self.project
        if self.inspection:
            self._show_plugins(self.inspection)
        action = '已撤销配置修改。' if result['status'] == 'undone' else '配置已保存到原项目。'
        self.status_label.setText(result.get('warning') or action + ('点击“刷新工作区使用配置”准备新快照；不会自动运行测试。' if self.can_refresh else '后续冻结项目时使用新配置。'))

    def request_configuration_refresh(self):
        if self.task or not self.refresh_configuration_button.isEnabled():
            return
        self.refresh_requested = True
        self._group_timer.stop()
        if self._can_use_environment():
            self.use_environment()
        else:
            self.accept()

    def copy_plugin_configuration(self):
        if not self.copy_plugins_button.isEnabled():
            return
        QApplication.clipboard().setText(self.plugin_preview.toPlainText())
        self.status_label.setText('已复制插件配置。替换原项目中的同名配置表，再刷新工作区；当前快照保持原样。')

    def cancel_operation(self):
        if self.task:
            self.task.control.cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText('正在取消当前操作并等待子进程结束…')

    def reject(self):
        self._group_timer.stop()
        if self.task:
            self._closing = True
            self.task.control.cancel()
            self.content.setEnabled(False)
            self.buttons.setEnabled(False)
            self.status_label.setText('正在取消并等待当前操作结束，随后关闭…')
        else:
            self._closing = True
            super().reject()

    def closeEvent(self, event):
        self._group_timer.stop()
        if self.task:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
