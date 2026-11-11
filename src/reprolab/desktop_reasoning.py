"""Compact per-model effort editor shared by connection and saved-model controls."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton
from .model_reasoning import ADAPTER_LABELS, EFFORT_LABELS, allowed_adapters, reasoning_choices


class ReasoningEditor(QWidget):
    changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False
        self._context = ('', 'openai', '')
        self._definition = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel('思考强度'))
        self.effort_combo = QComboBox()
        row.addWidget(self.effort_combo, 1)
        self.settings_button = QPushButton('兼容设置')
        self.settings_button.setCheckable(True)
        row.addWidget(self.settings_button)
        layout.addLayout(row)
        self.adapter_combo = QComboBox()
        self.adapter_combo.hide()
        layout.addWidget(self.adapter_combo)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setTextFormat(Qt.TextFormat.PlainText)
        self.hint.hide()
        layout.addWidget(self.hint)
        self.settings_button.toggled.connect(self.adapter_combo.setVisible)
        self.settings_button.toggled.connect(self.hint.setVisible)
        self.adapter_combo.currentIndexChanged.connect(self._adapter_changed)
        self.effort_combo.currentIndexChanged.connect(self._changed)
        self.set_context('', 'openai', '')

    def set_context(self, url, protocol, model, option=None, definition=None):
        self._loading = True
        self._context = (url, protocol, model)
        self._definition = definition
        option = option or {'adapter': 'auto', 'effort': 'default'}
        self.adapter_combo.clear()
        for adapter in allowed_adapters(protocol):
            self.adapter_combo.addItem(ADAPTER_LABELS[adapter], adapter)
        if definition is not None and 'reasoning_levels' in definition:
            self.adapter_combo.addItem('模型档位映射', 'catalog')
            option = dict(option, adapter='catalog')
        self.adapter_combo.setCurrentIndex(self.adapter_combo.findData(option['adapter']))
        self.settings_button.setVisible(definition is None)
        if definition is not None:
            self.adapter_combo.hide()
            self.hint.hide()
        self._fill_efforts(option['effort'])
        self._loading = False

    def _fill_efforts(self, selected='default'):
        if self._definition is not None:
            levels = tuple(self._definition.get('reasoning_levels', {}))
            hint = '档位来自模型卡片；在连接设置中编辑模型即可调整。'
        else:
            levels, hint = reasoning_choices(*self._context, self.adapter_combo.currentData())
        self.effort_combo.blockSignals(True)
        self.effort_combo.clear()
        for effort in ('default', *levels):
            self.effort_combo.addItem(EFFORT_LABELS[effort], effort)
        index = self.effort_combo.findData(selected)
        if index < 0:
            self.effort_combo.addItem('需重新选择：' + selected, selected)
            index = self.effort_combo.count() - 1
        self.effort_combo.setCurrentIndex(index)
        self.effort_combo.blockSignals(False)
        self.hint.setText(hint)
        self.hint.setVisible(self._definition is None and bool(self._context[2]) and (not levels or self.settings_button.isChecked()))
        self.effort_combo.setToolTip(hint + ' 强度不等于输出 Token 上限；较高强度可能增加耗时与费用。')
        self.settings_button.setToolTip(hint)

    def _adapter_changed(self):
        if self._loading:
            return
        self._fill_efforts()
        self._changed()

    def _changed(self):
        if not self._loading:
            self.changed.emit(self.value())

    def value(self):
        return {'adapter': self.adapter_combo.currentData(), 'effort': self.effort_combo.currentData()}
