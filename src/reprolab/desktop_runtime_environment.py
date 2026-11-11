"""Reviewable variable declarations and memory-only local values."""
import os
from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (QWidget, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QTableWidget, QHeaderView, QAbstractItemView, QLineEdit, QComboBox, QPushButton, QDialogButtonBox, QScrollArea)
from .pytest_profile import read_pytest_profile
from .runtime_environment import validate_variables, validate_value, resolve_variables
from .project_config import preview_configuration, apply_configuration


class LocalValuesDialog(QDialog):
    def __init__(self, names, initial=None, parent=None):
        super().__init__(parent)
        self.names = names
        self.values = dict(initial or {})
        self.setWindowTitle('本次验收的本机变量')
        self.resize(660, 420)
        layout = QVBoxLayout(self)
        note = QLabel('这些值仅在当前应用会话使用，不写入配置或运行条件记录。留空使用启动进程环境；项目日志仍可能自行输出值。')
        note.setWordWrap(True); layout.addWidget(note)
        fields = QWidget(); form = QFormLayout(fields); self.edits = {}
        for name in names:
            field = QLineEdit(self.values.get(name, ''))
            field.setEchoMode(QLineEdit.EchoMode.Password)
            field.setPlaceholderText('启动环境已提供' if name in os.environ else '需要填写')
            field.setAccessibleName(name)
            label = QLabel(name); label.setWordWrap(True); label.setMaximumWidth(220)
            form.addRow(label, field); self.edits[name] = field
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(fields)
        layout.addWidget(scroll, 1)
        self.error = QLabel(); self.error.setTextFormat(Qt.TextFormat.PlainText); self.error.setWordWrap(True); layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def accept(self):
        try:
            values = {name: field.text() for name, field in self.edits.items() if field.text()}
            resolve_variables({'pass_env': self.names}, values)
        except ValueError as exc:
            self.error.setText(str(exc)); return
        self.values = values
        super().accept()


class RuntimeVariablesPanel(QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        layout = QVBoxLayout(self)
        note = QLabel('普通值随项目保存；本机值仅本次使用，测试日志仍需检查。')
        note.setWordWrap(True); layout.addWidget(note)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['变量名称', '值的来源', '值'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 240)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(1, 230)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide(); layout.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        self.add_button = QPushButton('新增变量'); self.remove_button = QPushButton('删除所选')
        self.apply_button = QPushButton('预览并应用声明'); self.apply_button.setObjectName('primary')
        self.use_button = QPushButton('使用本次值'); self.clear_button = QPushButton('清除本次值')
        buttons.addWidget(self.add_button); buttons.addWidget(self.remove_button); buttons.addStretch(1); buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)
        actions = QHBoxLayout()
        self.undo_button = QPushButton('撤销上次配置')
        self.undo_button.setEnabled(False)
        actions.addWidget(self.use_button); actions.addWidget(self.clear_button); actions.addStretch(1)
        actions.addWidget(self.undo_button); layout.addLayout(actions)
        self.undo_button.clicked.connect(owner.undo_plugin_configuration)
        self.notice = QLabel(); self.notice.setTextFormat(Qt.TextFormat.PlainText); self.notice.setWordWrap(True); layout.addWidget(self.notice)
        self.apply_button.setToolTip('普通值和本机变量名称会写入配置，本机值仅保留在当前会话。\nPATH、PYTHON*、PYTEST* 及加载器控制变量不可覆盖。')
        self.add_button.clicked.connect(lambda: self.add_variable())
        self.remove_button.clicked.connect(self.remove_selected)
        self.apply_button.clicked.connect(self.apply_declarations)
        self.use_button.clicked.connect(self.use_values)
        self.clear_button.clicked.connect(self.clear_values)
        self.reload()

    def reload(self):
        try:
            profile = read_pytest_profile(self.owner.repository or self.owner.project)
        except (OSError, ValueError) as exc:
            self.notice.setText(str(exc)); return
        self.table.setRowCount(0)
        for name, value in profile.get('environment', {}).items(): self.add_variable(name, value, local=False)
        for name in profile.get('pass_env', []): self.add_variable(name, self.owner.runtime_values.get(name, ''), local=True)
        self.apply_button.setEnabled(bool(self.owner.repository))
        self.notice.setToolTip('修改目标：' + str(Path(self.owner.repository) / 'pyproject.toml') if self.owner.repository else '当前为冻结快照')
        self.notice.setText('' if self.owner.repository else '当前快照只读，可填写本次值。')

    def add_variable(self, name='', value='', *, local=True):
        row = self.table.rowCount()
        if row >= 64:
            self.notice.setText('最多 64 个变量。'); return
        self.table.insertRow(row)
        key = QLineEdit(name); key.setPlaceholderText('例如 DJANGO_SETTINGS_MODULE'); key.setAccessibleName('变量名称')
        mode = QComboBox(); mode.addItem('普通值 · 随快照保存', 'public'); mode.addItem('本机值 · 不保存值', 'local')
        mode.setCurrentIndex(1 if local else 0); mode.setAccessibleName('值的来源')
        field = QLineEdit(value); field.setAccessibleName('变量值')
        def style():
            field.setEchoMode(QLineEdit.EchoMode.Password if mode.currentData() == 'local' else QLineEdit.EchoMode.Normal)
            field.setPlaceholderText('留空使用启动进程环境' if mode.currentData() == 'local' else '会保存到配置和证据')
        mode.currentIndexChanged.connect(style); style()
        for col, widget in enumerate((key, mode, field)):
            self.table.setCellWidget(row, col, widget)
            widget.installEventFilter(self)
        self.table.setCurrentCell(row, 0)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.FocusIn:
            index = self.table.indexAt(watched.pos())
            if index.isValid(): self.table.setCurrentCell(index.row(), index.column())
        return super().eventFilter(watched, event)

    @property
    def refresh_button(self):
        return self.owner.refresh_configuration_button

    def sync_actions(self):
        self.undo_button.setEnabled(self.owner.undo_plugins_button.isEnabled())

    def remove_selected(self):
        rows = {index.row() for index in self.table.selectedIndexes()}
        if not rows and self.table.currentRow() >= 0: rows.add(self.table.currentRow())
        for row in sorted(rows, reverse=True): self.table.removeRow(row)

    def draft(self):
        public, names, local, seen = {}, [], {}, set()
        for row in range(self.table.rowCount()):
            name, mode, value = (self.table.cellWidget(row, col) for col in range(3))
            key, text = name.text().strip(), value.text()
            if not key and not text: continue
            if key in seen: raise ValueError('变量名称重复：' + key)
            seen.add(key)
            if mode.currentData() == 'local':
                names.append(key)
                if text: validate_value(key, text); local[key] = text
            else: public[key] = text
        public, names = validate_variables(public, names)
        return public, names, local

    def use_values(self):
        try:
            public, names, local = self.draft()
            saved = read_pytest_profile(self.owner.repository or self.owner.project)
            if public != saved.get('environment', {}) or names != saved.get('pass_env', []):
                raise ValueError('变量声明尚未应用，请先预览并应用声明；已有快照还需刷新。')
            resolve_variables(saved, local)
        except (OSError, ValueError) as exc:
            self.notice.setText(str(exc)); return
        self.owner.runtime_values = local
        self.notice.setText('本机值已用于当前应用会话；不会写入设置、配置或条件记录。测试日志仍可能包含自行输出的值。')

    def clear_values(self):
        self.owner.runtime_values = {}
        for row in range(self.table.rowCount()):
            if self.table.cellWidget(row, 1).currentData() == 'local': self.table.cellWidget(row, 2).clear()
        self.notice.setText('已清除界面提供的本次值；声明仍保留，启动进程环境中的同名变量仍可提供值。')

    def apply_declarations(self):
        if self.owner.task or not self.owner.repository: return
        try:
            public, names, local = self.draft()
            profile = read_pytest_profile(self.owner.repository)
            plan = preview_configuration(self.owner.repository, profile['plugins'], profile['import_mode'], environment=public, pass_env=names)
            if not plan.changed:
                self.use_values(); return
            if not self.owner._review_configuration(plan): return
            def saved(result):
                self.owner.runtime_values = local
                self.owner._configuration_applied(result)
                self.reload()
            self.owner._start(lambda control: apply_configuration(plan, self.owner.config_history_root, control=control),
                saved, '正在保存运行变量声明；本机值只保留在当前会话…', deliver_on_close=True)
        except (OSError, ValueError) as exc:
            self.notice.setText('未应用：' + str(exc))
