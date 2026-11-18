"""Choose repair requirements and review the checked results in the desktop workbench."""
from collections import Counter
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHeaderView,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .acceptance_plan import AcceptancePlan, baseline_pin, read_acceptance_plan, save_acceptance_plan
from .project_comparison import comparison_scope_message
from .project_review_summary import render_review_markdown
from .replay_stability import MAX_ROUNDS, MIN_ROUNDS, STABILITY_SUMMARY
from .replay_verification import _CONDITION_LABELS


_STATUS = {'passed': '通过', 'failed': '失败', 'blocked': '受阻'}
_STATUS_COLOR = {'passed': '#176348', 'failed': '#a43a4d', 'blocked': '#865315'}
_OUTCOMES = {
    'improved': '基线失败 → 候选通过', 'regression_passed': '两版通过',
    'baseline_failure': '两版失败', 'regression_detected': '新增失败',
    'inconclusive': '无完整可比较结果', None: '报告中缺少可核对的结果',
}
_POLICIES = {'strict': '严格', 'no-new-failures': '允许原有失败'}
_COMPARISON = {
    'selected_scope_passed': '所选范围通过', 'new_failures': '出现新增失败',
    'existing_failures_allowed': '策略允许原有失败', 'existing_failures': '仍有原有失败',
    'blocked_or_incomplete': '所选范围包含受阻项或执行未完成',
}
_REPLAY = {
    'verified': '所选结果全部复现', 'different': '所选结果有变化', 'incomplete': '重放记录不足',
    'inputs_changed': '固定输入不同', 'same_run': '两份证据来自同一次运行',
}
_STABILITY = {
    'verified': '每轮均复现', 'different': '轮间变化或持续偏离', 'incomplete': '记录不足',
    'cancelled': '稳定性检查已取消',
}


def acceptance_headline(summary):
    return '无法完成验收' if summary is None else '验收' + _STATUS[summary['gate']['status']]


def _tree(name, headers):
    tree = QTreeWidget()
    tree.setObjectName(name)
    tree.setRootIsDecorated(False)
    tree.setAlternatingRowColors(True)
    tree.setUniformRowHeights(True)
    tree.setHeaderLabels(headers)
    tree.header().setStretchLastSection(False)
    tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
    return tree


def _case_title(file, name):
    if file is not None:
        return name, f'{file} · {name}'
    _, separator, case = name.partition('::')
    return (case if separator else name), name


def _targets(report):
    if report.get('runner') == 'vitest':
        for entry in report['entries']:
            for case in entry.get('case_entries', ()):
                yield entry['test_id'], case
    else:
        for entry in report['entries']:
            yield None, entry


class AcceptanceOptionsDialog(QDialog):
    def __init__(self, report, parent, *, options=None):
        super().__init__(parent)
        self.setObjectName('projectAcceptanceOptions')
        self.setWindowTitle('修复验收')
        self.directory = Path(report['report_dir']).parent
        options = options or {}
        self.runner = report.get('runner', 'pytest')
        self.current_baseline = baseline_pin(report)
        self.locked_baseline = None
        layout = QVBoxLayout(self)
        report_id = QLabel(f'当前报告 · {report["run_id"]}')
        report_id.setTextFormat(Qt.TextFormat.PlainText)
        report_id.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(report_id)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(form)
        policy = self.policy = QComboBox()
        policy.setObjectName('acceptancePolicy')
        for key, label in _POLICIES.items():
            policy.addItem(label, key)
        policy.setToolTip('严格策略要求所选范围通过。允许原有失败仍会拦截新增失败和不完整执行。')
        form.addRow('回归门槛', policy)
        self.pin_baseline = QCheckBox('锁定基线快照')
        self.pin_baseline.setObjectName('acceptancePinBaseline')
        self.pin_baseline.setToolTip('保存并核对基线源码及普通文件权限的指纹。')
        self.pin_baseline.toggled.connect(self._baseline_toggled)
        form.addRow(self.pin_baseline)
        self.baseline_notice = QLabel()
        self.baseline_notice.setObjectName('acceptanceBaselineNotice')
        self.baseline_notice.setTextFormat(Qt.TextFormat.PlainText)
        self.baseline_notice.setWordWrap(True)
        form.addRow(self.baseline_notice)

        search = QLineEdit()
        search.setObjectName('acceptanceSearch')
        search.setAccessibleName('搜索验收目标')
        search.setPlaceholderText('搜索测试名或路径')
        search.setClearButtonEnabled(True)
        self.search = search
        self.selection = QLabel()
        self.selection.setObjectName('acceptanceSelection')
        self.selection.setWordWrap(True)
        layout.addWidget(self.selection)
        layout.addWidget(search)
        tree = self.targets = _tree('acceptanceTargets', ['测试', '报告中的结果'])
        tree.setAccessibleName('需从失败变为通过的测试')
        layout.addWidget(tree, 1)

        self.available = {(file, entry['test_id']): entry['status'] for file, entry in _targets(report)}
        self.items = {}
        self.missing = set()
        tree.itemChanged.connect(self._filter)
        search.textChanged.connect(self._filter)

        materials = QFormLayout()
        materials.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(materials)
        self.require_receipt = QCheckBox('要求重放回执')
        self.require_receipt.setObjectName('acceptanceRequireReceipt')
        self.require_stability = QCheckBox('要求多轮复现')
        self.require_stability.setObjectName('acceptanceRequireStability')
        self.receipt = self._path_row(materials, self.require_receipt, 'acceptanceReceipt', '选择本次回传目录',
                                      self._pick_receipt)
        self.stability = self._path_row(materials, self.require_stability, 'acceptanceStability',
                                        '选择本次稳定性摘要', self._pick_stability)
        self.minimum = QSpinBox()
        self.minimum.setObjectName('acceptanceMinRounds')
        self.minimum.setRange(MIN_ROUNDS, MAX_ROUNDS)
        self.receipt.setToolTip('选择包含 replay-evidence.zip、replay-check.json 和 replay-receipt.md 的目录。')
        self.stability.setToolTip('选择 replay-stability.json，旁边需保留各轮报告目录 reports。')
        self.require_stability.toggled.connect(self.minimum.setEnabled)
        materials.addRow('最少复现轮数', self.minimum)

        self.require_approval = QCheckBox('要求签名审查')
        self.require_approval.setObjectName('acceptanceRequireApproval')
        approval = QWidget()
        approval_form = QFormLayout(approval)
        approval_form.setContentsMargins(0, 0, 0, 0)
        self.approval_document, self.approval_key, self.approval_reviewer = (QLineEdit() for _ in range(3))
        for label, field, name in (
            ('签名记录', self.approval_document, 'acceptanceApprovalDocument'),
            ('可信公钥', self.approval_key, 'acceptanceApprovalKey'),
            ('预期审查者', self.approval_reviewer, 'acceptanceApprovalReviewer'),
        ):
            field.setObjectName(name)
            approval_form.addRow(label, field)
        materials.addRow(self.require_approval, approval)
        self.require_approval.toggled.connect(approval.setVisible)
        approval.setVisible(False)
        previous_approval = options.get('approval')
        if previous_approval and all(previous_approval):
            for field, value in zip((self.approval_document, self.approval_key, self.approval_reviewer), previous_approval):
                field.setText(str(value))
            self.require_approval.setChecked(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        submit = buttons.button(QDialogButtonBox.StandardButton.Ok)
        submit.setObjectName('acceptanceStart')
        submit.setText('核对验收')
        submit.setDefault(True)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self._submit)
        buttons.rejected.connect(self.reject)
        load = buttons.addButton('打开计划…', QDialogButtonBox.ButtonRole.ActionRole)
        load.setObjectName('acceptanceOpenPlan')
        load.clicked.connect(self._open_plan)
        save = buttons.addButton('保存计划…', QDialogButtonBox.ButtonRole.ActionRole)
        save.setObjectName('acceptanceSavePlan')
        save.clicked.connect(self._save_plan)
        self.plan_feedback = QLabel()
        self.plan_feedback.setObjectName('acceptancePlanFeedback')
        self.plan_feedback.setWordWrap(True)
        self.plan_feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.plan_feedback.hide()
        layout.addWidget(self.plan_feedback)
        layout.addWidget(buttons)

        plan = options.get('plan') or AcceptancePlan(self.runner, 'strict', ())
        self.receipt.setText(str(options['receipt']) if options.get('receipt') else '')
        self.stability.setText(str(options['stability']) if options.get('stability') else '')
        self._apply_plan(plan)
        (search if self.items else policy).setFocus()
        self.resize(740, 550 if self.items else 290)

    def _apply_plan(self, plan):
        if self.runner != plan.runner:
            raise ValueError(f'计划使用 {plan.runner}，当前报告使用 {self.runner}。')
        self.policy.setCurrentIndex(self.policy.findData(plan.policy))
        self.locked_baseline = plan.baseline
        self.approved_selection = plan.approved_selection
        self.protected_inputs = plan.protected_inputs
        self.test_contract = plan.test_contract
        self.required_passes = plan.required_passes
        self.pin_baseline.blockSignals(True)
        self.pin_baseline.setChecked(plan.baseline is not None)
        self.pin_baseline.blockSignals(False)
        self._sync_baseline()
        self.require_receipt.setChecked(plan.require_receipt)
        self.require_stability.setChecked(plan.min_stability_rounds is not None)
        self.minimum.setEnabled(plan.min_stability_rounds is not None)
        self.minimum.setValue(plan.min_stability_rounds or MIN_ROUNDS)

        persistent = set(plan.required_passes if self.runner == 'vitest'
                         else ((None, name) for name in plan.required_passes))
        checked = plan.targets if self.runner == 'vitest' else [(None, name) for name in plan.targets]
        # Keep missing goals visible and checked, in the same order as the plan.
        # An unmatched target still reaches the evidence checker.
        order = dict.fromkeys([*checked, *self.available])
        checked = set(checked)
        self.missing = checked - self.available.keys()
        tree = self.targets
        tree.blockSignals(True)
        tree.clear()
        self.items = {}
        for file, name in order:
            title, identity = _case_title(file, name)
            outcome = self.available.get((file, name))
            item = QTreeWidgetItem([title, _OUTCOMES[outcome]])
            item.setToolTip(0, identity)
            if (file, name) in persistent:
                item.setText(1, _OUTCOMES[outcome] + ' · 持续通过')
            else:
                item.setCheckState(0, Qt.CheckState.Checked if (file, name) in checked else Qt.CheckState.Unchecked)
            self.items[file, name] = item
        tree.addTopLevelItems(list(self.items.values()))
        tree.blockSignals(False)
        tree.setVisible(bool(self.items))
        self.search.setVisible(bool(self.items))
        self._filter()

    def _baseline_toggled(self, checked):
        self.locked_baseline = self.current_baseline if checked else None
        self._sync_baseline()

    def _sync_baseline(self):
        pinned = self.locked_baseline
        self.pin_baseline.setEnabled(self.current_baseline is not None or pinned is not None)
        if self.current_baseline is None:
            message = '当前报告缺少完整基线指纹。'
        elif pinned is not None and pinned != self.current_baseline:
            message = '当前报告的基线与计划不同，验收会受阻。'
        elif pinned is not None:
            message = f'已锁定当前基线 · {pinned.source_sha256[:12]}'
        else:
            message = ''
        self.baseline_notice.setText(message)
        self.baseline_notice.setVisible(bool(message))

    def _filter(self, *_):
        needle = self.search.text().strip().casefold()
        selected = hidden = matches = missing = 0
        self.targets.setUpdatesEnabled(False)
        for target, item in self.items.items():
            visible = needle in item.toolTip(0).casefold()
            matches += visible
            item.setHidden(not visible)
            if item.checkState(0) == Qt.CheckState.Checked:
                selected += 1
                hidden += not visible
                missing += target in self.missing
        self.targets.setUpdatesEnabled(True)
        if not self.items:
            text = '报告无可勾选的用例级结果，可核对回归门槛和已有材料。'
        else:
            text = f'必须从失败变为通过的测试 · 已选 {selected} 项'
            if hidden:
                text += f' · {hidden} 项被搜索隐藏，仍参与验收'
            if not selected:
                text += ' · 可选'
            if missing:
                text += f' · {missing} 项缺少报告结果，将按受阻核对'
            if not matches:
                text += ' · 无匹配测试'
        if self.required_passes:
            text += f' · 持续通过 {len(self.required_passes)} 项'
        if self.approved_selection is not None:
            text += f' · 已批准范围 {len(self.approved_selection)} 项'
        if self.protected_inputs:
            text += f' · 固定失败样本 {len(self.protected_inputs["values"])} 项'
        self.selection.setText(text)

    def _path_row(self, form, requirement, name, placeholder, browse):
        edit = QLineEdit()
        edit.setObjectName(name)
        edit.setAccessibleName(placeholder)
        edit.setPlaceholderText(placeholder)
        edit.setClearButtonEnabled(True)
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        button = QPushButton('浏览…')
        button.setAutoDefault(False)
        button.setAccessibleName(placeholder)
        button.clicked.connect(browse)
        layout.addWidget(button)
        form.addRow(requirement, row)
        row.setEnabled(requirement.isChecked())
        requirement.toggled.connect(row.setEnabled)
        return edit

    def _pick_receipt(self):
        chosen = QFileDialog.getExistingDirectory(self, '选择回传回执目录', self.receipt.text() or str(self.directory))
        if chosen:
            self.receipt.setText(chosen)

    def _pick_stability(self):
        chosen, _ = QFileDialog.getOpenFileName(self, '选择稳定性摘要', self.stability.text() or str(self.directory),
                                                f'稳定性摘要 ({STABILITY_SUMMARY})')
        if chosen:
            self.stability.setText(chosen)

    def _plan(self):
        targets = [key for key, item in self.items.items() if item.checkState(0) == Qt.CheckState.Checked]
        if self.runner != 'vitest':
            targets = [name for _, name in targets]
        return AcceptancePlan(self.runner, self.policy.currentData(), tuple(targets),
                              self.require_receipt.isChecked(),
                              self.minimum.value() if self.require_stability.isChecked() else None,
                              self.locked_baseline, self.required_passes, self.approved_selection, self.protected_inputs,
                              self.test_contract)

    def _submit(self):
        try:
            self.options()
        except ValueError as exc:
            self._show_plan_feedback(str(exc))
        else:
            self.accept()

    def _show_plan_feedback(self, message):
        self.plan_feedback.setText(message)
        self.plan_feedback.show()

    def _open_plan(self):
        path, _ = QFileDialog.getOpenFileName(self, '打开验收计划', str(self.directory), '验收计划 (*.json)')
        if not path:
            return
        try:
            plan = read_acceptance_plan(path)
            self._apply_plan(plan)
        except (OSError, ValueError) as exc:
            self._show_plan_feedback('无法打开计划 · ' + str(exc))
        else:
            self.search.clear()
            self._show_plan_feedback('已打开计划 · ' + Path(path).name)

    def _save_plan(self):
        path, _ = QFileDialog.getSaveFileName(self, '保存验收计划', str(self.directory / 'acceptance-plan.json'),
                                              '验收计划 (*.json)', options=QFileDialog.Option.DontConfirmOverwrite)
        if not path:
            return
        target = Path(path)
        if not target.suffix:
            target = target.with_suffix('.json')
        try:
            save_acceptance_plan(self._plan(), target)
        except (OSError, ValueError) as exc:
            self._show_plan_feedback('无法保存计划 · ' + str(exc))
        else:
            self._show_plan_feedback('已保存计划 · ' + target.name)

    def options(self):
        plan = self._plan()
        receipt = self.receipt.text().strip() if plan.require_receipt else ''
        stability = self.stability.text().strip() if plan.min_stability_rounds is not None else ''
        result = {'plan': plan, 'receipt': Path(receipt) if receipt else None,
                  'stability': Path(stability) if stability else None}
        if self.require_approval.isChecked():
            document, key, actor = [field.text().strip() for field in (self.approval_document, self.approval_key,
                                                                    self.approval_reviewer)]
            if not all((document, key, actor)):
                raise ValueError('请提供签名记录、可信公钥和预期审查者。')
            result['approval'] = (Path(document), Path(key), actor)
        return result


def _receipt_details(receipt):
    if receipt['receipt_status'] == 'missing':
        return '请提供本次回传回执目录'
    if receipt['receipt_status'] == 'unavailable':
        return '无法读取或核对重放回执'
    details = []
    if receipt['receipt_status'] == 'inconsistent':
        parts = ['证据 ZIP' if item['part'] == 'evidence' else item['part'] for item in receipt['issues']]
        details.append('材料不一致 · ' + '、'.join(parts))
    if verification := receipt.get('verification_status'):
        details.append(_REPLAY[verification])
    if counts := receipt.get('counts'):
        details.append(f'一致 {counts["matched"]} 项 · 变化 {counts["different"]} 项 · 未能核对 {counts["incomplete"]} 项')
    conditions = receipt.get('conditions', {})
    for value, label in ((False, '条件有差异'), (None, '条件未知')):
        names = [name for key, name in _CONDITION_LABELS.items() if key in conditions and conditions[key] is value]
        if names:
            details.append(label + ' · ' + '、'.join(names))
    return ' · '.join(details)


def _result_rows(summary):
    comparison = summary.get('comparison_gate', summary['gate'])
    title = '回归门槛 · ' + _POLICIES[comparison['policy']]
    scope = summary.get('comparison_scope')
    details = _COMPARISON.get(comparison['reason'], '')
    if scope:
        details = ' · '.join(filter(None, (details, comparison_scope_message(scope))))
    yield title, title, comparison['status'], details
    if baseline := summary.get('baseline_check'):
        details = {'matched': '基线源码及文件权限一致', 'different': '基线与计划锁定的快照不同',
                   'missing': '报告缺少可核对的基线指纹'}[baseline['reason']]
        if baseline['reason'] == 'different':
            for field, label in (('source_sha256', '源码'), ('source_mode_sha256', '普通文件权限')):
                if baseline['expected'][field] != baseline['actual'][field]:
                    details += f' · {label}指纹不同'
        yield '锁定基线', '锁定基线', baseline['status'], details
    if approved := summary.get('approved_scope'):
        details = (f'已批准 {approved["approved_count"]} 项 · 本次 {approved["selected_count"]} 项'
                   f' · 缺少 {len(approved["missing_tests"])} 项 · 新增 {len(approved["added_tests"])} 项')
        if approved['missing_tests']:
            details += ' · ' + '、'.join(approved['missing_tests'][:5])
        yield '已批准回归范围', '已批准回归范围', approved['status'], details
    for target in summary.get('required_passes', ()):
        title, identity = _case_title(target.get('file'), target['test_id'])
        details = {'passed': '候选通过', 'failed': '候选失败'}.get(target['outcome'], '候选缺少可核对结果')
        yield '持续通过 · ' + title, identity, target['status'], details
    if protected := summary.get('protected_inputs'):
        details = '已核对计划指纹' if protected['plan_bound'] else '报告缺少匹配的计划指纹'
        yield '固定失败样本', '固定失败样本', protected['status'], details
        for target in protected['entries']:
            title, identity = _case_title(None, target['test_id'])
            details = '候选已使用样本' if target['inputs_consumed']['after'] else '候选缺少匹配的样本记录'
            yield '固定样本 · ' + title, identity, target['status'], details
    if approval := summary.get('review_approval'):
        reason = {'approved': '已确认签名批准', 'reviewer_rejected': '签名判定为拒绝',
                  'approval_unavailable': '签名材料无法读取', 'approval_unverified': '签名绑定不匹配'}[approval['reason']]
        yield '签名审查', '签名审查', approval['status'], reason
    if contract := summary.get('test_contract'):
        changes = '、'.join(item['path'] for item in contract['changes'][:5])
        detail = f'{contract["file_count"]} 个锁定文件' + (' · 需核对 ' + changes if changes else '')
        yield '测试契约', '测试契约', contract['status'], detail
    if receipt := summary.get('receipt'):
        yield '重放回执', '重放回执', receipt['status'], _receipt_details(receipt)
    if stability := summary.get('stability'):
        details = [f'要求至少 {stability["minimum_rounds"]} 轮']
        verification = stability['verification_status']
        if verification == 'missing':
            details.append('请选择本次稳定性摘要')
        elif verification == 'unavailable':
            details.append('无法读取或核对稳定性证据')
        else:
            details += [f'{stability["completed_rounds"]}/{stability["requested_rounds"]} 轮已核对',
                        _STABILITY[stability['verification_status']]]
            if unverified := stability['unverified_rounds']:
                details.append('未能核对轮次 ' + '、'.join(map(str, unverified)))
        yield '多轮稳定性', '多轮稳定性', stability['status'], ' · '.join(details)
    for target in summary.get('required_improvements', ()):
        title, identity = _case_title(target.get('file'), target['test_id'])
        details = _OUTCOMES[target['outcome']]
        if target.get('reason') == 'same_snapshot_divergence':
            details += ' · 同快照结果分歧'
        elif target.get('reason') == 'excluded_changes_only':
            details += ' · 本次改动未纳入快照'
        elif target.get('reason') == 'pytest_import_origin':
            details += ' · 实际导入来源需核对'
        elif target.get('reason') in ('pytest_input_control', 'pytest_input_changed'):
            details += ' · 业务输入需核对'
        elif target.get('reason') in ('pytest_fixture_control', 'pytest_fixture_changed'):
            details += ' · 夹具输入需核对'
        elif target.get('reason') == 'pytest_version_order':
            details += ' · 版本先后对照观察'
        elif target.get('reason') == 'pytest_order_control':
            details += ' · 固定顺序下的对照观察'
        yield title, identity, target['status'], details


class AcceptanceResultDialog(QDialog):
    def __init__(self, summary, parent, *, error=None, ci_suggested_path=None, export_error=None):
        super().__init__(parent)
        self.setObjectName('projectAcceptanceResult')
        self.setWindowTitle('修复验收结果')
        self.ci_destination = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(12)

        headline = QLabel(acceptance_headline(summary))
        headline.setObjectName('acceptanceHeadline')
        if summary is None:
            headline.setStyleSheet('font-size: 20px; font-weight: 600;')
            layout.addWidget(headline)
        else:
            color = _STATUS_COLOR[summary['gate']['status']]
            headline.setStyleSheet(f'color: {color}; font-size: 22px; font-weight: 650;')
            checks = list(_result_rows(summary))
            layout.addWidget(self._overview(headline, summary, checks))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText('关闭')
        buttons.rejected.connect(self.reject)
        adjust = buttons.addButton('调整条件…', QDialogButtonBox.ButtonRole.AcceptRole)
        adjust.setObjectName('acceptanceAdjust')
        buttons.accepted.connect(self.accept)
        if summary is None:
            details = QPlainTextEdit(error)
            details.setObjectName('acceptanceError')
            details.setAccessibleName('验收错误详情')
            details.setReadOnly(True)
            layout.addWidget(details, 1)
        else:
            rows = _tree('acceptanceChecks', ['要求', '状态', '核对详情'])
            rows.setAccessibleName('验收要求及核对结果')
            rows.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
            rows.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            rows.setColumnWidth(0, 350)
            first_attention = None
            for title, identity, status, details in checks:
                item = QTreeWidgetItem([title, _STATUS[status], details])
                item.setToolTip(0, identity)
                item.setToolTip(2, details)
                item.setData(1, Qt.ItemDataRole.UserRole, status)
                rows.addTopLevelItem(item)
                if first_attention is None and status != 'passed':
                    first_attention = item
            self._add_result_filters(layout, rows)
            layout.addWidget(rows, 1)
            current = QLabel()
            current.setObjectName('acceptanceDetail')
            current.setWordWrap(True)
            current.setTextFormat(Qt.TextFormat.PlainText)
            current.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            current.setMinimumHeight(current.fontMetrics().height() * 3)
            rows.currentItemChanged.connect(lambda item, _: current.setText(
                item.toolTip(0) + '\n' + item.text(2) if item else ''))
            rows.setCurrentItem(first_attention or rows.topLevelItem(0))
            layout.addWidget(current)
            copy = buttons.addButton('复制 PR 摘要', QDialogButtonBox.ButtonRole.ActionRole)
            copy.setObjectName('acceptanceCopy')

            def copy_summary():
                QApplication.clipboard().setText(render_review_markdown(summary))
                copy.setText('已复制')

            copy.clicked.connect(copy_summary)
            export = buttons.addButton('导出 CI 配置…', QDialogButtonBox.ButtonRole.ActionRole)
            export.setObjectName('acceptanceExportCI')
            export.setToolTip('保存报告所选范围和验收计划，用于 CI 的下一次检查。')
            export.clicked.connect(lambda: self._choose_ci_destination(ci_suggested_path))
        if export_error:
            feedback = QLabel('无法导出 CI 配置 · ' + export_error)
            feedback.setObjectName('acceptanceExportError')
            feedback.setTextFormat(Qt.TextFormat.PlainText)
            feedback.setWordWrap(True)
            feedback.setStyleSheet(f'color: {_STATUS_COLOR["failed"]};')
            layout.addWidget(feedback)
        layout.addWidget(buttons)
        self.resize(800, 510 if summary is not None else 290)

    def _add_result_filters(self, layout, rows):
        bar = QHBoxLayout()
        layout.addLayout(bar)
        search = QLineEdit()
        search.setObjectName('acceptanceResultSearch')
        search.setAccessibleName('搜索验收结果')
        search.setPlaceholderText('搜索要求、完整测试名或详情')
        search.setClearButtonEnabled(True)
        bar.addWidget(search, 1)
        find = QShortcut(QKeySequence.StandardKey.Find, self)
        find.activated.connect(search.setFocus)
        status_filter = QComboBox()
        status_filter.setObjectName('acceptanceResultFilter')
        status_filter.setAccessibleName('按验收状态筛选')
        for value, text in (('all', '全部状态'), ('attention', '需要处理'),
                            ('failed', '仅失败'), ('blocked', '仅受阻'), ('passed', '仅通过')):
            status_filter.addItem(text, value)
        bar.addWidget(status_filter)
        count = QLabel()
        count.setObjectName('acceptanceResultCount')
        bar.addWidget(count)
        items = [rows.topLevelItem(index) for index in range(rows.topLevelItemCount())]

        def update_results():
            query = search.text().strip().casefold()
            mode = status_filter.currentData()
            first = None
            visible = 0
            rows.setUpdatesEnabled(False)
            for item in items:
                status = item.data(1, Qt.ItemDataRole.UserRole)
                allowed = mode in ('all', status) or (mode == 'attention' and status != 'passed')
                matches = allowed and (not query or any(
                    query in field.casefold() for field in (item.text(0), item.toolTip(0), item.text(2))
                ))
                item.setHidden(not matches)
                if matches:
                    visible += 1
                    if first is None:
                        first = item
            current = rows.currentItem()
            if current is None or current.isHidden():
                rows.setCurrentItem(first)
                if first is not None:
                    rows.scrollToItem(first)
            rows.setUpdatesEnabled(True)
            count.setText(f'{visible}/{len(items)} 项' if visible else '无匹配项')

        search.textChanged.connect(update_results)
        status_filter.currentIndexChanged.connect(update_results)
        count.setText(f'{len(items)}/{len(items)} 项')

    @staticmethod
    def _overview(headline, summary, checks):
        frame = QFrame()
        frame.setObjectName('detailPanel')
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 12, 16, 12)
        overview = QVBoxLayout()
        overview.addWidget(headline)
        runner = {'pytest': 'pytest', 'go': 'Go', 'vitest': 'Vitest'}[summary['runner']]
        unit = '个测试文件' if summary['runner'] == 'vitest' else '项测试'
        scope = QLabel(f'{runner} · 所选范围 {summary["selected_count"]} {unit}')
        scope.setObjectName('acceptanceScope')
        overview.addWidget(scope)
        layout.addLayout(overview, 1)

        totals = Counter(row[2] for row in checks)
        requirements = QVBoxLayout()
        requirements.addWidget(QLabel(f'验收条件 · {len(checks)} 项'))
        counts = QLabel('  ·  '.join(f'{label} {totals[status]}' for status, label in _STATUS.items()))
        counts.setObjectName('acceptanceCheckCounts')
        requirements.addWidget(counts)
        layout.addLayout(requirements)
        return frame

    def _choose_ci_destination(self, suggested):
        path, _ = QFileDialog.getSaveFileName(
            self, '导出 CI 配置', str(suggested or 'reprolab-ci-config.zip'),
            'CI 配置 (*.zip)', options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if path:
            self.ci_destination = Path(path if path.lower().endswith('.zip') else path + '.zip')
            self.accept()
