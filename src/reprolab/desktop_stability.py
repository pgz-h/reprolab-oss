"""Show stability observations and links to the saved replay rounds."""
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QHeaderView, QLabel, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .replay_stability import OUTCOME_LABELS, STABILITY_PREFIX, STABILITY_SUMMARY, stability_summary


_OUTCOMES = {'passed': '通过', 'failed': '失败', 'skipped': '跳过', 'inconclusive': '受阻',
             'not_run': '未执行'}
_VERDICTS = {'verified': '与原结果一致', 'different': '与原结果不同', 'incomplete': '记录不足',
             'unavailable': '无法核对', 'inputs_changed': '固定输入不同', 'same_run': '同一次运行'}


def _outcome(pair):
    return '—' if pair is None else ' → '.join(_OUTCOMES.get(pair[side], pair[side]) for side in ('before', 'after'))


def _conditions(check):
    values = tuple(check.get('conditions', {}).values())
    if False in values:
        return '有差异'
    return '已记录项一致' if values and all(values) else '部分未知'


def _table(headers, rows):
    table = QTableWidget(len(rows), len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setAlternatingRowColors(True)
    table.setTextElideMode(Qt.TextElideMode.ElideRight)
    table.verticalHeader().hide()
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
    table.setColumnWidth(0, 270)
    for index, row in enumerate(rows):
        for column, value in enumerate(row):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            table.setItem(index, column, item)
    return table


def _test_title(row):
    if 'file' in row:
        return row['test_id']
    path, separator, name = row['test_id'].partition('::')
    return name if separator else path


class StabilityResultDialog(QDialog):
    def __init__(self, result, parent, *, open_report, open_folder):
        super().__init__(parent)
        self.setObjectName('replayStabilityResult')
        self.setWindowTitle('重放稳定性')
        layout = QVBoxLayout(self)
        headline = QLabel(stability_summary(result))
        headline.setObjectName('stabilityHeadline')
        headline.setWordWrap(True)
        headline.setStyleSheet('font-size: 16px; font-weight: 600;')
        layout.addWidget(headline)
        if issue := result.get('issue'):
            notice = QLabel(issue)
            notice.setTextFormat(Qt.TextFormat.PlainText)
            notice.setWordWrap(True)
            notice.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(notice)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        by_status = {key: i for i, key in enumerate(('variable', 'consistently_different', 'incomplete', 'matched'))}
        entries = sorted(result['entries'], key=lambda row: by_status[row['status']])
        headers = ['测试（基线 → 候选）', '观察', '原运行'] + [f'第 {i} 轮' for i in range(1, result['requested_rounds'] + 1)]
        table = _table(headers, [
            (_test_title(row), OUTCOME_LABELS[row['status']], _outcome(row['source']), *map(_outcome, row['rounds']))
            for row in entries
        ])
        table.setObjectName('stabilityOutcomes')
        if result['requested_rounds'] <= 4:
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        names = [' · '.join(filter(None, (row.get('file'), row.get('project'), row['test_id']))) for row in entries]
        for index, name in enumerate(names):
            table.item(index, 0).setToolTip(name)
        outcomes_page = QWidget()
        outcomes_layout = QVBoxLayout(outcomes_page)
        outcomes_layout.setContentsMargins(0, 0, 0, 0)
        outcomes_layout.addWidget(table)
        selected = QLabel()
        selected.setWordWrap(True)
        selected.setTextFormat(Qt.TextFormat.PlainText)
        selected.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        selected.setAccessibleName('当前测试完整名称')
        outcomes_layout.addWidget(selected)
        table.currentCellChanged.connect(lambda row, *_: selected.setText(names[row] if row >= 0 else ''))
        if entries:
            table.selectRow(0)
        tabs.addTab(outcomes_page, '逐项结果')
        rounds = result['rounds']
        runs = _table(['报告', '轮次', '与原运行核对', '记录的运行条件'], [
            (Path(item['report_dir']).name, str(item['round']), _VERDICTS[item['verification']['status']],
             _conditions(item['verification']))
            for item in rounds
        ])
        runs.setObjectName('stabilityRounds')
        for index, item in enumerate(rounds):
            runs.item(index, 0).setToolTip(item['report_dir'])
        runs.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tabs.addTab(runs, '各轮报告')
        if issues := result.get('history_issues'):
            failures = _table(['无法核对的报告', '轮次'], [
                (item['message'], str(item['round'])) for item in issues
            ])
            failures.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            tabs.addTab(failures, '核对问题')

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText('关闭')
        buttons.rejected.connect(self.reject)
        report_button = buttons.addButton('打开所选报告', QDialogButtonBox.ButtonRole.ActionRole)
        report_button.setObjectName('stabilityOpenReport')
        report_button.setVisible(False)
        report_button.setEnabled(False)
        tabs.currentChanged.connect(lambda index: report_button.setVisible(index == 1))
        runs.itemSelectionChanged.connect(lambda: report_button.setEnabled(bool(runs.selectedItems())))
        report_button.clicked.connect(lambda: open_report(rounds[runs.currentRow()]['report_dir']))
        folder = buttons.addButton('打开保存目录', QDialogButtonBox.ButtonRole.ActionRole)
        folder.clicked.connect(open_folder)
        layout.addWidget(buttons)
        self.resize(870, 490)


class StabilityHistoryDialog(QDialog):
    def __init__(self, result, parent):
        super().__init__(parent)
        self.setWindowTitle('稳定性历史')
        self.setObjectName('replayStabilityHistory')
        self.selected_summary = None
        checks = result['checks']
        layout = QVBoxLayout(self)
        if not checks:
            layout.addWidget(QLabel('此报告暂无稳定性检查记录。'))
        if result['skipped'] or result['truncated']:
            layout.addWidget(QLabel('部分记录无法读取，或超出最近 50 次的显示范围。'))
        statuses = {'verified': '每轮均复现', 'different': '观察到结果差异', 'incomplete': '记录不足',
                    'cancelled': '已取消', 'running': '仅有过程记录'}
        table = _table(['检查时间（本地）', '已保存轮次', '当时记录'], [
            (self._check_date(item['check_id']), f'{item["saved_rounds"]}/{item["requested_rounds"]}',
             statuses[item['recorded_status']])
            for item in checks
        ])
        table.setObjectName('stabilityHistoryChecks')
        for index, item in enumerate(checks):
            table.item(index, 0).setToolTip(item['check_id'])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText('关闭')
        buttons.rejected.connect(self.reject)
        open_button = buttons.addButton('复核所选检查', QDialogButtonBox.ButtonRole.AcceptRole)
        open_button.setObjectName('stabilityHistoryOpen')
        open_button.setEnabled(False)
        browse = buttons.addButton('打开摘要…', QDialogButtonBox.ButtonRole.ActionRole)
        browse.setObjectName('stabilityHistoryBrowse')

        def browse_summary():
            start = str(Path(checks[0]['summary_file']).parent.parent) if checks else str(Path.home())
            path, _ = QFileDialog.getOpenFileName(self, '打开稳定性摘要', start, f'稳定性摘要 ({STABILITY_SUMMARY})')
            if path:
                self.selected_summary = path
                self.accept()

        def open_selected():
            if table.currentRow() >= 0:
                self.selected_summary = checks[table.currentRow()]['summary_file']
                self.accept()

        buttons.accepted.connect(open_selected)
        browse.clicked.connect(browse_summary)
        table.cellDoubleClicked.connect(lambda *_: open_selected())
        table.itemSelectionChanged.connect(lambda: open_button.setEnabled(bool(table.selectedItems())))
        if checks:
            table.selectRow(0)
        layout.addWidget(buttons)
        self.resize(680, 350)

    @staticmethod
    def _check_date(check_id):
        stamp = check_id.removeprefix(STABILITY_PREFIX).partition('-')[0]
        try:
            created = datetime.strptime(stamp, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        except ValueError:
            return check_id
        return created.astimezone().strftime('%Y-%m-%d %H:%M:%S')
