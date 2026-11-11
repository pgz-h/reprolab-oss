"""A checked source chain, per-test observations and separate local annotations."""
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .desktop_tasks import Task
from .desktop_history import RunComparisonDialog
from .project_history import (_checked_report, _match_reference, compare_project_reports,
                              CHANGE_LABELS, CONDITION_LABELS)
from .processing_history import (read_processing_history, save_run_note, format_processing_history,
                                  export_processing_history, MAX_NOTE_CHARS, STATUS_LABELS)

ROLE = Qt.ItemDataRole.UserRole
EXECUTION = {'passed': '通过', 'failed': '失败', 'blocked': '受阻', 'not_run': '未完整执行'}


def label(text=''):
    widget = QLabel(text)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def tree(headers):
    widget = QTreeWidget()
    widget.setHeaderLabels(headers)
    widget.setRootIsDecorated(False)
    widget.setAlternatingRowColors(True)
    widget.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for column in range(1, len(headers)):
        widget.header().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
    return widget


def saved_time(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone().strftime('%m-%d %H:%M:%S')
    except (AttributeError, ValueError):
        return str(value or '未记录')


class ProcessingHistoryDialog(QDialog):
    busyChanged = Signal(bool)
    locationChanged = Signal(str, str)

    def __init__(self, data, notes_root, locations=None, parent=None):
        super().__init__(parent)
        self.task = None
        self._closing = False
        self._loading = False
        self._active_round = None
        self._saved_note = ''
        self.notes_root = Path(notes_root)
        self.locations = dict(locations or {})
        self.comparison_dialog = None
        self.setWindowTitle('多轮处理记录')
        self.resize(1100, 790)
        layout = QVBoxLayout(self)
        self.content = QWidget()
        body = QVBoxLayout(self.content)
        body.setContentsMargins(0, 0, 0, 0)
        self.summary = label()
        self.summary.setStyleSheet('font-size: 15px; font-weight: 600;')
        self.gap_notice = label()
        self.gap_notice.setStyleSheet('color: #94602a;')
        body.addWidget(self.summary)
        body.addWidget(self.gap_notice)
        self.tabs = QTabWidget()
        body.addWidget(self.tabs, 1)
        rounds_page, tests_page = QWidget(), QWidget()
        rounds_layout, tests_layout = QVBoxLayout(rounds_page), QVBoxLayout(tests_page)
        self.tabs.addTab(rounds_page, '各轮记录')
        self.tabs.addTab(tests_page, '测试去向')
        split = QSplitter(Qt.Orientation.Vertical)
        self.rounds_tree = tree(['来源轮次', '保存时间', '实际结果', '范围', '与上一来源比较'])
        split.addWidget(self.rounds_tree)
        self.round_details = QPlainTextEdit()
        self.round_details.setReadOnly(True)
        split.addWidget(self.round_details)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        rounds_layout.addWidget(split, 1)
        rounds_layout.addWidget(label('本轮处理说明（本机另存，不改变测试结论）'))
        self.note_edit = QPlainTextEdit()
        self.note_edit.setPlaceholderText('例如：修复金额格式；本轮只复查了支付相关测试，其余留待下一轮。')
        self.note_edit.setMaximumHeight(85)
        rounds_layout.addWidget(self.note_edit)
        note_row = QHBoxLayout()
        self.note_state = label()
        self.save_note_button = QPushButton('保存本轮说明')
        note_row.addWidget(self.note_state, 1)
        note_row.addWidget(self.save_note_button)
        rounds_layout.addLayout(note_row)
        actions = QHBoxLayout()
        self.open_button = QPushButton('打开这轮报告')
        self.compare_button = QPushButton('与上一来源比较')
        actions.addWidget(self.open_button)
        actions.addWidget(self.compare_button)
        actions.addStretch(1)
        rounds_layout.addLayout(actions)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索完整测试 ID…')
        self.filter = QComboBox()
        for text, value in (('全部历史测试', 'all'), ('本轮未复查', 'omitted'), ('本轮失败／受阻', 'attention'), ('本轮通过', 'passed')):
            self.filter.addItem(text, value)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.filter)
        tests_layout.addLayout(filters)
        self.test_count = label()
        tests_layout.addWidget(self.test_count)
        test_split = QSplitter(Qt.Orientation.Vertical)
        self.tests_tree = tree(['测试', '本轮', '最近一次观察', '观察来源', '与上次观察比较'])
        test_split.addWidget(self.tests_tree)
        self.test_details = QPlainTextEdit()
        self.test_details.setReadOnly(True)
        test_split.addWidget(self.test_details)
        test_split.setStretchFactor(0, 3)
        test_split.setStretchFactor(1, 2)
        tests_layout.addWidget(test_split, 1)
        layout.addWidget(self.content, 1)
        self.status_label = label()
        layout.addWidget(self.status_label)
        bottom = QHBoxLayout()
        self.reload_button = QPushButton('重新核对')
        self.relocate_button = QPushButton('重新定位来源…')
        self.copy_button = QPushButton('复制处理摘要')
        self.export_button = QPushButton('导出处理记录…')
        self.cancel_button = QPushButton('取消读取')
        for button in (self.reload_button, self.relocate_button, self.copy_button, self.export_button, self.cancel_button):
            bottom.addWidget(button)
        bottom.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText('关闭')
        close.rejected.connect(self.reject)
        bottom.addWidget(close)
        layout.addLayout(bottom)
        self.rounds_tree.currentItemChanged.connect(self._select_round)
        self.tests_tree.currentItemChanged.connect(self._select_test)
        self.note_edit.textChanged.connect(self._sync)
        self.search.textChanged.connect(self._filter_tests)
        self.filter.currentIndexChanged.connect(self._filter_tests)
        self.save_note_button.clicked.connect(self.save_note)
        self.open_button.clicked.connect(self.open_report)
        self.compare_button.clicked.connect(self.compare_previous)
        self.reload_button.clicked.connect(self.reload_history)
        self.relocate_button.clicked.connect(self.relocate_source)
        self.copy_button.clicked.connect(self.copy_summary)
        self.export_button.clicked.connect(self.export_history)
        self.cancel_button.clicked.connect(self.cancel_operation)
        self._apply(data)

    def _dirty(self):
        return self.note_edit.toPlainText() != self._saved_note

    def _confirm_discard(self):
        if not self._dirty():
            return True
        return QMessageBox.question(self, '处理说明尚未保存', '是否放弃当前未保存的说明？选择取消可返回保存或继续编辑。',
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Discard

    def _sync(self, *_):
        idle = self.task is None and not self._closing
        self.content.setEnabled(idle)
        for button in (self.reload_button, self.copy_button, self.export_button):
            button.setEnabled(idle)
        gap = self.data.get('source_gap')
        self.relocate_button.setEnabled(bool(idle and gap and gap.get('reference') and gap['reason'] in ('missing', 'unavailable')))
        self.reload_button.setText('读取更多来源（最多50轮）' if gap and gap['reason'] == 'limit' and self.data['max_rounds'] < 50 else '重新核对')
        self.cancel_button.setEnabled(self.task is not None)
        length = len(self.note_edit.toPlainText())
        self.note_state.setText(f'{length} / {MAX_NOTE_CHARS} 字符' + (' · 超过上限，草稿保留' if length > MAX_NOTE_CHARS else ' · 未保存' if self._dirty() else ' · 已与本机记录同步'))
        self.save_note_button.setEnabled(bool(idle and self._active_round and self._dirty() and length <= MAX_NOTE_CHARS
                                             and not self._active_round['note'].get('error')))
        self.open_button.setEnabled(bool(idle and self._active_round))
        self.compare_button.setEnabled(bool(idle and self._active_round and self._active_round['number'] > 1))

    def _apply(self, data):
        self.data = data
        self.setWindowTitle('多轮处理记录 · ' + str(data['rounds'][-1].get('title') or '')[:100])
        self._loading = True
        self._active_round = None
        self._saved_note = ''
        self.note_edit.clear()
        self.rounds_tree.clear()
        self.tests_tree.clear()
        summary = data['summary']
        counts = summary['current_execution_counts']
        self.summary.setText(f"已核对 {summary['rounds']} 轮来源 · 此区间包含 {summary['unique_tests']} 项测试\n"
            + f"本轮检查 {summary['selected_now']} 项：通过 {counts.get('passed', 0)} · 失败 {counts.get('failed', 0)} · 受阻／未完整执行 {counts.get('blocked', 0) + counts.get('not_run', 0)}\n"
            + f"本轮未复查 {summary['not_rechecked']} 项，其中最近一次需处理 {summary['not_rechecked_last_attention']} 项。")
        gap = data.get('source_gap')
        if gap:
            self.summary.setText(self.summary.text().replace('本轮未复查 ', '已核对范围内未复查 ') + '\n更早的测试范围尚未读取。')
        self.gap_notice.setStyleSheet('color: #94602a;' if gap else 'color: #717781;')
        self.gap_notice.setText('来源链未完整读取：' + gap['message'] + ' 当前统计仅覆盖已核对区间。' if gap else '已核对保存的复查来源。各轮结果与本机处理说明分别保留。')
        for row in data['rounds']:
            change = row['comparison']
            text = '已核对区间起点' if gap else '已记录起点'
            if change is not None:
                text = ' · '.join(f'{name} {change["state_counts"].get(key, 0)}' for key, name in
                    (('became_passed', '失败转通过'), ('now_passed', '新增通过记录'), ('deteriorated', '转为失败'),
                     ('only_after', '新增选择'), ('only_before', '未复查')) if change['state_counts'].get(key)) or '共同项结果未变化'
            item = QTreeWidgetItem([f"第 {row['number']} 轮" + ('（当前）' if row['is_current'] else ''), saved_time(row['created_at']),
                                   STATUS_LABELS.get(row['status'], row['status'] or '未知'), str(row['selected_count']) + ' 项', text])
            item.setData(0, ROLE, row['number'])
            item.setToolTip(0, row['reference']['run_id'])
            self.rounds_tree.addTopLevelItem(item)
        for row in data['tests']:
            last, comparison = row['latest_observation'], row['comparison']
            current = EXECUTION.get(last['execution'], '未知') if row['selected_now'] else '本轮未复查'
            change = CHANGE_LABELS.get(comparison['change'], comparison['change']) if comparison else '本轮未复查' if not row['selected_now'] else '无更早观察可比'
            item = QTreeWidgetItem([row['test_id'], current, STATUS_LABELS.get(last['status'], last['status']), f"第 {last['round']} 轮", change])
            item.setData(0, ROLE, row)
            self.tests_tree.addTopLevelItem(item)
        self._loading = False
        self.rounds_tree.setCurrentItem(self.rounds_tree.topLevelItem(self.rounds_tree.topLevelItemCount() - 1))
        self._filter_tests()
        if self.tests_tree.topLevelItemCount():
            self.tests_tree.setCurrentItem(self.tests_tree.topLevelItem(0))
        self.status_label.setText(data['notice'])
        self._sync()

    def _select_round(self, item, previous=None):
        if self._loading or item is None:
            return
        if not self._confirm_discard():
            self.rounds_tree.blockSignals(True)
            self.rounds_tree.setCurrentItem(previous)
            self.rounds_tree.blockSignals(False)
            return
        row = self._active_round = self.data['rounds'][item.data(0, ROLE) - 1]
        self._saved_note = row['note']['text']
        self.note_edit.setPlainText(self._saved_note)
        lines = [f"第 {row['number']} 轮 · 实际选择 {row['selected_count']} 项 · {STATUS_LABELS.get(row['status'], row['status'])}"]
        if row['comparison']:
            lines.extend(['', '与上一已记录来源比较：', row['comparison']['summary']])
        changes = row['candidate_changes']
        if changes and changes['available']:
            lines.extend(['', f"候选快照的保存文件变化：{changes['count']} 个"])
            lines.extend(item['kind'] + ' · ' + item['path'] for item in changes['files'])
            if changes['truncated']:
                lines.append('此处展示前 200 个变化文件。')
        if row['comparison']:
            lines.extend(['', '比较依据：', *row['comparison']['notices']])
        lines.extend(['', '运行：' + row['reference']['run_id'], '报告：' + row['reference']['report_dir']])
        if row['note'].get('error'):
            lines.extend(['', '本机说明读取问题：' + row['note']['error']])
        self.round_details.setPlainText('\n'.join(lines))
        self._sync()

    def _filter_tests(self, *_):
        needle, mode = self.search.text().strip().casefold(), self.filter.currentData()
        visible = 0
        for index in range(self.tests_tree.topLevelItemCount()):
            item = self.tests_tree.topLevelItem(index)
            row = item.data(0, ROLE)
            matches = (mode == 'all' or mode == 'omitted' and not row['selected_now']
                       or mode == 'passed' and row['selected_now'] and row['latest_observation']['execution'] == 'passed'
                       or mode == 'attention' and row['selected_now'] and row['latest_observation']['execution'] != 'passed')
            show = matches and (not needle or needle in row['test_id'].casefold())
            item.setHidden(not show)
            visible += int(show)
        self.test_count.setText(f'显示 {visible} / {self.tests_tree.topLevelItemCount()} 项 · 未复查项的历史通过或失败不代表当前结果')
        current = self.tests_tree.currentItem()
        if current is not None and current.isHidden():
            self.tests_tree.setCurrentItem(next((self.tests_tree.topLevelItem(i) for i in range(self.tests_tree.topLevelItemCount()) if not self.tests_tree.topLevelItem(i).isHidden()), None))

    def _select_test(self, item, *_):
        if item is None:
            self.test_details.clear()
            return
        row = item.data(0, ROLE)
        lines = [row['test_id'], '本轮已实际选择。' if row['selected_now'] else '本轮未复查；下列只是历史观察，不作为当前通过或仍失败的结论。', '', '保存的逐轮观察：']
        lines.extend(f"第 {observation['round']} 轮：{STATUS_LABELS.get(observation['status'], observation['status'])}" for observation in row['observations'])
        comparison = row['comparison']
        if comparison:
            lines.extend(['', f"与最近一次观察（第 {comparison['previous_round']} 轮）比较：" + CHANGE_LABELS[comparison['change']]])
            for key, value in comparison['conditions'].items():
                lines.append(CONDITION_LABELS[key] + '：' + ('相同' if value is True else '不同' if value is False else '未知'))
            lines.append('共享测试文件：' + comparison['test_file']['status'])
        error = row['latest_observation'].get('exception')
        if error:
            lines.extend(['', '最近保存的异常摘要：' + str(error['type']), error['message']])
        self.test_details.setPlainText('\n'.join(lines))

    def _run(self, action, callback, message, *, deliver_on_close=False):
        if self.task or self._closing:
            return
        self.status_label.setText(message)
        task = self.task = Task(action, self)
        def succeeded(result):
            if not self._closing or deliver_on_close:
                try:
                    callback(result)
                except Exception as exc:
                    self.status_label.setText('结果未能显示：' + str(exc))
        task.succeeded.connect(succeeded, Qt.ConnectionType.QueuedConnection)
        task.failed.connect(lambda error: self.status_label.setText('未完成：' + error), Qt.ConnectionType.QueuedConnection)
        task.cancelled.connect(lambda: self.status_label.setText('已取消操作；现有记录保留。'), Qt.ConnectionType.QueuedConnection)
        task.progress.connect(lambda phase, payload: self.status_label.setText(str((payload or {}).get('message') or '正在核对来源…')), Qt.ConnectionType.QueuedConnection)
        task.finished.connect(self._finished)
        self._sync()
        self.busyChanged.emit(True)
        task.start()

    def _finished(self):
        task = self.task
        self.task = None
        task.deleteLater()
        self._sync()
        self.busyChanged.emit(False)
        if self._closing:
            super().reject()

    def save_note(self):
        row = self._active_round
        if not row or not self.save_note_button.isEnabled():
            return
        text, reference = self.note_edit.toPlainText(), dict(row['reference'])
        def saved(note):
            row['note'] = note
            self._saved_note = text
            self.status_label.setText('本轮说明已保存到本机；原报告与执行结论未改变。')
        self._run(lambda control: save_run_note(reference, text, self.notes_root, expected_note_sha256=row['note']['sha256'], control=control),
                  saved, '正在核对报告身份并保存本机说明…', deliver_on_close=True)

    def reload_history(self):
        if not self._confirm_discard():
            return
        self.note_edit.setPlainText(self._saved_note)
        reference = dict(self.data['current'])
        limit = 50 if (self.data.get('source_gap') or {}).get('reason') == 'limit' else self.data['max_rounds']
        self._run(lambda control: read_processing_history(reference['report_dir'], expected_report=reference,
            locations=self.locations, notes_root=self.notes_root, max_rounds=limit, control=control), self._apply, '正在重新核对已记录来源…')

    def relocate_source(self):
        gap = self.data.get('source_gap')
        if not self.relocate_button.isEnabled() or not gap or not self._confirm_discard():
            return
        reference = dict(gap['reference'])
        path = QFileDialog.getExistingDirectory(self, '选择这份来源报告的新位置（仍核对身份）', str(Path(reference['report_dir']).parent))
        if not path:
            return
        self.note_edit.setPlainText(self._saved_note)
        locations = dict(self.locations)
        def action(control):
            report = _checked_report(path, control=control)
            _match_reference(report, reference)
            locations[reference['report_sha256']] = report['report_dir']
            return read_processing_history(self.data['current']['report_dir'], expected_report=self.data['current'],
                locations=locations, notes_root=self.notes_root, max_rounds=self.data['max_rounds'], control=control)
        def shown(data):
            self.locations = locations
            self._apply(data)
            self.locationChanged.emit(reference['report_sha256'], str(Path(path).resolve()))
        self._run(action, shown, '正在核对所选来源身份和证据；不会改写任何报告…')

    def open_report(self):
        if not self._active_round:
            return
        reference = dict(self._active_round['reference'])
        def action(control):
            report = _checked_report(reference['report_dir'], control=control)
            _match_reference(report, reference)
            return Path(report['report_dir']) / 'report.html'
        def shown(path):
            self.status_label.setText('已交给系统打开该轮只读报告。' if QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))) else '系统未能打开报告，请从保存目录查看。')
        self._run(action, shown, '正在重新核对所选报告后打开…')

    def compare_previous(self):
        if not self._active_round or self._active_round['number'] <= 1:
            return
        left = dict(self.data['rounds'][self._active_round['number'] - 2]['reference'])
        right = dict(self._active_round['reference'])
        def action(control):
            control.check()
            comparison = compare_project_reports(left['report_dir'], right['report_dir'], expected_before=left, expected_after=right)
            control.check()
            return comparison
        def shown(comparison):
            dialog = self.comparison_dialog = RunComparisonDialog(comparison, self)
            def closed(*_):
                self.comparison_dialog = None
                dialog.deleteLater()
            dialog.finished.connect(closed)
            dialog.setModal(True)
            dialog.show()
        self._run(action, shown, '正在核对并比较所选轮次与它的上一来源…')

    def copy_summary(self):
        QApplication.clipboard().setText(format_processing_history(self.data))
        self.status_label.setText('已复制处理摘要和已保存说明；未保存的草稿不包含在内。')

    def export_history(self):
        path, _ = QFileDialog.getSaveFileName(self, '导出处理记录（包含已保存本机说明）',
            str(Path(self.data['current']['report_dir']).parent / ('history-' + self.data['current']['run_id'] + '.json')), 'JSON (*.json)')
        if not path:
            return
        try:
            self.data['summary_text'] = format_processing_history(self.data)
            result = export_processing_history(self.data, path)
            self.status_label.setText('处理记录已导出：' + result['output'])
        except (OSError, ValueError) as exc:
            self.status_label.setText('导出未完成：' + str(exc))

    def cancel_operation(self):
        if self.task:
            accepted = self.task.control.cancel()
            self.status_label.setText('正在取消读取，请等待结束。' if accepted else '说明已进入保存阶段，正在完成保存。')

    def reject(self):
        if self.comparison_dialog is not None and self.comparison_dialog.isVisible():
            self.comparison_dialog.reject()
        if not self._closing and not self._confirm_discard():
            return
        if self.task:
            self._closing = True
            self.cancel_operation()
            return
        super().reject()

    def closeEvent(self, event):
        if self.task or self._dirty():
            self.reject()
            if self.isVisible():
                event.ignore()
                return
        super().closeEvent(event)
