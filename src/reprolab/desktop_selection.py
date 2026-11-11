"""Review every requested and missing test before replacing a selection."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from .project_selections import reconcile_selection


class SelectionReviewDialog(QDialog):
    def __init__(self, selected, available, *, title='核对测试选择', parent=None, collection_complete=True, discovery_status='collected'):
        super().__init__(parent)
        self.review = reconcile_selection(selected, available, collection_complete=collection_complete)
        self.setWindowTitle(title)
        self.resize(820, 520)
        self.setMinimumSize(560, 360)
        layout = QVBoxLayout(self)
        result = self.review
        summary = QLabel(f'原选择 {result["requested_count"]} 项 · 当前匹配 {result["matched_count"]} 项'
                         f' · 本次未找到 {result["missing_count"]} 项 · 其他可选 {result["other_count"]} 项')
        if discovery_status == 'uncollected':
            summary.setText(f'原选择 {result["requested_count"]} 项 · 尚未发现，等待核对')
        summary.setWordWrap(True)
        layout.addWidget(summary)
        can_apply = discovery_status == 'collected'
        notice = QLabel(('尚无可用发现，只能查看原范围，请处理环境／日志后发现测试。' if not can_apply else
                         '发现列表不完整，本次未找到不代表已删除；请明确审阅采用的范围。' if not collection_complete else
                         '本次未找到的目标可能改名或受环境影响，不自动替换。' if result['missing_tests'] else
                         '原范围未指定。' if selected is None else '原范围为空，采用后仍保持空选择。' if not selected else
                         '完整 ID 已匹配。') + ' 采用后仍需实际运行；其他测试不会自动加入。')
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索完整测试 ID…')
        self.missing_only = QCheckBox('只看本次未找到项')
        self.missing_only.setChecked(bool(result['missing_tests']))
        filters = QHBoxLayout()
        filters.addWidget(self.search, 1)
        filters.addWidget(self.missing_only)
        layout.addLayout(filters)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['完整测试 ID', '核对结果'])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 580)
        missing = set(result['missing_tests'])
        for identity in result['requested_tests'] or []:
            absent = identity in missing
            item = QTreeWidgetItem([identity, '待发现核对' if discovery_status == 'uncollected' else '本次未找到' if absent else '本次可用'])
            item.setToolTip(0, identity)
            item.setData(0, Qt.ItemDataRole.UserRole, absent)
            self.tree.addTopLevelItem(item)
        layout.addWidget(self.tree, 1)
        buttons = QHBoxLayout()
        copy_missing = QPushButton('复制全部本次未找到 ID')
        copy_missing.setEnabled(bool(missing))
        copy_missing.clicked.connect(lambda: QApplication.clipboard().setText('\n'.join(result['missing_tests'])))
        buttons.addWidget(copy_missing)
        buttons.addStretch(1)
        cancel = QPushButton('保留当前勾选')
        cancel.setDefault(True)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        apply = QPushButton(f'仅采用可用 {result["matched_count"]} 项' if result['requested_tests'] else '采用空选择')
        apply.setAutoDefault(False)
        apply.setEnabled(can_apply and selected is not None and (bool(result['matched_tests']) or not result['requested_tests']))
        apply.clicked.connect(self.accept)
        buttons.addWidget(apply)
        layout.addLayout(buttons)
        self.search.textChanged.connect(self._filter)
        self.missing_only.toggled.connect(self._filter)
        self._filter()

    def _filter(self, *_):
        needle = self.search.text().strip().casefold()
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            item.setHidden((self.missing_only.isChecked() and not item.data(0, Qt.ItemDataRole.UserRole))
                           or (bool(needle) and needle not in item.text(0).casefold()))
