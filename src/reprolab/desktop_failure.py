"""Local report-bound repair dialogs; preparation and execution stay separate."""
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFontDatabase, QTextCursor
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .desktop_tasks import Task
from .draft_context import MAX_CONTEXT_BYTES
from .repair_drafts import identity, draft_path, read_draft, save_draft, discard_draft
from .repair_suggestions import message_suggestions
from .project_failure import (export_repair_brief, prepare_report_repair, read_repair_preparation,
                              render_repair_brief, save_report_repair, validate_repair_confirmation)


def _label(text=''):
    item = QLabel(text)
    item.setWordWrap(True)
    item.setTextFormat(Qt.TextFormat.PlainText)
    item.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return item


def _editor(text='', readonly=False):
    item = QPlainTextEdit()
    item.setPlainText(text)
    item.setReadOnly(readonly)
    item.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    return item


def _button(text, callback):
    item = QPushButton(text)
    item.clicked.connect(lambda: callback())
    return item


class _StatusView(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.setReadOnly(True)

    def setText(self, text):
        self.setPlainText(text)


class _LocalDialog(QDialog):
    busyChanged = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(940, 760)
        self.task = None
        self._closing = False
        self._on_finished = None
        outer = self.outer_layout = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)
        self.content = QWidget()
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.content)
        outer.addWidget(scroll, 1)
        self.status = _StatusView()
        self.status.setFixedHeight(48)
        self.status.setStyleSheet('border: none; background: transparent; color: #8a8f98; font-size: 12px;')
        outer.addWidget(self.status)
        self.close_button = _button('关闭', self.reject)
        self.close_button.setObjectName('quiet')
        self.footer_actions = QHBoxLayout()
        self.footer_actions.addStretch(1)
        self.footer_actions.addWidget(self.close_button)
        outer.addLayout(self.footer_actions)

    def _run(self, action, callback, message):
        if self.task is not None:
            return
        self.status.setText(message)
        self.content.setEnabled(False)
        self.close_button.setText('取消准备／保存')
        task = self.task = Task(action, self)
        task.succeeded.connect(lambda result: self._success(callback, result), Qt.ConnectionType.QueuedConnection)
        task.failed.connect(lambda error: self.status.setText('未完成：' + error + '\n原准备材料保留，可修正后重试。'),
                            Qt.ConnectionType.QueuedConnection)
        task.cancelled.connect(lambda: self.status.setText('已取消本次操作；此前已保存的材料保留。可重新准备或重试保存。'),
                               Qt.ConnectionType.QueuedConnection)
        task.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        self.busyChanged.emit(True)
        task.start()

    def _success(self, callback, result):
        try:
            callback(result)
        except Exception as exc:
            self.status.setText('未完成：' + str(exc))

    def _finished(self):
        task, self.task = self.task, None
        if task:
            task.deleteLater()
        self.content.setEnabled(True)
        self.close_button.setText('关闭')
        self.busyChanged.emit(False)
        if self._closing:
            self._closing = False
            self.reject()
        elif self._on_finished:
            callback, self._on_finished = self._on_finished, None
            QTimer.singleShot(0, self, callback)

    def reject(self):
        if self.task is not None:
            self.task.control.cancel()
            self.close_button.setEnabled(False)
            self.status.setText('正在取消；保存已经开始提交时会先完成保存，再关闭。')
            self._closing = True
            return False
        self.close_button.setEnabled(True)
        super().reject()
        return True

    def closeEvent(self, event):
        if self.reject():
            event.accept()
        else:
            event.ignore()


class RepairDraftPicker(_LocalDialog):
    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.setWindowTitle('恢复本机修复说明')
        self.selected_path = None
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['项目 / 测试', '保存时间（UTC）', '来源运行'])
        self.tree.setRootIsDecorated(False)
        self.body.addWidget(self.tree)
        self.restore_button = _button('恢复所选草稿', self.restore)
        self.restore_button.setEnabled(False)
        self.footer_actions.insertWidget(0, self.restore_button)
        self.tree.itemSelectionChanged.connect(lambda: self.restore_button.setEnabled(
            bool(self.tree.currentItem() and self.tree.currentItem().data(0, Qt.ItemDataRole.UserRole))))
        for path in sorted(Path(root).glob('*.json')):
            try:
                saved = read_draft(path)
                binding = saved['identity']
                item = QTreeWidgetItem([Path(binding['repository']).name + ' / ' + binding['test_id'],
                                       saved['saved_at'][:19], binding['run_id']])
                item.setData(0, Qt.ItemDataRole.UserRole, str(path))
                item.setToolTip(0, binding['repository'] + '\n' + binding['test_id'])
            except (OSError, ValueError) as exc:
                item = QTreeWidgetItem(['无法读取：' + path.name, '', ''])
                item.setToolTip(0, str(exc))
            self.tree.addTopLevelItem(item)
        self.tree.header().setStretchLastSection(False)
        self.tree.setColumnWidth(0, 480)
        self.tree.setColumnWidth(1, 180)
        self.status.setText('草稿仅保存在本机；恢复时重新核对来源。' if self.tree.topLevelItemCount() else '没有已保存的修复说明草稿。')

    def restore(self):
        item = self.tree.currentItem()
        if item and item.data(0, Qt.ItemDataRole.UserRole):
            self.selected_path = item.data(0, Qt.ItemDataRole.UserRole)
            self.accept()


class RepairBriefDialog(_LocalDialog):
    def __init__(self, material, output_root, parent=None, *, draft_root=None, draft=None, source_available=True):
        super().__init__(parent)
        self.material = material
        self.output_root = Path(output_root)
        self.draft_root = Path(draft_root) if draft_root is not None else None
        self.source_available = source_available
        self._draft_record = None
        self._restoring = False
        self._draft_discarded = False
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.timeout.connect(self.save_draft)
        self._generated_text = ''
        self._shared_text = ''
        self.setWindowTitle('准备给 AI 的修复说明 · 本地预览')
        self.body.addWidget(_label('填写任务后自动生成本地预览；可直接编辑，复制与导出使用同一份全文。'))
        split = QSplitter(Qt.Orientation.Horizontal)
        inputs = QWidget()
        form = QVBoxLayout(inputs)
        form.setContentsMargins(0, 0, 0, 0)
        fields = QFormLayout()
        self.problem = _editor(material.get('issue') or '')
        self.problem.setPlaceholderText('描述选中失败所暴露的问题')
        self.expected = _editor()
        self.expected.setPlaceholderText('确认用户应看到的稳定行为；保持原断言要求')
        self.allowed = _editor('相关业务源码；保持现有测试断言。需要修改测试预期时先说明依据，由我决定。')
        for widget in (self.problem, self.expected, self.allowed):
            widget.setMaximumHeight(68)
        fields.addRow('问题描述', self.problem)
        fields.addRow('确认的期望行为', self.expected)
        fields.addRow('允许修改的目标', self.allowed)
        form.addLayout(fields)
        self.blocks = QTreeWidget()
        self.blocks.setHeaderLabels(['选择加入的历史材料（最多 12 段）'])
        self.blocks.setRootIsDecorated(False)
        for key, block in material['blocks'].items():
            item = QTreeWidgetItem([block['title']])
            item.setData(0, Qt.ItemDataRole.UserRole, key)
            item.setToolTip(0, block['title'])
            item.setCheckState(0, Qt.CheckState.Checked if key == 'test' else Qt.CheckState.Unchecked)
            self.blocks.addTopLevelItem(item)
        form.addWidget(self.blocks, 1)
        self.current = _editor()
        self.current.setMaximumHeight(68)
        self.current.setPlaceholderText('可选：粘贴当前工作区材料，将单列为“未经来源运行验证的当前材料”')
        form.addWidget(self.current)
        self.generate_button = _button('重新生成预览', self.generate)
        self.generate_button.setToolTip('手工编辑过的预览会先询问是否替换；重新生成后可撤销恢复。')
        form.addWidget(self.generate_button)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inputs)
        preview_panel = QWidget()
        preview_body = QVBoxLayout(preview_panel)
        preview_body.setContentsMargins(0, 0, 0, 0)
        preview_body.addWidget(_label('最终将复制／导出的全文（可编辑、滚动、删减）'))
        self.preview = _editor()
        self.preview.setPlaceholderText('填写问题与期望行为后，完整预览会自动更新。')
        preview_body.addWidget(self.preview, 1)
        self.counter = _label()
        preview_body.addWidget(self.counter)
        self.omit_button = _button('将选中内容标为省略', self.omit_selection)
        self.omit_button.setEnabled(False)
        self.preview.copyAvailable.connect(self.omit_button.setEnabled)
        preview_actions = QHBoxLayout()
        preview_actions.addWidget(self.omit_button)
        self.undo_button = _button('撤销上次编辑', self.preview.undo)
        self.undo_button.setEnabled(False)
        self.preview.undoAvailable.connect(self.undo_button.setEnabled)
        preview_actions.addWidget(self.undo_button)
        preview_body.addLayout(preview_actions)
        self.copy_button = _button('复制完整预览', self.copy)
        self.copy_button.setObjectName('primary')
        self.export_button = _button('导出预览为本地文本…', self.export)
        actions = QHBoxLayout()
        actions.addWidget(self.copy_button)
        actions.addWidget(self.export_button)
        preview_body.addLayout(actions)
        split.addWidget(scroll)
        split.addWidget(preview_panel)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        self.body.addWidget(split, 1)
        self._initial_inputs = self._input_state()
        self._preview_inputs = self._initial_inputs
        self._shared_inputs = None
        self.preview.textChanged.connect(self._count)
        for widget in (self.problem, self.expected, self.allowed, self.current):
            widget.textChanged.connect(self._inputs_changed)
        self.blocks.itemChanged.connect(self._inputs_changed)
        self.draft_status = _label()
        self.draft_status.setToolTip('仅在本机保存表单与预览文本，不读取运行变量值或模型凭据。手工粘贴的内容也会保存，请勿填入临时秘密。')
        self.outer_layout.insertWidget(1, self.draft_status)
        self.retry_draft_button = _button('重试保存草稿', self.save_draft)
        self.discard_draft_button = _button('放弃草稿…', self.discard_draft)
        self.footer_actions.insertWidget(0, self.discard_draft_button)
        self.footer_actions.insertWidget(1, self.retry_draft_button)
        self.retry_draft_button.hide()
        self.discard_draft_button.setVisible(self.draft_root is not None)
        if self.draft_root is not None:
            self.draft_status.setText('编辑后自动保存到本机')
            self.draft_status.setToolTip(self.draft_status.toolTip() + '\n' + str(self.draft_root))
            for widget in (self.problem, self.expected, self.allowed, self.current, self.preview):
                widget.textChanged.connect(self._schedule_draft)
            self.blocks.itemChanged.connect(self._schedule_draft)
            try:
                saved = draft or read_draft(draft_path(self.draft_root, identity(material)))
                if saved:
                    self._restore_draft(saved)
            except (OSError, ValueError) as exc:
                self.draft_status.setText('草稿未恢复，原文件保留：' + str(exc))
        if not source_available:
            for widget in (self.problem, self.expected, self.allowed, self.current, self.preview):
                widget.setReadOnly(True)
            self.blocks.setEnabled(False)
            self.generate_button.setEnabled(False)
            self.draft_status.setText('仅查看草稿：来源不可用或未通过校验。请重新定位原报告后，从同一失败项恢复。')
        self._count()

    def _draft_state(self):
        def inputs(value):
            return [*value[:4], list(value[4])]
        return {'inputs': inputs(self._input_state()), 'preview_inputs': inputs(self._preview_inputs),
                'preview': self.preview.toPlainText(), 'generated': self._generated_text}

    def _restore_draft(self, saved):
        if saved['identity'] != identity(self.material):
            raise ValueError('草稿与当前项目、报告或测试不匹配。')
        self._restoring = True
        try:
            state = saved['state']; values = state['inputs']
            for widget, text in zip((self.problem, self.expected, self.allowed, self.current), values[:4]):
                widget.blockSignals(True); widget.setPlainText(text); widget.blockSignals(False)
            self.blocks.blockSignals(True)
            for index in range(self.blocks.topLevelItemCount()):
                item = self.blocks.topLevelItem(index)
                item.setCheckState(0, Qt.CheckState.Checked if item.data(0, Qt.ItemDataRole.UserRole) in values[4] else Qt.CheckState.Unchecked)
            self.blocks.blockSignals(False)
            self.preview.blockSignals(True); self.preview.setPlainText(state['preview']); self.preview.blockSignals(False)
            self._generated_text = state['generated']
            self._preview_inputs = (*state['preview_inputs'][:4], tuple(state['preview_inputs'][4]))
            self._draft_record = saved
            self.draft_status.setText('已恢复本机草稿 · ' + Path(saved['identity']['repository']).name + ' · ' + self.material['test_id'])
            self.draft_status.setToolTip(self.draft_status.toolTip() + '\n来源运行：' + saved['identity']['run_id'] + '\n保存于：' + saved['saved_at'])
        finally:
            self._restoring = False

    def _schedule_draft(self, *_):
        if self._restoring or self._draft_discarded or not self.source_available:
            return
        self.draft_status.setText('草稿待保存到本机…')
        self._draft_timer.start(350)

    def save_draft(self):
        self._draft_timer.stop()
        if self.draft_root is None or self._draft_discarded or not self.source_available:
            return False
        state = self._draft_state()
        if self._draft_record is None and self._input_state() == self._initial_inputs and not state['preview']:
            return True
        try:
            if self._draft_record is None or state != self._draft_record['state']:
                self._draft_record = save_draft(self.draft_root, self.material, state,
                    expected=(self._draft_record or {}).get('_sha256'))
            self.draft_status.setText('草稿已保存到本机 · ' + self._draft_record['saved_at'][11:19] + ' UTC')
            self.retry_draft_button.hide()
            return True
        except (OSError, ValueError) as exc:
            self.draft_status.setText('草稿未保存：' + str(exc))
            self.retry_draft_button.show()
            return False

    def discard_draft(self):
        if not self._confirm_discard('放弃这份修复说明草稿？', '删除本机草稿并关闭；原报告和执行证据保留。', '放弃草稿'):
            return
        self._draft_timer.stop()
        try:
            discard_draft(draft_path(self.draft_root, identity(self.material)),
                          expected=(self._draft_record or {}).get('_sha256'))
        except (OSError, ValueError) as exc:
            self.draft_status.setText('未放弃草稿：' + str(exc)); return
        self._draft_discarded = True
        super().reject()

    def _input_state(self):
        return (self.problem.toPlainText(), self.expected.toPlainText(), self.allowed.toPlainText(),
                self.current.toPlainText(), tuple(self.blocks.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
                    for i in range(self.blocks.topLevelItemCount())
                    if self.blocks.topLevelItem(i).checkState(0) == Qt.CheckState.Checked))

    def _inputs_changed(self, *_):
        if self._restoring or not self.source_available:
            return
        if self._input_state() == self._preview_inputs:
            self._count()
            self.status.setText('输入已恢复为当前预览所依据的内容；手工编辑保留。')
            return
        self.generate(automatic=True)

    def generate(self, *, automatic=False):
        if not self.source_available:
            return
        inputs = self._input_state()
        problem, expected, allowed, current_material, keys = inputs
        try:
            text = render_repair_brief(self.material, problem=problem,
                expected_behavior=expected, allowed_changes=allowed,
                selected_blocks=keys, current_material=current_material)
            current = self.preview.toPlainText()
            if automatic and current != self._generated_text and current != text:
                self._count()
                self.status.setText('输入或材料选择已改变；手工预览已保留。请重新生成并核对后再复制或导出。')
                return
            if current and current != self._generated_text and current != text and not self._confirm_discard(
                    '替换手工编辑的预览？', '重新生成会替换现有删改。可以继续编辑，或生成后用撤销恢复。', '重新生成'):
                self._count()
                self.status.setText('已保留手工编辑的完整预览；与当前输入不一致时暂停复制和导出。')
                return
            cursor = self.preview.textCursor()
            cursor.beginEditBlock()
            cursor.select(QTextCursor.SelectionType.Document)
            cursor.insertText(text)
            cursor.endEditBlock()
            self._generated_text = text
            self._preview_inputs = inputs
            self._count()
            self.status.setText('预览已自动更新，可审阅后复制或导出。' if automatic else
                                '预览已更新；可直接编辑，撤销可恢复上次文字。')
        except ValueError as exc:
            self._count()
            self.status.setText(str(exc))

    def _confirm_discard(self, title, message, action):
        dialog = QMessageBox(self)
        dialog.setWindowTitle(title)
        dialog.setText(message)
        keep = dialog.addButton('继续编辑', QMessageBox.ButtonRole.RejectRole)
        discard = dialog.addButton(action, QMessageBox.ButtonRole.AcceptRole)
        dialog.setDefaultButton(keep)
        dialog.setEscapeButton(keep)
        dialog.exec()
        return dialog.clickedButton() is discard

    def reject(self):
        if self.task is not None:
            return super().reject()
        if self.draft_root is not None:
            if not self.source_available or self._draft_discarded or self.save_draft():
                return super().reject()
        text = self.preview.toPlainText()
        inputs = self._input_state()
        unshared_inputs = inputs != (self._initial_inputs if self._shared_inputs is None else self._shared_inputs)
        if (unshared_inputs or text.strip() and text != self._shared_text) and not self._confirm_discard(
                '关闭尚未交付的修复说明？', '当前输入或预览尚未复制或导出。可继续编辑并保存需要的内容。', '关闭窗口'):
            return False
        return super().reject()

    def _count(self):
        text = self.preview.toPlainText()
        size = len(text.encode('utf-8'))
        stale = self._input_state() != self._preview_inputs
        valid = bool(text.strip()) and size <= MAX_CONTEXT_BYTES and not stale and self.source_available
        if self.draft_root is not None and valid:
            try:
                values = self._input_state()
                render_repair_brief(self.material, problem=values[0], expected_behavior=values[1],
                    allowed_changes=values[2], current_material=values[3], selected_blocks=values[4])
            except ValueError:
                valid = False
        self.counter.setText(f'全文 {size:,} / {MAX_CONTEXT_BYTES:,} 字节；编辑后不会隐藏截断。' +
                             ('请删减至 96 KiB 后再复制／导出。' if size > MAX_CONTEXT_BYTES else ''))
        if stale:
            self.counter.setText('输入或材料选择已改变，当前预览尚未更新。重新生成后可复制／导出。')
        self.generate_button.setText('更新预览并核对' if stale else '重新生成预览')
        self.copy_button.setEnabled(valid)
        self.export_button.setEnabled(valid)

    def omit_selection(self):
        cursor = self.preview.textCursor()
        if cursor.hasSelection():
            cursor.insertText('[用户省略此处内容]')
            self.preview.setTextCursor(cursor)

    def copy(self):
        if not self.copy_button.isEnabled():
            return
        text = self.preview.toPlainText()
        QApplication.clipboard().setText(text)
        self._shared_text = text
        self._shared_inputs = self._input_state()
        self.status.setText('已复制右侧完整预览；没有发送到外部服务。')

    def export(self):
        if not self.export_button.isEnabled():
            return
        path, _ = QFileDialog.getSaveFileName(self, '保存与预览相同的修复说明',
            str(self.output_root / (self.material['reference']['run_id'] + '-repair.txt')), '文本 (*.txt)')
        if not path:
            self.status.setText('已取消导出，编辑后的预览保留。')
            return
        if not self.export_button.isEnabled():
            self.status.setText('输入或预览已改变，请核对后重新导出。')
            return
        text = self.preview.toPlainText()
        inputs = self._input_state()
        protected = self.material.get('path_roots', {})
        def action(control):
            control.commit()
            return export_repair_brief(path, text, protected=(protected.get('report_dir'), protected.get('repository')))
        def saved(result):
            self._shared_text = text
            self._shared_inputs = inputs
            self.status.setText(result['notice'] + '\n' + result['file'])
        self._run(action, saved, '正在保存完整预览到本地文本…')


class ReportRepairDialog(_LocalDialog):
    caseReady = Signal(str)

    def __init__(self, report, reference, test_id, settings, output_root, parent=None, *, is_current=None, brief_draft=None):
        super().__init__(parent)
        self.setWindowTitle('准备修复验收')
        self.report, self.reference, self.test_id = report, reference, test_id
        self.settings, self.output_root = settings, Path(output_root)
        self.is_current = is_current or (lambda: True)
        self.brief_draft = brief_draft
        self.plan = None
        self.saved_case = None
        self.preparation_panel = QWidget()
        preparation = QVBoxLayout(self.preparation_panel)
        preparation.setContentsMargins(0, 0, 0, 0)
        preparation.setSpacing(12)
        preparation.addWidget(_label('选择修复后的工作区。工具将保存原故障源码和当前候选，之后由你确认验收条件。'))
        self.repository = QLineEdit((report or {}).get('repository', ''))
        source_row = QHBoxLayout()
        source_row.addWidget(self.repository, 1)
        source_row.addWidget(_button('选择工作区', self.choose_repository))
        preparation.addLayout(source_row)
        actions = QHBoxLayout()
        self.prepare_button = _button('冻结当前修改', self.prepare)
        self.prepare_button.setObjectName('primary')
        self.prepare_button.setEnabled(bool(report and test_id))
        actions.addWidget(self.prepare_button)
        actions.addWidget(_button('打开已有准备', self.reopen))
        actions.addStretch(1)
        preparation.addLayout(actions)
        self.body.addWidget(self.preparation_panel)

        self.plan_summary = _label()
        self.plan_summary.setStyleSheet('font-size: 14px; line-height: 1.5;')
        self.plan_summary.hide()
        self.body.addWidget(self.plan_summary)
        summary_actions = QHBoxLayout()
        self.evidence_toggle = _button('查看来源与测试差异', self._toggle_evidence)
        self.evidence_toggle.setObjectName('quiet')
        self.evidence_toggle.hide()
        self.adjust_button = _button('重新准备候选', self._adjust_candidate)
        self.adjust_button.setObjectName('quiet')
        self.adjust_button.hide()
        summary_actions.addWidget(self.evidence_toggle)
        summary_actions.addWidget(self.adjust_button)
        summary_actions.addStretch(1)
        self.body.addLayout(summary_actions)
        self.tabs = QTabWidget()
        self.tabs.setMaximumHeight(210)
        self.source_view = _editor(readonly=True)
        self.diff_view = _editor(readonly=True)
        self.tabs.addTab(self.source_view, '来源记录')
        self.tabs.addTab(self.diff_view, '测试差异')
        self.tabs.hide()
        self.body.addWidget(self.tabs)

        self.review_panel = QWidget()
        review = QFormLayout(self.review_panel)
        review.setContentsMargins(0, 12, 0, 0)
        review.setVerticalSpacing(14)
        self.problem = _editor()
        self.expected = _editor()
        self.problem.setFixedHeight(66)
        self.expected.setFixedHeight(66)
        self.problem.setPlaceholderText('这次修复了什么问题？')
        self.expected.setPlaceholderText('修复后应该满足什么行为？')
        self.failure_type = QLineEdit()
        self.failure_type.setReadOnly(True)
        self.message = _editor()
        self.message.setFixedHeight(62)
        self.message.setPlaceholderText('选取历史异常中稳定、非空的片段')
        review.addRow('问题', self.problem)
        review.addRow('期望行为', self.expected)
        review.addRow('历史异常', self.failure_type)
        self.message_choices = QComboBox()
        self.message_choices.activated.connect(self._choose_message)
        self.suggestion_notice = _label()
        review.addRow('观察到的消息', self.suggestion_notice)
        review.addRow('片段建议（待核对）', self.message_choices)
        review.addRow('匹配的消息片段', self.message)
        self.prefill_notice = _label()
        review.addRow(self.prefill_notice)
        self.review_panel.hide()
        self.body.addWidget(self.review_panel)
        self.body.addStretch(1)
        self.review_hint = _label()
        self.review_hint.setStyleSheet('color: #858b95; font-size: 12px;')
        self.confirm = QCheckBox('我已核对问题与验收条件，并确认使用原报告的完整测试。')
        self.outer_layout.insertWidget(1, self.review_hint)
        self.outer_layout.insertWidget(2, self.confirm)
        self.save_button = _button('保存修复案例', self.save)
        self.save_button.setObjectName('primary')
        self.save_button.setEnabled(False)
        self.go_button = _button('打开案例，转到验收', self.open_case)
        self.go_button.setEnabled(False)
        self.go_button.hide()
        self.footer_actions.insertWidget(0, self.save_button)
        self.footer_actions.insertWidget(1, self.go_button)
        self.confirm.toggled.connect(self._sync_confirmation)
        for widget in (self.problem, self.expected, self.message):
            widget.textChanged.connect(self._review_changed)
        self.repository.textChanged.connect(self._review_changed)
        self._sync_confirmation()

    def _choose_message(self, index):
        self.message.setPlainText(self.message_choices.itemData(index) or '')

    def _toggle_evidence(self):
        visible = self.tabs.isHidden()
        self.tabs.setVisible(visible)
        self.evidence_toggle.setText('收起来源详情' if visible else '查看来源与测试差异')

    def _adjust_candidate(self):
        self.preparation_panel.show()
        self.confirm.setChecked(False)
        self.repository.setFocus()

    def _review_changed(self, *_):
        self.confirm.setChecked(False)
        self.saved_case = None
        self.go_button.setEnabled(False)
        self.go_button.hide()
        self._sync_confirmation()

    def _sync_confirmation(self, *_):
        reason = ''
        if self.plan is None:
            reason = '请先冻结当前工作区，或重开一份已保存的修复准备。'
        else:
            try:
                candidate = self.repository.text().strip()
                if not candidate or Path(candidate).expanduser().resolve() != Path(self.plan['repository']).resolve():
                    raise ValueError('候选目录已改变，请重新冻结后再确认。')
                validate_repair_confirmation(self.plan, problem=self.problem.toPlainText(),
                    expected_behavior=self.expected.toPlainText(), failure_type=self.failure_type.text(),
                    message_contains=self.message.toPlainText())
            except (ValueError, OSError) as exc:
                reason = str(exc)
        valid = not reason
        self.confirm.setEnabled(valid)
        self.save_button.setEnabled(valid and self.confirm.isChecked())
        self.review_hint.setText(reason or '确认信息与历史观察匹配。请核对行为和消息片段的稳定性，再勾选保存。')

    def choose_repository(self):
        path = QFileDialog.getExistingDirectory(self, '选择修复后的候选工作区', self.repository.text())
        if path:
            self.repository.setText(path)
            self.status.setText('已选择候选目录。请点击“读取失败快照并冻结当前工作区”创建新的固定准备。')

    def prepare(self):
        if not self.is_current():
            self.status.setText('当前报告或测试已改变，请从现在的所选项重新打开准备入口。')
            return
        repository = self.repository.text().strip()
        self._run(lambda control: prepare_report_repair(self.report['report_dir'], self.test_id, self.output_root,
            repository=repository, expected_reference=self.reference, control=control), self._prepared,
            '正在校验来源证据并冻结当前工作区，不执行测试…')

    def _prepared(self, plan):
        if not self.is_current():
            self.status.setText('准备已保存，但当前报告或测试已改变；请重开以下修复准备继续：\n' + plan['directory'])
            return
        self._show_plan(plan)

    def _show_plan(self, plan):
        previous = self.plan
        preserve_inputs = bool(previous and previous['source_report'] == plan['source_report'] and previous['test_target'] == plan['test_target'])
        self.plan, self.saved_case = plan, None
        self.repository.setText(plan['repository'])
        self.preparation_panel.hide()
        self.plan_summary.setText('故障版  ·  原报告中实际失败的源码\n'
                                  '候选版  ·  ' + Path(plan['repository']).name + ' · ' + plan['candidate']['frozen_at'] + '\n'
                                  '候选指纹  ·  ' + plan['source_sha256']['after'][:12] + '（后续源码修改不会自动带入）\n'
                                  '两版使用同一份原始测试。保存后可在下一步明确执行。')
        self.plan_summary.show()
        self.evidence_toggle.show()
        self.adjust_button.setVisible(bool(self.report))
        self.review_panel.show()
        self.tabs.hide()
        self.evidence_toggle.setText('查看来源与测试差异')
        self.go_button.setEnabled(False)
        self.go_button.hide()
        ref, observed = plan['source_report'], plan['observation']
        lines = ['来源会话：' + ref['project_id'], '来源运行：' + ref['run_id'],
                 '报告 SHA-256：' + ref['report_sha256'], '完整测试 ID：' + plan['test_target'], '',
                 '本次故障版：原报告中实际失败的变更后快照',
                 '冻结：' + plan['failure_snapshot']['frozen_at'], '观察：' + plan['failure_snapshot']['observed_at'],
                 'SHA-256：' + plan['source_sha256']['before'], '',
                 '候选版：' + plan['candidate']['repository'] + ' 准备时的当前工作区',
                 '候选来源目录（本机记录）：' + plan['repository'],
                 '冻结：' + plan['candidate']['frozen_at'], 'SHA-256：' + plan['source_sha256']['after'], '',
                 '原报告基线（仅背景，不作为本次故障版）：' + plan['baseline_background']['commit'],
                 '历史两版状态：' + observed['before']['status'] + ' / ' + observed['after']['status'],
                 '实际故障阶段：call（setup / teardown 完整通过）',
                 '历史实际异常：' + observed['after']['exception']['type'], observed['after']['exception']['message'], '',
                 '采用原报告完整测试 SHA-256：' + plan['test_sha256'],
                 '当前测试文件：' + plan['test_change']['status'],
                 '测试文件变化仅供核对。正式执行将把原报告完整测试覆入两版运行副本，各版配置和 fixture 保留。',
                 '本入口不替换测试；需要新测试时必须先重新观察故障版并确认条件，不能直接沿用本次观察。',
                 '准备目录：' + plan['directory'], plan['notice']]
        self.source_view.setPlainText('\n'.join(lines))
        self.diff_view.setPlainText(plan['test_change']['diff'])
        if not preserve_inputs:
            self.prefill_notice.clear()
            self.problem.setPlainText(plan.get('issue') or '')
            self.expected.clear()
            draft = self.brief_draft
            if draft:
                binding = draft['identity']; ref = plan['source_report']
                matches = (binding['repository'] == plan['repository'] and binding['test_id'] == plan['test_target']
                    and binding['test_sha256'] == plan['test_sha256']
                    and all(binding[k] == ref[k] for k in ('project_id', 'run_id', 'report_sha256')))
                if matches:
                    self.prefill_notice.setText('问题与期望来自同一失败项的本机草稿，尚待确认。')
                    self.problem.setPlainText(draft['state']['inputs'][0])
                    self.expected.setPlainText(draft['state']['inputs'][1])
                    self.problem.setToolTip('来自同一失败项的本机草稿，尚未作为验收条件确认。')
                    self.expected.setToolTip(self.problem.toolTip())
        self.failure_type.setText(observed['after']['exception']['type'])
        suggested = message_suggestions(observed['after']['exception']['message'])
        self.suggestion_notice.setText('call · ' + observed['after']['exception']['type'] + '\n' + suggested['notice'])
        self.suggestion_notice.setToolTip(observed['after']['exception']['message'])
        self.message_choices.clear()
        self.message_choices.addItem('人工选取／编辑片段', '')
        for value in suggested['candidates']:
            self.message_choices.addItem(value, value)
        if not preserve_inputs:
            self.message.setPlainText(suggested['candidates'][0] if suggested['candidates'] else '')
            self.message_choices.setCurrentIndex(1 if suggested['candidates'] else 0)
        self.confirm.setChecked(False)
        self.save_button.setEnabled(False)
        self.status.setText('两版及完整测试已保存，可关闭后重开。请确认问题、期望行为和稳定消息片段，再保存正式案例。\n' + plan['directory'])
        if plan['test_change']['status'] != 'same':
            self.tabs.setCurrentWidget(self.diff_view)
            self.tabs.show()
            self.evidence_toggle.setText('收起来源详情')

    def reopen(self):
        path, _ = QFileDialog.getOpenFileName(self, '打开修复准备记录', str(self.output_root), '准备记录 (preparation.json)')
        if not path:
            self.status.setText('已取消打开，当前准备内容保留。')
            return
        self._run(lambda control: self._read_plan(path, control), self._reopened,
                  '正在核对准备目录的自带快照和完整测试；不读取原报告，不执行…')

    def _reopened(self, plan):
        self.report = None
        self.prepare_button.setEnabled(False)
        self.prepare_button.setToolTip('这是独立重开的固定准备；如需新候选，请回到原报告重新准备。')
        self._show_plan(plan)

    @staticmethod
    def _read_plan(path, control):
        control.check()
        plan = read_repair_preparation(Path(path).parent)
        control.check()
        return plan

    def save(self):
        self._sync_confirmation()
        if not self.save_button.isEnabled():
            return
        path, _ = QFileDialog.getSaveFileName(self, '保存新正式修复案例',
            str(Path(self.settings['cases']).expanduser() / (self.plan['preparation_id'] + '.json')), '案例 (*.json)')
        if not path:
            self.status.setText('已取消保存；冻结材料和确认内容保留，可再次保存。')
            return
        self._sync_confirmation()
        if not self.save_button.isEnabled():
            self.status.setText('确认条件已改变，请重新核对后保存。')
            return
        plan = self.plan
        options = dict(problem=self.problem.toPlainText(), expected_behavior=self.expected.toPlainText(),
            failure_type=self.failure_type.text(), message_contains=self.message.toPlainText(), confirmed=True,
            expected_preparation=plan['_preparation_sha256'])
        self._run(lambda control: save_report_repair(plan['directory'], path, control=control, **options),
                  self._saved, '正在保存新案例与自带的两版快照、测试及确认条件，不执行…')

    def _saved(self, result):
        self.saved_case = result['case']
        self.go_button.setEnabled(True)
        self.go_button.show()
        self.status.setText(result['notice'] + '\n' + self.saved_case + '\n可打开单项验收，明确点击运行；也可稍后从此路径打开。')

    def open_case(self):
        if self.saved_case and self.task is None:
            self.accept()
            self.caseReady.emit(self.saved_case)
