"""Inspect and share a comparison of two saved runs."""
import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSplitter,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from .persistence import atomic_write
from .project_history import CHANGE_LABELS, TEST_LABELS, EXECUTION_LABELS, CONDITION_LABELS

_STATUS = {'regression_passed': '两版通过', 'regression_detected': '新增失败',
           'baseline_failure': '原有失败', 'improved': '已改善', 'inconclusive': '受阻'}


class RunComparisonDialog(QDialog):
    def __init__(self, comparison, parent=None):
        super().__init__(parent)
        self.comparison = comparison
        self.setWindowTitle('两次运行结果对比')
        self.resize(1040, 760)
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        scope = comparison['scope']
        self.summary.setText(comparison.get('summary') or
            f"共同选中 {scope['common']} 项 · 前次 {scope['before_count']} 项 · 本次 {scope['after_count']} 项")
        layout.addWidget(self.summary)
        selection = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索测试 ID…')
        self.filter = QComboBox()
        self.filter.addItem('全部对比', '')
        for key, label in CHANGE_LABELS.items():
            self.filter.addItem(label, key)
        selection.addWidget(self.search, 1)
        selection.addWidget(self.filter)
        layout.addLayout(selection)
        split = QSplitter(Qt.Orientation.Vertical)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['测试 ID', '变化', '前次实际结果', '本次实际结果', '共享测试文件', '本轮执行'])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        short_labels = {'resolved': '条件一致，转为通过', 'conditions_changed': '条件变化，需复核',
                        'conditions_unknown': '条件缺失，需复核'}
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for entry in comparison['entries']:
            item = QTreeWidgetItem([entry['test_id'], short_labels.get(entry['change'], CHANGE_LABELS[entry['change']]),
                    _STATUS.get((entry['before'] or {}).get('status'), '未选中'),
                    _STATUS.get((entry['after'] or {}).get('status'), '未选中'),
                    TEST_LABELS.get((entry.get('test_file') or {}).get('status'), '未知'),
                    EXECUTION_LABELS.get(entry.get('after_execution'), '未知')])
            item.setData(0, Qt.ItemDataRole.UserRole, entry)
            item.setToolTip(0, entry['test_id'])
            item.setToolTip(1, CHANGE_LABELS[entry['change']])
            self.tree.addTopLevelItem(item)
        split.addWidget(self.tree)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        split.addWidget(self.details)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        layout.addWidget(split, 1)
        self.notice = QPlainTextEdit('\n'.join(comparison['notices']))
        self.notice.setReadOnly(True)
        self.notice.setMaximumHeight(140)
        self.notice.hide()
        self.notice_button = QPushButton('查看比较依据')
        self.notice_button.clicked.connect(self.toggle_notice)
        layout.addWidget(self.notice_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.notice)
        actions = QHBoxLayout()
        copy = QPushButton('复制对比摘要')
        copy.clicked.connect(self.copy_summary)
        export = QPushButton('导出对比 JSON…')
        export.clicked.connect(self.export_json)
        actions.addWidget(copy)
        actions.addWidget(export)
        actions.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText('关闭')
        close.rejected.connect(self.reject)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.search.textChanged.connect(self.apply_filter)
        self.filter.currentIndexChanged.connect(self.apply_filter)
        self.tree.currentItemChanged.connect(self.show_entry)
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def toggle_notice(self):
        self.notice.setVisible(self.notice.isHidden())
        self.notice_button.setText('收起比较依据' if not self.notice.isHidden() else '查看比较依据')

    def apply_filter(self, *_):
        needle = self.search.text().casefold().strip()
        mode = self.filter.currentData()
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            entry = item.data(0, Qt.ItemDataRole.UserRole)
            if mode in ('blocked', 'not_run'):
                matches = entry.get('after_execution') == mode
            elif mode == 'still_failed':
                matches = entry.get('state_change') == mode
            else:
                matches = not mode or entry['change'] == mode
            item.setHidden(not matches or needle not in entry['test_id'].casefold())
        current = self.tree.currentItem()
        if current and current.isHidden():
            self.tree.setCurrentItem(next((self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())
                                          if not self.tree.topLevelItem(i).isHidden()), None))

    def show_entry(self, item, *_):
        entry = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if entry is None:
            self.details.clear()
            return
        lines = [entry['test_id'], '变化：' + CHANGE_LABELS[entry['change']], '']
        test_file = entry.get('test_file') or {}
        lines.extend(['共享测试文件：' + str(test_file.get('path') or '未知'),
                      '指纹核对：' + TEST_LABELS.get(test_file.get('status'), '未知'),
                      '前次 SHA-256：' + str(test_file.get('before_sha256') or '未知'),
                      '本次 SHA-256：' + str(test_file.get('after_sha256') or '未知'), ''])
        for key, value in (entry.get('conditions') or {}).items():
            label = '相同' if value is True else '已变化，需复核' if value is False else '记录不完整，需复核'
            lines.append(CONDITION_LABELS.get(key, key) + '：' + label)
        changed = entry.get('changed_support_files') or []
        if changed:
            lines.extend(['变化的辅助文件：', *changed[:100]])
            if len(changed) > 100:
                lines.append(f'另有 {len(changed) - 100} 个文件，完整清单见导出 JSON。')
        lines.append('')
        for key, title in (('before', '前次运行'), ('after', '后次运行')):
            record = entry[key]
            lines.append(title)
            if record is None:
                lines.extend(['这次运行没有选中该项，不推断其执行结果。', ''])
                continue
            lines.append('结果：' + _STATUS.get(record.get('status'), str(record.get('status') or '未知')))
            for side, name in (('before', '基线版本'), ('after', '变更后版本')):
                outcome = record.get(side) or {}
                labels = {'passed': '通过', 'failed': '失败', 'skipped': '跳过',
                          'inconclusive': '受阻', 'not_run': '未执行'}
                lines.append(name + '：' + labels.get(outcome.get('status'), str(outcome.get('status') or '未知')))
                error = outcome.get('exception')
                if isinstance(error, dict) and error:
                    lines.append('异常：' + str(error.get('type') or '') + ': ' + str(error.get('message') or ''))
                if outcome.get('duration_seconds') is not None:
                    lines.append('耗时（秒）：' + str(outcome['duration_seconds']))
            lines.append('')
        self.details.setPlainText('\n'.join(lines))

    def copy_summary(self):
        result = self.comparison
        lines = ['ReproLab 两次运行对比',
                 str(result['before'].get('run_id')) + ' → ' + str(result['after'].get('run_id')),
                 self.summary.text(), *result['notices'], '']
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if not item.isHidden():
                lines.append(item.text(0) + ' · ' + item.text(1) + ' · ' + item.text(2) + ' → ' + item.text(3)
                             + ' · 测试文件：' + item.text(4) + ' · 本轮执行：' + item.text(5))
        text = '\n'.join(lines)
        if len(text) > 200000:
            text = text[:200000] + '\n…摘要已截断，完整内容可导出 JSON。'
        QApplication.clipboard().setText(text)

    def export_json(self):
        target, _ = QFileDialog.getSaveFileName(self, '导出运行对比', 'run-comparison.json', 'JSON (*.json)')
        if not target:
            return
        path = Path(target).expanduser().resolve()
        if any(path.is_relative_to(Path(self.comparison[key]['report_dir']).resolve()) for key in ('before', 'after')):
            self.notice.setPlainText('对比摘要应保存在来源报告目录之外。')
            self.notice.show()
            return
        try:
            atomic_write(path, json.dumps(self.comparison, ensure_ascii=False, indent=2))
            self.notice.setPlainText('已导出对比摘要：' + str(path))
            self.notice.show()
        except OSError as exc:
            self.notice.setPlainText('导出未完成：' + str(exc))
            self.notice.show()
