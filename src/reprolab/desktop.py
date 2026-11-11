"""Chinese Qt desktop UI sharing ReproLab's existing verifier and evidence tools."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import uuid
import zipfile

from PySide6.QtCore import Qt, QTimer, QUrl, QLockFile, QSize
from PySide6.QtGui import QDesktopServices, QPalette, QColor, QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QSplitter, QLineEdit, QFileDialog,
    QPlainTextEdit, QFormLayout, QDoubleSpinBox, QMessageBox, QDialog, QComboBox,
    QDialogButtonBox, QCheckBox, QProgressBar, QGroupBox, QInputDialog, QHeaderView,
)

from . import __version__
from .desktop_store import DesktopStore, LABELS, input_stamp, read_json, generation_preview, check_preview
from .desktop_tasks import Task
from .desktop_batch import BatchDialog
from .desktop_creation import CaseWizard, DraftWizard
from .recipes import run_saved_case
from .draft_management import verify_draft
from .drafts import recover_draft, generate_draft, _read_bytes
from .report import write_report
from .evidence_bundle import pack_report, unpack_report
from .evidence import inspect_report
from .doctor import diagnose
from .preflight import check_case
from .diagnostics import diagnose_result
from .retry import plan_retry

STYLE = """
QWidget { font-family: ".AppleSystemUIFont", "PingFang SC"; font-size: 13px; color: #303238; }
QMainWindow, QDialog { background: #f8f9fb; }
QWidget#sidebar { background: rgba(246, 247, 249, 220); border-right: 1px solid rgba(225, 228, 235, 170); }
QWidget#workspace { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #fafbfc, stop:0.6 #f7f8fa, stop:1 #f2f4f8); }
QLabel#brand { font-size: 20px; font-weight: 700; letter-spacing: -0.5px; }
QLabel#brandMark { background: #292c32; color: white; border-radius: 10px; font-size: 19px; font-weight: 700; }
QLabel#pageTitle { font-size: 26px; font-weight: 650; letter-spacing: -0.5px; }
QLabel#muted { color: #8b8e96; font-size: 12px; }
QLabel#eyebrow { color: #898d97; font-size: 11px; font-weight: 600; letter-spacing: 1px; }
QLabel#heroTitle { font-size: 30px; font-weight: 650; color: #25282e; }
QLabel#cardTitle { font-size: 17px; font-weight: 600; }
QLabel#badge { background: #eff1f4; color: #898d96; border-radius: 10px; padding: 5px 10px; font-size: 11px; }
QLabel#title { font-size: 25px; font-weight: 650; }
QLabel#verdict { font-size: 20px; font-weight: 600; }
QPushButton { background: #ffffff; border: 1px solid #e0e3e9; border-radius: 7px; padding: 7px 12px; min-height: 18px; }
QPushButton:hover { background: #f5f6f9; border-color: #c5cad4; }
QPushButton:pressed { background: #e9ecf1; }
QPushButton:disabled { color: #adb1b9; background: #f5f6f8; border-color: #eceef2; }
QPushButton#primary { background: #2f3238; border-color: #2f3238; color: #ffffff; font-weight: 600; }
QPushButton#primary:hover { background: #1f2228; }
QPushButton#primary:disabled { background: #c2c6ce; border-color: #c2c6ce; color: #ffffff; }
QPushButton#navigation { text-align: left; padding: 10px 12px; min-height: 22px; border: none; background: transparent; color: #7a7f88; }
QPushButton#navigation:hover { background: #eceef3; color: #434853; }
QPushButton#navigation:checked { background: #ffffff; color: #30343c; font-weight: 600; }
QPushButton#quiet { border: none; background: transparent; color: #707681; padding: 6px 8px; }
QPushButton#quiet:hover { background: #eff1f4; }
QWidget#hero { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(255,255,255,245), stop:1 rgba(255,255,255,200)); border: 1px solid rgba(255,255,255,245); border-radius: 16px; }
QWidget#card { background: rgba(255,255,255,220); border: 1px solid rgba(255,255,255,245); border-radius: 13px; }
QWidget#detailPanel { background: rgba(255,255,255,190); border: 1px solid rgba(231,234,240,190); border-radius: 12px; }
QTabWidget::pane { border: none; background: transparent; }
QTabBar::tab { padding: 10px 18px; margin-right: 12px; color: #8d9199; border-bottom: 2px solid transparent; background: transparent; }
QTabBar::tab:selected { color: #30353e; border-bottom: 2px solid #4c515b; font-weight: 600; }
QTabBar::tab:hover { color: #3c424c; }
QTabWidget#workspacePages::pane { border: none; }
QLineEdit, QPlainTextEdit, QTreeWidget, QListWidget, QComboBox, QDoubleSpinBox {
    border: 1px solid #e3e6ec; border-radius: 7px; background: #ffffff; padding: 7px;
    selection-background-color: #e9edf3; selection-color: #303845;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border-color: #a7b1c2; }
QTreeWidget::item, QListWidget::item { padding: 8px 6px; border-bottom: 1px solid #f0f2f6; }
QTreeWidget::item:selected { background: #eceff5; color: #303845; }
QHeaderView::section { background: #f8f9fb; border: none; border-bottom: 1px solid #e8ebf0; padding: 9px 7px; color: #8a8f98; font-size: 12px; font-weight: 500; }
QComboBox QAbstractItemView { background: white; color: #303238; selection-background-color: #e9edf3; }
QComboBox::drop-down { border: none; width: 25px; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 0; }
QScrollBar::handle:vertical { background: #d2d7e0; border-radius: 3px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QSplitter::handle { background: transparent; }
QSplitter::handle:horizontal { width: 12px; }
QGroupBox { border: 1px solid #e4e7ed; border-radius: 10px; margin-top: 12px; padding-top: 15px; }
QGroupBox::title { subcontrol-origin: margin; left: 13px; padding: 0 5px; color: #8a8f98; }
QProgressBar { border: none; background: #eceff5; border-radius: 2px; max-height: 4px; }
QProgressBar::chunk { background: #737f96; }
QMenu { background: white; border: 1px solid #e5e8ef; border-radius: 8px; padding: 5px; }
QMenu::item { padding: 8px 18px; border-radius: 4px; }
QMenu::item:selected { background: #eef1f7; }
QPushButton#metric { text-align: left; border: 1px solid #e8ebf0; background: white; border-radius: 9px; padding: 10px 14px; font-size: 13px; min-height: 40px; }
QPushButton#metric:hover { background: #f7f9fc; border-color: #c8d0df; }
QPushButton#metric[attention="true"] { color: #a95d5b; background: #fff9f8; border-color: #f0e2e1; }
"""

STYLE += "QComboBox::down-arrow { image: url('" + (
    Path(__file__).parent / "ui_assets/chevron-down.svg").as_posix() + "'); width: 12px; height: 8px; }"


def label(text="", name=None):
    item = QLabel(text)
    item.setWordWrap(True)
    item.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        item.setObjectName(name)
    return item


def text_view():
    item = QPlainTextEdit()
    item.setReadOnly(True)
    return item


def button(text, callback, primary=False):
    item = QPushButton(text)
    if primary:
        item.setObjectName("primary")
    item.clicked.connect(callback)
    return item


def row(*widgets):
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    for item in widgets:
        layout.addWidget(item)
    return widget


def tree(headers):
    widget = QTreeWidget()
    widget.setHeaderLabels(headers)
    widget.setRootIsDecorated(False)
    widget.setAlternatingRowColors(True)
    widget.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    return widget


def open_local(path):
    path = Path(path)
    if not path.exists():
        raise ValueError("文件已移动或不存在，请检查保存位置。")
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.absolute()))):
        raise ValueError("系统未能打开文件，请从保存目录手动打开。")


class MainWindow(QMainWindow):
    def __init__(self, store=None):
        super().__init__()
        # Keep custom light surfaces and native controls readable in macOS dark mode.
        app = QApplication.instance()
        app.setStyle("Fusion")
        palette = QPalette()
        for role, color in {
            QPalette.ColorRole.Window: "#f8f9fb", QPalette.ColorRole.WindowText: "#303238",
            QPalette.ColorRole.Base: "#ffffff", QPalette.ColorRole.AlternateBase: "#fafbfc",
            QPalette.ColorRole.Text: "#303238", QPalette.ColorRole.Button: "#ffffff",
            QPalette.ColorRole.ButtonText: "#303238", QPalette.ColorRole.Highlight: "#e9edf3",
            QPalette.ColorRole.HighlightedText: "#303845", QPalette.ColorRole.ToolTipBase: "#ffffff",
            QPalette.ColorRole.ToolTipText: "#303238",
        }.items():
            palette.setColor(role, QColor(color))
        app.setPalette(palette)
        self.store = store or DesktopStore()
        self.task = None
        self.last_result = None
        self.active_case = None
        self.imported_case = None
        self._close_when_done = False
        self._refresh_after = False
        self._operation_busy = False
        self._wanted_draft = None
        self.setWindowTitle("ReproLab")
        self.resize(1280, 840)
        self.setMinimumSize(960, 680)
        self.setStyleSheet(STYLE)
        central = QWidget()
        self.setCentralWidget(central)
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(184)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(16, 25, 16, 20)
        side.setSpacing(7)
        brand = QHBoxLayout()
        mark = label('R', 'brandMark')
        mark.setFixedSize(33, 33)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.addWidget(mark)
        brand.addWidget(label('ReproLab', 'brand'))
        side.addLayout(brand)
        side.addSpacing(28)
        self.navigation = QVBoxLayout()
        self.navigation.setSpacing(6)
        side.addLayout(self.navigation)
        side.addStretch(1)
        shell.addWidget(sidebar)
        workspace = QWidget()
        workspace.setObjectName('workspace')
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(26, 22, 26, 16)
        layout.setSpacing(17)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(5)
        self.page_title = label('', 'pageTitle')
        heading.addWidget(self.page_title)
        header.addLayout(heading, 1)
        layout.addLayout(header)
        self.tabs = QTabWidget()
        self.tabs.setObjectName('workspacePages')
        self.tabs.tabBar().hide()
        layout.addWidget(self.tabs, 1)
        shell.addWidget(workspace, 1)
        self._cases_page()
        self._drafts_page()
        self._reports_page()
        self._settings_page()
        from .desktop_project import ProjectWorkbench
        self.project_workbench = ProjectWorkbench(self.store, self)
        self.tabs.addTab(self.project_workbench, '项目工作台')
        from .desktop_home import StartPage
        self.home_page = StartPage(
            project=self.open_project_workbench, case=self.new_case, ai=self.new_ai_suite,
            reports=lambda: self.tabs.setCurrentWidget(self.reports_page), demo=self.run_demo,
            project_demo=self.create_project_demo, resume=self.resume_project)
        self.tabs.insertTab(0, self.home_page, '开始使用')
        self.tabs.setCurrentWidget(self.home_page)
        self.project_workbench.busyChanged.connect(self._project_busy_changed)
        self.project_workbench.projectOpened.connect(self._project_opened)
        self.project_workbench.casePrepared.connect(self._open_project_repair_case)
        self._project_opened(self.store.settings.get('last_project', ''),
                             self.store.settings.get('last_project_title', ''))
        self.navigation_buttons = {}
        self.page_metadata = {}
        for title, caption, page, icon in (
            ('概览', '用实际测试，确认一次修改。', self.home_page, 'home'),
            ('项目检查', '选定版本与测试，查看修改前后的结果。', self.project_workbench, 'project'),
            ('修复验收', '确认旧问题能够复现，并验证修复后的表现。', self.cases_page, 'check'),
            ('报告与证据', '查看保存的结果、执行记录和可重放材料。', self.reports_page, 'reports'),
            ('AI 辅助', '准备测试与检查计划，审阅后再执行。', self.drafts_page, 'spark'),
            ('设置', '管理工作目录、运行环境和模型连接。', self.settings_page, 'settings'),
        ):
            if page is self.drafts_page:
                self.navigation.addSpacing(20)
            entry = QPushButton(title)
            entry.setObjectName('navigation')
            entry.setCheckable(True)
            entry.setIcon(QIcon(str(Path(__file__).parent / 'ui_assets' / ('nav-' + icon + '.svg'))))
            entry.setIconSize(QSize(18, 18))
            entry.clicked.connect(lambda checked=False, target=page: self.tabs.setCurrentWidget(target))
            self.navigation.addWidget(entry)
            self.navigation_buttons[page] = entry
            self.page_metadata[page] = (title, caption)
        self.status_label = label(self.store.warning or '就绪', 'muted')
        self.status_label.setMaximumHeight(38)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.cancel_button = button('取消', self.cancel_run)
        self.cancel_button.setEnabled(False)
        self.global_footer = QWidget()
        footer_layout = QVBoxLayout(self.global_footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer = QHBoxLayout()
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.cancel_button)
        footer_layout.addLayout(footer)
        footer_layout.addWidget(self.progress_bar)
        layout.addWidget(self.global_footer)
        self.tabs.currentChanged.connect(self._sync_navigation)
        self._sync_navigation()
        self.refresh()

    def _sync_navigation(self, *_):
        current = self.tabs.currentWidget()
        for page, entry in self.navigation_buttons.items():
            entry.setChecked(page is current)
        title, caption = self.page_metadata[current]
        self.page_title.setText(title)
        self.page_title.setToolTip(caption)
        self.global_footer.setVisible(current not in (self.home_page, self.project_workbench))

    def page(self, title):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.tabs.addTab(page, title)
        return layout

    def _cases_page(self):
        layout = self.page("案例验收")
        self.cases_page = layout.parentWidget()
        self.refresh_button = button("刷新记录", self.refresh)
        self.add_button = button("选择案例文件…", self.choose_case)
        self.ai_suite_button = button("AI 检查计划", self.new_ai_suite)
        self.new_case_button = button("新建修复案例", self.new_case)
        self.run_button = button("开始验收", self.run_selected, True)
        case_actions = QHBoxLayout()
        case_actions.addWidget(self.new_case_button)
        case_actions.addWidget(self.add_button)
        case_actions.addStretch(1)
        case_actions.addWidget(self.ai_suite_button)
        case_actions.addWidget(self.refresh_button)
        layout.addLayout(case_actions)
        self.case_search = QLineEdit()
        self.case_search.setPlaceholderText("搜索案例名称、路径或状态…")
        self.case_filter = QComboBox()
        for title, value in [("全部状态", ""), ("待验收", "pending"), ("已通过", "verified"),
                             ("需要查看", "attention"), ("历史结果", "stale"), ("已取消", "cancelled")]:
            self.case_filter.addItem(title, value)
        self.batch_button = button("批量验收（0）", self.open_batch)
        self.select_visible_button = button("勾选可见项", self.select_visible_cases)
        self.clear_checked_button = button("清空勾选", self.clear_checked_cases)
        layout.addWidget(row(self.case_search, self.case_filter, self.select_visible_button, self.clear_checked_button, self.batch_button))
        split = QSplitter()
        self.case_tree = tree(["案例", "最近一次记录"])
        self.case_tree.currentItemChanged.connect(self.case_selected)
        self.case_tree.itemChanged.connect(self.update_actions)
        self.case_search.textChanged.connect(self.filter_cases)
        self.case_filter.currentIndexChanged.connect(self.filter_cases)
        split.addWidget(self.case_tree)
        right = QWidget()
        side = QVBoxLayout(right)
        side.setContentsMargins(16, 0, 0, 0)
        self.case_title = label("选择一个案例", "verdict")
        self.case_description = label()
        self.case_description.setStyleSheet('color: #858a94; font-size: 13px;')
        side.addWidget(self.case_title)
        side.addWidget(self.case_description)
        self.python_edit = QLineEdit(self.store.settings["python"])
        self.python_edit.setPlaceholderText("内置 Python")
        self.python_choose = button("选择 Python…", self.choose_python)
        self.python_reset = button("使用内置", lambda: self.python_edit.clear())
        side.addWidget(row(self.python_edit, self.python_choose, self.python_reset))
        side.addWidget(self.run_button)
        self.verdict_label = label("待验收", "verdict")
        side.addWidget(self.verdict_label)
        self.before_label = label("旧版：尚未运行")
        self.after_label = label("新版：尚未运行")
        result_box = QGroupBox("实际运行结果")
        result_layout = QVBoxLayout(result_box)
        result_layout.addWidget(self.before_label)
        result_layout.addWidget(self.after_label)
        side.addWidget(result_box)
        self.guidance_label = label()
        side.addWidget(self.guidance_label)
        self.report_button = button("打开完整报告", self.open_current_report)
        self.evidence_button = button("打开证据目录", self.open_current_evidence)
        self.report_button.setEnabled(False)
        self.evidence_button.setEnabled(False)
        side.addWidget(row(self.report_button, self.evidence_button))
        self.details_toggle = QCheckBox("查看来源与运行详情")
        self.case_details = text_view()
        self.case_details.setVisible(False)
        self.details_toggle.toggled.connect(self.case_details.setVisible)
        side.addWidget(self.details_toggle)
        side.addWidget(self.case_details, 1)
        side.addStretch()
        split.addWidget(right)
        split.setSizes([360, 720])
        layout.addWidget(split, 1)
        self.run_button.setToolTip("按此案例的故障声明验收；结果只覆盖所选测试。")

    def _drafts_page(self):
        layout = self.page("测试草稿")
        self.drafts_page = layout.parentWidget()
        self.new_draft_button = button("准备新草稿…", self.new_draft, True)
        self.draft_search = QLineEdit()
        self.draft_search.setPlaceholderText("搜索草稿或状态…")
        self.draft_search.textChanged.connect(lambda: self.filter_tree(self.draft_tree, self.draft_search.text()))
        layout.addWidget(row(self.draft_search, self.new_draft_button))
        layout.addWidget(label("草稿已生成 ≠ 修复验证通过。先审阅问题、上下文、测试与假设，再明确运行。"))
        split = QSplitter()
        self.draft_tree = tree(["草稿", "生成状态 / 最近验收"])
        self.draft_tree.currentItemChanged.connect(self.draft_selected)
        split.addWidget(self.draft_tree)
        self.draft_tabs = QTabWidget()
        self.draft_views = {}
        for name, title in [("issue.txt", "问题"), ("prompt.txt", "发送上下文"), ("regression.py", "测试草稿"), ("explanation.md", "说明与假设")]:
            view = text_view()
            self.draft_views[name] = view
            self.draft_tabs.addTab(view, title)
        split.addWidget(self.draft_tabs)
        split.setSizes([360, 700])
        layout.addWidget(split, 1)
        self.draft_notice = label()
        layout.addWidget(self.draft_notice)
        self.recover_button = button("离线恢复响应", self.recover_selected)
        self.import_response_button = button("导入响应…", self.import_response)
        self.generate_button = button("预览并生成…", self.preview_generate)
        self.verify_draft_button = button("审阅后验收", self.run_draft, True)
        layout.addWidget(row(self.recover_button, self.import_response_button, self.generate_button, self.verify_draft_button))

    def _reports_page(self):
        layout = self.page("报告与证据")
        self.reports_page = layout.parentWidget()
        self.report_search = QLineEdit()
        self.report_search.setPlaceholderText("搜索报告、批次或结论…")
        self.report_search.textChanged.connect(lambda: self.filter_tree(self.report_tree, self.report_search.text()))
        layout.addWidget(self.report_search)
        self.report_tree = tree(["报告", "当时结论", "生成时间"])
        self.report_tree.currentItemChanged.connect(self.report_selected)
        layout.addWidget(self.report_tree, 1)
        self.report_info = label()
        layout.addWidget(self.report_info)
        self.browse_report_button = button("打开报告", self.open_selected_report)
        self.check_button = button("检查证据", self.check_selected_report)
        self.check_button.setToolTip("校验保存材料的一致性，不重新执行测试，也不证明作者身份。")
        self.pack_button = button("导出证据 ZIP…", self.pack_selected)
        self.unpack_button = button("导入证据 ZIP…", self.choose_bundle)
        self.explain_report_button = button("AI 解读结果…", self.explain_selected_report)
        self.retry_failed_button = button("重跑未通过项…", lambda: self.open_retry("not_verified"))
        self.resume_batch_button = button("继续未完成项…", lambda: self.open_retry("unfinished"))
        layout.addWidget(row(self.browse_report_button, self.check_button, self.pack_button,
                             self.explain_report_button, self.retry_failed_button, self.resume_batch_button, self.unpack_button))
        self.import_info = label()
        self.replay_button = button("审阅后重放", self.replay_imported, True)
        self.replay_button.setEnabled(False)
        layout.addWidget(self.import_info)
        layout.addWidget(self.replay_button)

    def _settings_page(self):
        layout = self.page("设置与诊断")
        self.settings_page = layout.parentWidget()
        form = QFormLayout()
        self.path_edits = {}
        from .desktop_models import SavedModelSelector
        model_path = QLineEdit(self.store.settings.get('model_config', ''), self.settings_page)
        model_path.hide()
        self.model_selector = SavedModelSelector(model_path, self.store.directory.parent / 'models', self.settings_page)
        self.model_connection_button = button("选择 / 连接模型…", self.configure_model)
        self.model_import_button = button("导入配置…", lambda: self.choose_setting('model_config'))
        self.model_selector.changed.connect(self._model_selected)
        self.model_selector.setToolTip('AI 可用于准备测试、生成验收计划和解读结果。已有测试检查不需要模型。')
        form.addRow('AI 模型（可选）', row(self.model_selector, self.model_connection_button, self.model_import_button))
        for key, title in [("cases", "案例目录"), ("drafts", "草稿目录"), ("reports", "报告目录"),
                           ("imports", "证据导入目录"), ("model_config", "本机模型配置")]:
            edit = model_path if key == 'model_config' else QLineEdit(self.store.settings[key])
            choose = button("选择…", lambda checked=False, k=key: self.choose_setting(k))
            self.path_edits[key] = edit
            if key == 'model_config':
                edit.setParent(self.settings_page)
                edit.hide()
                choose.deleteLater()
            else:
                form.addRow(title, row(edit, choose))
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(.1, 300)
        self.timeout_spin.setValue(self.store.settings["timeout"])
        self.timeout_spin.setSuffix(" 秒 / 每个版本")
        form.addRow("单版运行时限", self.timeout_spin)
        layout.addLayout(form)
        self.save_button = button("保存设置", self.save_settings)
        self.doctor_button = button("检查环境", self.run_doctor, True)
        layout.addWidget(row(self.save_button, self.doctor_button))
        self.diagnostics = text_view()
        layout.addWidget(self.diagnostics, 1)
        self.about_label = label(f"ReproLab {__version__} · Alpha", "muted")
        self.about_label.setToolTip(f"桌面状态：{self.store.directory}\nmacOS arm64 · ad-hoc 签名，尚未 Apple 公证。")
        layout.addWidget(self.about_label)

    def selected_case(self):
        item = self.case_tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def selected_draft(self):
        item = self.draft_tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def selected_report(self):
        item = self.report_tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _busy(self, active, cancellable=False):
        self.home_page.set_busy(active or self.project_workbench.busy)
        self._operation_busy = active
        for widget in [self.run_button, self.refresh_button, self.add_button, self.case_tree,
                       self.draft_tree, self.report_tree, self.python_edit, self.python_choose,
                       self.python_reset, self.save_button, self.doctor_button, self.timeout_spin,
                       self.recover_button, self.import_response_button, self.generate_button,
                       self.verify_draft_button, self.check_button, self.pack_button, self.unpack_button,
                       self.ai_suite_button, self.model_connection_button, self.model_import_button, self.model_selector, self.new_case_button, self.new_draft_button, self.batch_button,
                       self.explain_report_button, self.retry_failed_button, self.resume_batch_button,
                       self.case_search, self.case_filter, self.select_visible_button, self.clear_checked_button,
                       *self.path_edits.values()]:
            widget.setEnabled(not active)
        self.cancel_button.setEnabled(active and cancellable)
        self.progress_bar.setRange(0, 0 if active else 1)
        self.replay_button.setEnabled(not active and self.imported_case is not None)
        if not active:
            self.update_actions()

    def launch(self, action, completed, message, cancellable=False, *, runtime_values=None):
        if self.task:
            return False
        self.status_label.setText(message)
        self._busy(True, cancellable)
        self.task = Task(action, self, runtime_values=runtime_values)
        self._completion = completed
        self.task.succeeded.connect(self._succeeded)
        self.task.failed.connect(self._failed)
        self.task.cancelled.connect(self._cancelled)
        self.task.progress.connect(self._progress)
        self.task.finished.connect(self._finished)
        self.task.start()
        return True

    def _succeeded(self, result):
        try:
            self._completion(result)
        except Exception as exc:
            self._failed(str(exc))

    def _failed(self, message):
        if self.active_case:
            try:
                self.store.record(self.active_case, "error", error=message)
            except OSError:
                message += "（活动记录也无法保存；报告目录中的现有文件未删除。）"
            self.set_case_status(self.active_case, "error")
            self.verdict_label.setText("无法完成验收")
            self.last_result = None
            self.active_case = None
        self.status_label.setText("未完成：" + message)
        self.diagnostics.setPlainText(message)

    def _cancelled(self):
        if self.active_case:
            try:
                self.store.record(self.active_case, "cancelled")
            except OSError:
                pass  # The on-disk running marker becomes interrupted at next launch.
            self.set_case_status(self.active_case, "cancelled")
        self.active_case = None
        self.last_result = None
        self.verdict_label.setText("已取消 · 没有新的验收结论")
        self.status_label.setText("测试进程已清理；不完整材料不会标记为验证通过。")

    def _finished(self):
        old = self.task
        self.task = None
        old.deleteLater()
        self._busy(False)
        if self._close_when_done:
            self.close()
        elif self._refresh_after:
            self._refresh_after = False
            self.refresh()

    def _progress(self, phase, result):
        messages = {"preflight": "正在预检案例与环境，尚未运行测试…", "preparing": "正在准备独立运行副本…", "before": "正在运行修复前版本…",
                    "before_done": "旧版运行结束，正在准备新版…", "after": "正在运行修复后版本…",
                    "after_done": "新版运行结束，正在保存报告…", "complete": "运行结束，正在整理结果…"}
        self.status_label.setText(messages.get(phase, phase))
        if phase == "complete":
            self.cancel_button.setEnabled(False)
        if phase == "before_done":
            self.before_label.setText(self.result_line("旧版", result))
        if phase == "after_done":
            self.after_label.setText(self.result_line("新版", result))

    @staticmethod
    def result_line(name, result, diagnosis=None):
        diagnosis = diagnosis or result.get("diagnosis")
        if diagnosis:
            lines = str(diagnosis.get('detail') or '').splitlines()
            detail = next((line for line in lines if line.strip()), '')
            detail = detail[:160] + ('…' if len(detail) > 160 or len(lines) > 1 else '')
            return f"{name}：{diagnosis['summary']}" + ('\n' + detail if detail else '')
        kinds = {"passed": "测试通过", "failed": "测试失败", "inconclusive": "无法判断"}
        failure = result.get("failure") or {}
        suffix = f" · {failure.get('type', '')}: {failure.get('message', '')}" if failure else ""
        return f"{name}：{kinds.get(result.get('kind'), '未完成')}{suffix}\n{result.get('reason', '')}"

    def refresh(self):
        self.launch(lambda control: self.store.catalog(), self.apply_catalog, "正在读取本地案例、草稿和报告…")

    def apply_catalog(self, catalog):
        selected = self.store.settings["last_case"]
        checked = set(self.checked_cases())
        old_draft = self.selected_draft()
        wanted_draft = self._wanted_draft or (old_draft["draft_dir"] if old_draft else None)
        self._wanted_draft = None
        self.case_tree.blockSignals(True)
        self.case_tree.clear()
        for entry in catalog["cases"]:
            item = QTreeWidgetItem([entry["title"], LABELS.get(entry["status"], entry["status"])])
            item.setData(0, Qt.ItemDataRole.UserRole, entry)
            self.case_tree.addTopLevelItem(item)
            if entry["readable"]:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked if entry["file"] in checked else Qt.CheckState.Unchecked)
            item.setToolTip(0, entry["title"] + "\n" + entry["file"])
            if entry["file"] == selected:
                self.case_tree.setCurrentItem(item)
        if not self.case_tree.currentItem() and self.case_tree.topLevelItemCount():
            self.case_tree.setCurrentItem(self.case_tree.topLevelItem(0))
        self.case_tree.blockSignals(False)
        self.case_selected(self.case_tree.currentItem())
        self.draft_tree.clear()
        for entry in catalog["drafts"]:
            state = LABELS.get(entry["generation_status"], entry["generation_status"])
            if entry.get("verification"):
                state += " / " + LABELS.get(entry["verification"]["status"], "历史记录")
            item = QTreeWidgetItem([entry.get("title", Path(entry["draft_dir"]).name), state])
            item.setData(0, Qt.ItemDataRole.UserRole, entry)
            self.draft_tree.addTopLevelItem(item)
            if entry["draft_dir"] == wanted_draft:
                self.draft_tree.setCurrentItem(item)
        if self.draft_tree.topLevelItemCount() and not self.draft_tree.currentItem():
            self.draft_tree.setCurrentItem(self.draft_tree.topLevelItem(0))
        self.report_tree.clear()
        for entry in catalog["reports"]:
            item = QTreeWidgetItem([entry["title"], self.report_verdict(entry), entry["at"][:19].replace("T", " ") + " UTC"])
            item.setData(0, Qt.ItemDataRole.UserRole, entry)
            self.report_tree.addTopLevelItem(item)
        if self.report_tree.topLevelItemCount():
            self.report_tree.setCurrentItem(self.report_tree.topLevelItem(0))
        self.filter_cases()
        self.filter_tree(self.draft_tree, self.draft_search.text())
        self.filter_tree(self.report_tree, self.report_search.text())
        self.update_actions()
        self.status_label.setText("；".join(catalog["warnings"]) or f"已读取 {len(catalog['cases'])} 个案例、{len(catalog['drafts'])} 份草稿、{len(catalog['reports'])} 份历史报告。")

    def set_case_status(self, path, status):
        for index in range(self.case_tree.topLevelItemCount()):
            item = self.case_tree.topLevelItem(index)
            entry = item.data(0, Qt.ItemDataRole.UserRole)
            if entry["file"] == str(path):
                entry["status"] = status
                item.setData(0, Qt.ItemDataRole.UserRole, entry)
                item.setText(1, LABELS.get(status, status))

    def _describe_case(self, data):
        full = ('教学示例（人为构造）\n' if data.get('demo') else '') + (
            data.get('description') or '')
        short = '\n'.join(full.splitlines()[:2])[:240]
        self.case_description.setText(short + ('…' if short != full else ''))
        self.case_description.setToolTip(full)

    def case_selected(self, current, previous=None):
        if not current:
            self.case_title.setText("没有匹配的案例")
            self.case_description.clear()
            self.guidance_label.clear()
            self.last_result = None
            self.report_button.setEnabled(False)
            self.evidence_button.setEnabled(False)
            self.update_actions()
            return
        entry = current.data(0, Qt.ItemDataRole.UserRole)
        self.case_title.setText(entry["title"])
        self.store.settings["last_case"] = entry["file"]
        try:
            data = read_json(entry["file"])
            self._describe_case(data)
            self.case_details.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
        except (OSError, ValueError) as exc:
            self.case_description.setText(str(exc))
            self.case_details.clear()
        self.last_result = None
        self.report_button.setEnabled(False)
        self.evidence_button.setEnabled(False)
        self.verdict_label.setText(LABELS.get(entry["status"], "待验收"))
        self.before_label.setText("旧版：尚未运行")
        self.after_label.setText("新版：尚未运行")
        self.guidance_label.clear()
        record = self.store.activity.get(entry["file"], {})
        if record.get("report"):
            try:
                result = read_json(Path(record["report"]) / "report.json")
                self.last_result = result
                self.show_guidance(result)
                self.before_label.setText(self.result_line("旧版（最近记录）", result.get("before", {})))
                self.after_label.setText(self.result_line("新版（最近记录）", result.get("after", {})))
                self.report_button.setEnabled((Path(record["report"]) / "report.html").is_file())
                self.evidence_button.setEnabled((Path(record["report"]) / "evidence").is_dir())
            except (OSError, ValueError):
                pass

        self.update_actions()

    def add_case_path(self, path):
        if self.task:
            return
        path = str(Path(path).resolve())
        if path not in self.store.settings["extra_cases"]:
            self.store.settings["extra_cases"].append(path)
        self.store.settings["last_case"] = path
        self.case_search.clear()
        self.case_filter.setCurrentIndex(0)
        self.store.save()
        self.tabs.setCurrentWidget(self.cases_page)
        self.refresh()

    def update_actions(self, *args):
        if not hasattr(self, "doctor_button"):
            return
        idle = not self._operation_busy
        case = self.selected_case()
        self.run_button.setEnabled(idle and bool(case and case.get("readable")))
        count = len(self.checked_cases())
        self.batch_button.setText(f"批量验收（{count}）")
        self.batch_button.setEnabled(idle and 0 < count <= 50)
        draft = self.selected_draft()
        pending = bool(draft and draft.get("readable") and draft.get("generation_status") in {"prepared", "generation_failed"})
        self.generate_button.setEnabled(idle and pending)
        self.import_response_button.setEnabled(idle and pending)
        self.recover_button.setEnabled(idle and bool(draft and draft.get("recoverable")))
        self.verify_draft_button.setEnabled(idle and bool(draft and draft.get("case")))
        report = self.selected_report()
        self.browse_report_button.setEnabled(bool(report))
        self.explain_report_button.setEnabled(idle and bool(report) and report.get('kind') != 'project_regression'
                                             and report.get("status") not in ("invalid", "running"))
        single = bool(report and report.get("kind") not in ("batch", "suite") and report.get("status") != "invalid")
        self.check_button.setEnabled(idle and single)
        self.pack_button.setEnabled(idle and single)
        batch = bool(report and report.get("kind") in ("batch", "suite"))
        self.retry_failed_button.setVisible(batch)
        self.resume_batch_button.setVisible(batch)
        self.check_button.setVisible(not batch)
        self.pack_button.setVisible(not batch)
        self.retry_failed_button.setEnabled(idle and batch and bool(report.get("retry_count")))
        self.resume_batch_button.setEnabled(idle and batch and bool(report.get("unfinished_count")))

    def checked_cases(self):
        if not hasattr(self, "case_tree"):
            return []
        return [self.case_tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)["file"]
                for i in range(self.case_tree.topLevelItemCount())
                if self.case_tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked]

    def select_visible_cases(self):
        for i in range(self.case_tree.topLevelItemCount()):
            item = self.case_tree.topLevelItem(i)
            if not item.isHidden() and item.data(0, Qt.ItemDataRole.UserRole).get("readable"):
                item.setCheckState(0, Qt.CheckState.Checked)
        self.update_actions()

    def clear_checked_cases(self):
        for i in range(self.case_tree.topLevelItemCount()):
            self.case_tree.topLevelItem(i).setCheckState(0, Qt.CheckState.Unchecked)
        self.update_actions()

    def filter_tree(self, widget, query, state=""):
        query = query.strip().casefold()
        visible = []
        state_groups = {
            "attention": {"attention", "inconclusive", "still_failing", "not_reproduced", "invalid", "error", "interrupted", "regression_detected", "baseline_failure"},
            "verified": {"verified", "regression_passed", "improved", "checks_passed"},
        }
        allowed_states = state_groups.get(state, {state})
        for i in range(widget.topLevelItemCount()):
            item = widget.topLevelItem(i)
            data = item.data(0, Qt.ItemDataRole.UserRole)
            text = " ".join(item.text(c) for c in range(widget.columnCount())) + " " + str(data.get("file", data.get("directory", "")))
            status = data.get("status")
            matches = not state or (isinstance(status, str) and status in allowed_states)
            item.setHidden(query not in text.casefold() or not matches)
            if not item.isHidden():
                visible.append(item)
        if not widget.currentItem() or widget.currentItem().isHidden():
            widget.setCurrentItem(visible[0] if visible else None)
        self.update_actions()

    def filter_cases(self, *args):
        if hasattr(self, "case_tree"):
            self.filter_tree(self.case_tree, self.case_search.text(), self.case_filter.currentData())

    def _model_selected(self, config):
        previous = self.store.settings.get('model_config', '')
        self.store.settings['model_config'] = config
        try:
            self.store.save()
        except OSError:
            self.store.settings['model_config'] = previous
            self.path_edits['model_config'].setText(previous)
            self.status_label.setText('模型选择未保存，已保留先前连接。')
            return
        self.status_label.setText('已切换 AI 模型，后续生成和结果解读使用当前选择。')

    def configure_model(self):
        from .desktop_models import ModelConnectionDialog
        if self.task:
            return
        directory = Path(self.store.settings["cases"]).parent / "models"
        dialog = ModelConnectionDialog(self.path_edits["model_config"].text().strip() or None, directory, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.path_edits["model_config"].setText(dialog.result_data["config"])
            self.save_settings()
            self.status_label.setText("模型连接已保存，可以在AI项目验收或测试草稿中使用。")

    def _project_busy_changed(self, busy):
        self.home_page.set_busy(busy or self.task is not None)

    def open_project_workbench(self):
        self.tabs.setCurrentWidget(self.project_workbench)
        if self.project_workbench.state is None:
            self.project_workbench.repository_edit.setFocus()

    def _open_project_repair_case(self, path):
        try:
            data = read_json(path)
            if self.project_workbench.state:
                self.store.case_runtime_sources[str(Path(path).resolve())] = self.project_workbench.state['repository']
            chosen = data.get('python') or self.store.settings['python']
            self.store.settings['python'] = '' if chosen == sys.executable else chosen
            self.python_edit.setText(self.store.settings['python'])
            self.add_case_path(path)
            self.status_label.setText('正式修复案例已保存并打开；已预填来源报告的环境，请核对后明确点击运行。尚未执行。')
        except (OSError, ValueError) as exc:
            self.status_label.setText('案例已保存，但加入列表失败；可从此文件重开：' + str(path) + '\n' + str(exc))

    def _project_opened(self, directory, title):
        available = bool(directory and (Path(directory) / 'project.json').is_file())
        self.home_page.set_recent_project(title or Path(directory).name, available)

    def resume_project(self):
        if self.task or self.project_workbench.busy:
            return
        self.open_project_workbench()
        path = self.store.settings.get('last_project')
        if path:
            self.project_workbench.open_session(path)

    def create_project_demo(self):
        if self.task or self.project_workbench.busy:
            return
        self.open_project_workbench()
        self.project_workbench.new_demo()

    def run_demo(self):
        if self.task or self.project_workbench.busy:
            return
        self.case_search.clear()
        self.case_filter.setCurrentIndex(0)
        path = Path(__file__).parent / 'demo_case/case.json'
        self.start_verification(str(path.resolve()), python_executable=sys.executable)

    def new_ai_suite(self):
        from .desktop_suite import AISuiteDialog
        if self.task or not self.save_settings():
            return
        dialog = AISuiteDialog(self.store, self)
        dialog.exec()
        self.path_edits["model_config"].setText(self.store.settings["model_config"])
        self.python_edit.setText(self.store.settings["python"])
        self.refresh()

    def new_case(self):
        if self.task or not self.save_settings():
            return
        dialog = CaseWizard(dict(self.store.settings), self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_data:
            chosen = dialog.result_data["runtime"]["executable"]
            self.python_edit.setText("" if chosen == sys.executable else chosen)
            self.save_settings()
            self.add_case_path(dialog.result_data["case"])

    def new_draft(self):
        if self.task or not self.save_settings():
            return
        dialog = DraftWizard(dict(self.store.settings), self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_data:
            self._wanted_draft = dialog.result_data["draft_dir"]
            self.path_edits["drafts"].setText(str(Path(self._wanted_draft).parent))
            chosen = dialog.result_data["python"]
            self.python_edit.setText("" if chosen == sys.executable else chosen)
            self.save_settings()
            self.draft_search.clear()
            self.tabs.setCurrentWidget(self.drafts_page)
            self.refresh()

    def open_batch(self):
        if self.task or not self.save_settings():
            return
        paths = self.checked_cases()
        if not paths:
            return
        dialog = BatchDialog(self.store, paths, python_executable=self.store.settings["python"],
                             timeout=self.store.settings["timeout"], parent=self)
        dialog.exec()
        self.python_edit.setText(self.store.settings["python"])
        self.refresh()

    def open_retry(self, mode):
        if self.task or not self.save_settings():
            return
        entry = self.selected_report()
        if not entry or entry.get("kind") not in ("batch", "suite"):
            return
        try:
            plan = plan_retry(entry["directory"], mode=mode)
        except (OSError, ValueError) as exc:
            self.status_label.setText("无法准备重跑：" + str(exc))
            return
        if not plan["paths"]:
            self.status_label.setText("这个批次没有当前模式需要处理的案例。")
            return
        dialog = BatchDialog(self.store, plan["paths"], python_executable=self.store.settings["python"],
                             timeout=self.store.settings["timeout"], parent=self, retry_plan=plan)
        dialog.exec()
        self.python_edit.setText(self.store.settings["python"])
        self.refresh()

    def choose_case(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择已保存的案例", self.store.settings["cases"], "案例 JSON (*.json)")
        if path:
            self.add_case_path(path)

    def choose_python(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择项目 Python（Windows: Scripts/python.exe；macOS/Linux: bin/python）", str(Path.home()), options=QFileDialog.Option.DontResolveSymlinks)
        if path:
            self.python_edit.setText(os.path.abspath(path))

    def save_settings(self):
        updates = {}
        names = {"cases": "案例目录", "drafts": "草稿目录", "reports": "报告目录", "imports": "证据导入目录"}
        for key, edit in self.path_edits.items():
            value = edit.text().strip()
            if key in names:
                candidate = Path(value).expanduser()
                if not value or not candidate.is_absolute() or (candidate.exists() and not candidate.is_dir()):
                    self.status_label.setText(f"{names[key]}必须是完整的文件夹路径；请点击“选择…”指定目录。")
                    return False
                value = str(candidate)
            updates[key] = value
        updates["python"] = self.python_edit.text().strip()
        updates["timeout"] = self.timeout_spin.value()
        previous = dict(self.store.settings)
        self.store.settings.update(updates)
        try:
            self.store.save()
        except OSError as exc:
            self.store.settings = previous
            self.status_label.setText("设置未保存：" + str(exc))
            return False
        self.status_label.setText("设置已保存；点击“刷新记录”读取新目录。")
        return True

    def choose_setting(self, key):
        if self.task:
            return
        if key == "model_config":
            path, _ = QFileDialog.getOpenFileName(self, "选择本机模型配置（不会显示 Key）", str(Path.home()), "JSON (*.json)")
        else:
            path = QFileDialog.getExistingDirectory(self, "选择保存目录", self.path_edits[key].text())
        if path:
            self.path_edits[key].setText(path)

    def run_selected(self):
        entry = self.selected_case()
        if entry and entry["readable"]:
            self.start_verification(entry["file"])

    def start_verification(self, path, draft=None, *, python_executable=None):
        if self.task:
            return
        if not self.save_settings():
            return
        reference = self.store.activity.get(str(path), {})
        reference_dir = reference.get("report") or reference.get("reference_report")
        if not reference_dir and Path(path).parent.name == "evidence":
            reference_dir = str(Path(path).parent.parent)
        try:
            data = read_json(path)
            runtime_values = self.store.local_runtime_values(self.store.case_runtime_sources.get(str(Path(path).resolve())))
            runtime_values.update(self.store.case_runtime_values.get(str(Path(path).resolve()), {}))
            if data.get('source') in (None, 'snapshot') and data.get('test_target'):
                from .case import load_case
                from .pytest_profile import read_pytest_profile
                from .desktop_runtime_environment import LocalValuesDialog
                try:
                    profile = read_pytest_profile(load_case(path).after)
                    if profile.get('pass_env'):
                        dialog = LocalValuesDialog(profile['pass_env'], runtime_values, self)
                        if dialog.exec() != QDialog.DialogCode.Accepted:
                            return
                        runtime_values = dialog.values
                        self.store.case_runtime_values[str(Path(path).resolve())] = dict(runtime_values)
                except (OSError, ValueError) as exc:
                    self.status_label.setText('运行变量未准备：' + str(exc))
                    return
            matching = None
            for index in range(self.case_tree.topLevelItemCount()):
                item = self.case_tree.topLevelItem(index)
                if item.data(0, Qt.ItemDataRole.UserRole)["file"] == str(path):
                    matching = item
                    break
            if matching is None:
                entry = {"file": str(path), "title": data.get("title", Path(path).stem),
                         "source": data.get("source", "snapshot"), "status": "pending",
                         "readable": True, "error": ""}
                matching = QTreeWidgetItem([entry["title"], LABELS["pending"]])
                matching.setData(0, Qt.ItemDataRole.UserRole, entry)
                self.case_tree.addTopLevelItem(matching)
                if str(path) not in self.store.settings["extra_cases"]:
                    self.store.settings["extra_cases"].append(str(path))
            self.case_tree.setCurrentItem(matching)
            self.store.record(path, "running")
            self.set_case_status(path, "running")
        except (OSError, ValueError) as exc:
            self.status_label.setText("无法开始验收：" + str(exc))
            return
        self.case_title.setText(data.get("title", Path(path).stem))
        self._describe_case(data)
        self.active_case = str(path)
        self.last_result = None
        self.report_button.setEnabled(False)
        self.evidence_button.setEnabled(False)
        self.verdict_label.setText("正在检查")
        self.guidance_label.setText("正在预检，尚未执行测试。")
        self.before_label.setText("旧版：等待运行")
        self.after_label.setText("新版：等待运行")
        chosen = python_executable or self.store.settings["python"] or sys.executable
        output = self.store.settings["reports"]
        timeout = self.store.settings["timeout"]
        def action(control):
            previous = None
            if reference_dir:
                try:
                    previous = read_json(Path(reference_dir) / "report.json").get("environment") or {}
                except (OSError, ValueError):
                    previous = {}
            control.notify("preflight")
            preflight = check_case(path, output, timeout=timeout, python_executable=chosen,
                                   previous_environment=previous, control=control)
            control.check()
            try:
                stamp = input_stamp(path)
            except (OSError, ValueError, KeyError, TypeError):
                stamp = None
            control.check()
            if preflight["status"] != "ready":
                errors = [item["message"] for item in preflight["checks"] if item["status"] == "error"]
                return {"status": "inconclusive", "explanation": "预检未通过，未运行测试。",
                        "tests_executed": False, "preflight": preflight, "error": "；".join(errors)}, stamp
            if draft:
                result = verify_draft(draft, output, timeout=timeout, python_executable=chosen,
                                      control=control, previous_environment=previous)
            else:
                result = run_saved_case(path, output, timeout=timeout, python_executable=chosen,
                                        control=control, previous_environment=previous)
                write_report(result)
            try:
                unchanged = input_stamp(path) == stamp
            except (OSError, ValueError, KeyError, TypeError):
                unchanged = False
            return result, stamp if unchanged else None
        def completed(payload):
            result, stamp = payload
            self.store.record(path, result["status"], stamp=stamp, report=result.get("report_dir"))
            self.active_case = None
            self.last_result = result
            self.show_guidance(result)
            if python_executable is not None and data.get('demo'):
                self.guidance_label.setText('本次示例使用应用 Python；你原先选择的项目环境已保留。\n'
                                            + self.guidance_label.text())
            self.verdict_label.setText(LABELS.get(result["status"], result["status"]) + ("" if stamp else " · 历史结果"))
            self.before_label.setText(self.result_line("旧版", result.get("before", {}), result.get("diagnosis", {}).get("before")))
            self.after_label.setText(self.result_line("新版", result.get("after", {}), result.get("diagnosis", {}).get("after")))
            self.report_button.setEnabled(bool(result.get("report_dir")))
            self.evidence_button.setEnabled(bool(result.get("report_dir")))
            self.status_label.setText(result.get("explanation", result.get("error", ""))
                                      + ("\n" + result["input_integrity_notice"] if result.get("input_integrity_notice") else ""))
            self.case_details.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
            self.set_case_status(path, result["status"] if stamp else "stale")
            if result.get("report_dir"):
                entry = {"directory": result["report_dir"], "title": result["title"],
                         "status": result["status"], "at": result["generated_at"]}
                item = QTreeWidgetItem([entry["title"], LABELS[entry["status"]],
                                       entry["at"][:19].replace("T", " ") + " UTC"])
                item.setData(0, Qt.ItemDataRole.UserRole, entry)
                self.report_tree.insertTopLevelItem(0, item)
                self.report_tree.setCurrentItem(item)
        self.tabs.setCurrentWidget(self.cases_page)
        self.launch(action, completed, "正在准备验收…", cancellable=True, runtime_values=runtime_values)

    def show_guidance(self, result):
        if result.get("tests_executed") is False and result.get("preflight"):
            checks = [c for c in result["preflight"]["checks"] if c["status"] == "error"]
            lines = ["预检未通过，尚未执行测试。"]
            for check in checks[:2]:
                lines.append(check["message"][:180])
                if check.get("suggestion"):
                    lines.append("下一步：" + check["suggestion"][:180])
        else:
            diagnosis = result.get("diagnosis") or diagnose_result(result)
            steps = diagnosis.get("next_steps", [])
            if result.get("status") == "verified":
                steps = steps[-1:]
            lines = ["下一步：" + step[:180] for step in steps[:2]]
            comparison = result.get("environment_comparison") or {}
            if comparison.get("status") == "changed" or (comparison.get("status") == "unknown" and result.get("environment_reference_present")):
                lines.insert(0, "环境提醒：" + comparison["notice"])
        self.guidance_label.setText("\n".join(lines))

    def cancel_run(self):
        if self.task:
            accepted = self.task.control.cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("正在取消并清理测试进程…" if accepted else
                                     "两版测试已完成，正在保存结果；此时已不能取消。")

    def safe_open(self, path):
        try:
            open_local(path)
            self.status_label.setText("已交给系统打开：" + str(path))
        except (OSError, ValueError) as exc:
            self.status_label.setText(str(exc))

    def open_current_report(self):
        if self.last_result and self.last_result.get("report_dir"):
            self.safe_open(Path(self.last_result["report_dir"]) / "report.html")

    def open_current_evidence(self):
        if self.last_result and self.last_result.get("report_dir"):
            self.safe_open(Path(self.last_result["report_dir"]) / "evidence")

    def draft_selected(self, current, previous=None):
        if not current:
            if hasattr(self, "draft_views"):
                for view in self.draft_views.values():
                    view.clear()
            self.update_actions()
            return
        entry = current.data(0, Qt.ItemDataRole.UserRole)
        folder = Path(entry["draft_dir"])
        for name, view in self.draft_views.items():
            try:
                view.setPlainText(_read_bytes(folder / name).decode("utf-8"))
            except (OSError, ValueError, UnicodeError):
                view.setPlainText("暂无此项内容。")
        self.draft_notice.setText(entry.get("error") or entry.get("next_action", "") + "\n" + entry.get("notice", ""))
        self.update_actions()

    def draft_operation(self, action, message):
        def completed(result):
            self.status_label.setText(result.get("notice", "操作已完成；尚未验收。"))
            self._refresh_after = True
        self.launch(action, completed, message)

    def recover_selected(self):
        entry = self.selected_draft()
        if entry:
            self.draft_operation(lambda control: recover_draft(entry["draft_dir"]), "正在离线恢复已保存响应…")

    def import_response(self):
        entry = self.selected_draft()
        if not entry:
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择模型返回的 JSON（不会联网）", str(Path.home()), "JSON (*.json)")
        if not path:
            return
        source, ok = QInputDialog.getText(self, "声明响应来源", "模型或来源名称（仅记录你的声明）：")
        if ok and source.strip():
            self.draft_operation(lambda control: generate_draft(entry["draft_dir"], response_file=path, model=source.strip()), "正在检查并导入响应…")

    def preview_generate(self):
        entry = self.selected_draft()
        if not entry or self.task:
            return
        config = self.path_edits["model_config"].text().strip()
        try:
            preview = generation_preview(entry["draft_dir"], config)
        except (OSError, ValueError, UnicodeError) as exc:
            self.status_label.setText(str(exc))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("发送前预览 · 此操作将调用模型")
        dialog.resize(840, 650)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label(f"发送到：{preview['endpoint']}\n协议：{preview['protocol']}    模型：{preview['model']}\n思考强度：{preview['reasoning_description']}\n下方是将发送的全部问题和代码上下文；可能产生 API 费用。"))
        view = text_view()
        view.setPlainText(preview["prompt"])
        layout.addWidget(view, 1)
        consent = QCheckBox("我已审阅以上内容，并同意发送到此服务")
        layout.addWidget(consent)
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        send = controls.addButton("发送并生成草稿", QDialogButtonBox.ButtonRole.AcceptRole)
        send.setEnabled(False)
        consent.toggled.connect(send.setEnabled)
        controls.accepted.connect(dialog.accept)
        controls.rejected.connect(dialog.reject)
        layout.addWidget(controls)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            def action(control):
                check_preview(entry["draft_dir"], config, preview)
                return generate_draft(entry["draft_dir"], config_file=config)
            self.draft_operation(action, "正在请求模型（最长约 180 秒）…生成后仍需审阅和验收。")

    def run_draft(self):
        entry = self.selected_draft()
        if entry and entry.get("case"):
            self.start_verification(entry["case"], draft=entry["draft_dir"])
        else:
            self.status_label.setText("请先生成、导入或恢复草稿，再审阅后验收。")

    @staticmethod
    def report_verdict(entry):
        if entry.get('kind') == 'project_regression':
            return {'regression_passed': '项目所选回归通过', 'regression_detected': '项目发现新增失败',
                    'attention': '项目回归需处理', 'cancelled': '项目回归已取消'}.get(entry['status'], '项目记录需查看')
        if entry.get("kind") in ("batch", "suite"):
            return {"checks_passed": "所选回归检查通过", "verified": "批次全部通过", "attention": "批次有需查看的结果", "cancelled": "批次已取消",
                    "interrupted": "批次未完成", "running": "批次未完成"}.get(entry["status"], "未知状态")
        return LABELS.get(entry["status"], entry["status"])

    def report_selected(self, current, previous=None):
        if current:
            entry = current.data(0, Qt.ItemDataRole.UserRole)
            self.report_info.setText(("批次总览：" if entry.get("kind") in ("batch", "suite") else "历史报告：") + entry["directory"])
        self.update_actions()

    def explain_selected_report(self):
        from .desktop_suite import ExplanationDialog
        entry = self.selected_report()
        if entry and not self.task:
            ExplanationDialog(entry['directory'], self.store.settings['model_config'], self).exec()

    def open_selected_report(self):
        entry = self.selected_report()
        if entry:
            self.safe_open(Path(entry["directory"]) / ("summary.html" if entry.get("kind") in ("batch", "suite") else "report.html"))

    def check_selected_report(self):
        entry = self.selected_report()
        if entry and entry.get("kind") not in ("batch", "suite"):
            def completed(payload):
                checked, _ = payload
                self.report_info.setText("\n".join(filter(None, [
                    LABELS.get(checked["status"], checked["status"]),
                    "；".join(checked.get("issues", [])), checked.get("notice", "")])))
            if entry.get('kind') == 'project_regression':
                from .project_sessions import check_project_report
                action = lambda control: (check_project_report(entry['directory']), None)
            else:
                action = lambda control: inspect_report(entry['directory'])
            self.launch(action, completed, "正在检查证据…")

    def pack_selected(self):
        entry = self.selected_report()
        if not entry or entry.get("kind") in ("batch", "suite"):
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出证据包（不覆盖已有文件）", str(Path(self.store.settings["reports"]).parent / "evidence.zip"), "ZIP (*.zip)")
        if path:
            if entry.get('kind') == 'project_regression':
                from .project_sessions import pack_project_report
                pack = pack_project_report
            else:
                pack = pack_report
            self.launch(lambda control: pack(entry["directory"], path),
                        lambda result: self.status_label.setText("证据包已保存：" + path), "正在核对并打包证据…")

    def choose_bundle(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择证据包；导入不会执行代码", str(Path.home()), "ZIP (*.zip)")
        if path:
            self.import_bundle_path(path)

    def import_bundle_path(self, path):
        if self.task:
            return
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                if ('reprolab-bundle.json' not in names
                        and any(name.endswith('/report.json') and name.count('/') == 1 for name in names)):
                    self.tabs.setCurrentWidget(self.project_workbench)
                    self.project_workbench.import_bundle(path)
                    return
        except (OSError, zipfile.BadZipFile) as exc:
            self.status_label.setText('无法读取证据 ZIP：' + str(exc))
            return
        destination = Path(self.store.settings["imports"]) / ("import-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
        def completed(result):
            self.imported_case = result["case"]
            self.import_info.setText("证据已校验并导入，未执行代码。\n原报告结论：" + LABELS.get(result["recorded_verdict"], result["recorded_verdict"]) + "\n保存位置：" + result["directory"] + "\n审阅可信后，可明确点击下方按钮重新验收。")
            self.status_label.setText("导入完成；没有自动重放。")
        self.launch(lambda control: unpack_report(path, destination), completed, "正在校验证据包并导入…")

    def replay_imported(self):
        if self.imported_case:
            self.start_verification(self.imported_case)

    def run_doctor(self):
        if not self.save_settings():
            return
        chosen = self.store.settings["python"] or sys.executable
        config = self.store.settings["model_config"] or None
        def completed(result):
            lines = [LABELS.get(result["status"], result["status"]), result["notice"], ""]
            lines.extend(item["message"] for item in result["checks"])
            if result.get("runtime"):
                lines.append("\n环境：" + result["runtime"]["executable"])
            if result.get("model"):
                lines.append("\n模型：" + result["model"]["model"] + "\n服务：" + result["model"]["endpoint"])
            self.diagnostics.setPlainText("\n".join(lines))
            self.status_label.setText("诊断结束；未执行项目测试、未连接模型。")
        self.launch(lambda control: diagnose(python_executable=chosen, config_file=config), completed, "正在检查所选环境…")

    def closeEvent(self, event):
        processing = self.project_workbench.processing_dialog
        if processing is not None and processing.isVisible():
            if not self.project_workbench.request_close():
                self._close_when_done = bool(self.project_workbench._closing)
                if self._close_when_done and self.project_workbench.busy:
                    QTimer.singleShot(250, self.close)
                event.ignore()
                return
        if self.project_workbench.busy:
            self._close_when_done = True
            self.project_workbench.request_close()
            if self.task and self.cancel_button.isEnabled():
                self.cancel_run()
            self.status_label.setText('正在结束项目操作并保存已有结果…')
            QTimer.singleShot(250, self.close)
            event.ignore()
            return
        if self.task:
            if self.cancel_button.isEnabled():
                self._close_when_done = True
                self.cancel_run()
            else:
                self.status_label.setText("操作尚在进行，请等待完成后关闭。")
            event.ignore()
            return
        self.save_settings()
        event.accept()


def main(argv=None):
    parser = argparse.ArgumentParser(description="ReproLab 桌面端")
    parser.add_argument("--state-dir", type=Path, help="独立桌面设置目录")
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("ReproLab")
    app.setOrganizationName("ReproLab")
    try:
        store = DesktopStore(args.state_dir)
    except (OSError, ValueError) as exc:
        QMessageBox.critical(None, "无法读取桌面设置", str(exc))
        return 2
    lock = QLockFile(str(store.directory / "session.lock"))
    if not lock.tryLock(0):
        QMessageBox.information(None, "ReproLab 已打开", "此桌面设置目录已有窗口正在使用，请切回原窗口。")
        return 0
    window = MainWindow(store)
    window.show()
    code = app.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
