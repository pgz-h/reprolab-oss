"""Qt model cards following DeepSeek Harness's catalog/level editor (MIT).

Copyright (c) 2026 DeepSeek. Adaptation: ReproLab contributors.
See packaging/licenses/deepseek-harness/LICENSE.
"""
from copy import deepcopy
from uuid import uuid4
from pathlib import Path
from PySide6.QtCore import Qt, Signal, QMimeData, QPoint
from PySide6.QtGui import QDrag, QIcon, QPainter, QColor
from PySide6.QtWidgets import (QWidget, QFrame, QLabel, QLineEdit, QComboBox, QCheckBox,
    QPushButton, QToolButton, QVBoxLayout, QHBoxLayout, QGridLayout, QStyle, QSizePolicy, QLayout)
from .model_definitions import (LEVELS, default_level_value, changed_level_value, next_level,
                                 parse_capacity, format_capacity, validate_model)


def icon_button(widget, icon, text):
    b = QToolButton(widget)
    names={QStyle.StandardPixmap.SP_TrashIcon:'trash',QStyle.StandardPixmap.SP_ArrowDown:'chevron-down',QStyle.StandardPixmap.SP_ArrowUp:'chevron-up'}
    b.setIcon(QIcon(str(Path(__file__).parent/'ui_assets'/('model-'+names[icon]+'.svg'))))
    b.setToolTip(text);b.setAccessibleName(text)
    b.setAutoRaise(True)
    b.setFixedSize(30, 30)
    return b


class DragHandle(QToolButton):
    move = Signal(int)
    def __init__(self, identity, parent):
        super().__init__(parent)
        self.identity=identity;self.start=QPoint()
        self.setIcon(QIcon(str(Path(__file__).parent/'ui_assets/model-bars-3.svg')))
        self.setAutoRaise(True);self.setFixedSize(28,32)
        self.setToolTip('拖动排序，或聚焦后按上 / 下键')
        self.setAccessibleName('调整模型顺序')
    def mousePressEvent(self,event):
        self.start=event.position().toPoint();super().mousePressEvent(event)
    def mouseMoveEvent(self,event):
        if event.buttons() & Qt.MouseButton.LeftButton and (event.position().toPoint()-self.start).manhattanLength()>8:
            drag=QDrag(self);data=QMimeData();data.setData('application/x-reprolab-model',self.identity.encode())
            drag.setMimeData(data);drag.exec(Qt.DropAction.MoveAction)
        else:super().mouseMoveEvent(event)
    def keyPressEvent(self,event):
        if event.key() in (Qt.Key.Key_Up,Qt.Key.Key_Down):
            self.move.emit(-1 if event.key()==Qt.Key.Key_Up else 1);event.accept()
        else:super().keyPressEvent(event)


class Toggle(QCheckBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(38,22);self.setAccessibleName('启用推理等级')
        self.setCursor(Qt.CursorShape.PointingHandCursor)
    def hitButton(self, point):return self.rect().contains(point)
    def paintEvent(self, event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#202226' if self.isChecked() else '#b9bec7'))
        painter.drawRoundedRect(1,2,36,18,9,9)
        painter.setBrush(QColor('white'));painter.drawEllipse(20 if self.isChecked() else 3,4,14,14)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush);painter.setPen(QColor('#6c91c2'));painter.drawRoundedRect(0,0,37,21,10,10)


class LevelRow(QWidget):
    changed=Signal();removed=Signal(object)
    def __init__(self, level, wire, parent=None):
        super().__init__(parent);self.previous=level;self.loading=False
        self.setFixedHeight(40)
        line=QHBoxLayout(self);line.setContentsMargins(0,0,0,0);line.setSpacing(12)
        self.level=QComboBox();self.wire=QLineEdit()
        self.level.setMinimumHeight(34);self.wire.setMinimumHeight(34)
        for key in LEVELS:self.level.addItem(key,key)
        self.level.setCurrentIndex(self.level.findData(level))
        self.wire.setText(wire if isinstance(wire,str) else '')
        self.wire.setPlaceholderText('关闭思考' if level=='off' else level)
        self.wire.setEnabled(level!='off')
        self.remove=icon_button(self,QStyle.StandardPixmap.SP_TrashIcon,'移除档位')
        line.addWidget(self.level,1);line.addWidget(self.wire,1);line.addWidget(self.remove)
        self.level.currentIndexChanged.connect(self._level_changed)
        self.wire.textChanged.connect(self.changed)
        self.remove.clicked.connect(lambda:self.removed.emit(self))
    def _level_changed(self):
        new=self.level.currentData()
        value=changed_level_value(self.previous,self.wire.text(),new)
        self.wire.blockSignals(True);self.wire.setText(value or '');self.wire.setEnabled(new!='off')
        self.wire.setPlaceholderText('关闭思考' if new=='off' else new);self.wire.blockSignals(False)
        self.previous=new;self.changed.emit()
    def value(self):return self.level.currentData(),None if self.level.currentData()=='off' else self.wire.text().strip()


class ModelCard(QFrame):
    changed=Signal();removed=Signal(object);reordered=Signal(str,int);dropped=Signal(str,str,bool)
    def __init__(self, model, *, expanded=False, parent=None):
        super().__init__(parent)
        self.identity=uuid4().hex;self.original=deepcopy(model);self.rows=[];self.loading=True
        self.setObjectName('modelCard');self.setAcceptDrops(True)
        self.setStyleSheet('QFrame#modelCard { border:1px solid #d9dce2; border-radius:14px; background:#f6f7f9; } '
                          'QLabel#modelField { color:#858994; }')
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Maximum)
        outer=QVBoxLayout(self);outer.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize);outer.setContentsMargins(16,12,16,16);outer.setSpacing(14)
        head=QHBoxLayout();head.setSpacing(8)
        self.handle=DragHandle(self.identity,self)
        self.id_edit=QLineEdit(model.get('id',''));self.id_edit.setPlaceholderText('模型 ID')
        self.name_edit=QLineEdit(model.get('name',''));self.name_edit.setPlaceholderText('显示名称')
        self.expand_button=icon_button(self,QStyle.StandardPixmap.SP_ArrowDown,'展开模型设置')
        self.expand_button.setCheckable(True)
        self.remove_button=icon_button(self,QStyle.StandardPixmap.SP_TrashIcon,'删除模型')
        head.addWidget(self.handle);head.addWidget(self.id_edit,3);head.addWidget(self.name_edit,2)
        head.addWidget(self.expand_button);head.addWidget(self.remove_button);outer.addLayout(head)
        self.details=QWidget();detail=QVBoxLayout(self.details);detail.setContentsMargins(0,0,0,0);detail.setSpacing(14)
        grid=QGridLayout();grid.setHorizontalSpacing(14);grid.setVerticalSpacing(7)
        for col,title in enumerate(('上下文窗口','最大输出 token 数')):
            label=QLabel(title);label.setObjectName('modelField');grid.addWidget(label,0,col)
        self.context_edit=QLineEdit(format_capacity(model.get('context_window')));self.context_edit.setPlaceholderText('例如 1M（可留空）')
        self.output_edit=QLineEdit(format_capacity(model.get('max_tokens')));self.output_edit.setPlaceholderText('例如 128K（可留空）')
        self.context_edit.setToolTip('记录模型声明的上下文容量；不把字节数当作精确 token 数。')
        self.output_edit.setToolTip('填写后用于该模型请求的最大输出限制；服务仍可能有自己的上限。')
        grid.addWidget(self.context_edit,1,0);grid.addWidget(self.output_edit,1,1);detail.addLayout(grid)
        inp=QLabel('输入类型');inp.setObjectName('modelField');detail.addWidget(inp)
        inputs=QHBoxLayout();self.text_check=QCheckBox('文本');self.image_check=QCheckBox('图片')
        self.text_check.setChecked('text' in model.get('input',['text']));self.image_check.setChecked('image' in model.get('input',[]))
        self.image_check.setToolTip('记录模型能力；ReproLab 当前检查工作流只发送文本材料。')
        inputs.addWidget(self.text_check);inputs.addWidget(self.image_check);inputs.addStretch();detail.addLayout(inputs)
        reasoning=QHBoxLayout();label=QLabel('推理等级');label.setObjectName('modelField');reasoning.addWidget(label)
        reasoning.addStretch();self.reasoning_check=Toggle(self);self.reasoning_check.setChecked('reasoning_levels' in model)
        self.reasoning_check.setToolTip('为这个模型声明可选择的档位，以及每个档位实际发送的 API 值。')
        reasoning.addWidget(self.reasoning_check);detail.addLayout(reasoning)
        self.levels_widget=QWidget();levels=QVBoxLayout(self.levels_widget);levels.setContentsMargins(0,0,0,0);levels.setSpacing(8)
        labels=QHBoxLayout();labels.addWidget(QLabel('档位'),1);labels.addWidget(QLabel('API 参数值'),1);labels.addSpacing(30);levels.addLayout(labels)
        self.rows_widget=QWidget();self.rows_layout=QVBoxLayout(self.rows_widget);self.rows_layout.setContentsMargins(0,0,0,0);self.rows_layout.setSpacing(7);levels.addWidget(self.rows_widget)
        for level,wire in model.get('reasoning_levels',{}).items():self._append_level(level,wire)
        self.add_level_button=QPushButton('添加档位');self.add_level_button.clicked.connect(self.add_level)
        levels.addWidget(self.add_level_button,0,Qt.AlignmentFlag.AlignLeft)
        self.adapter_button=QPushButton('请求兼容选项');self.adapter_button.setCheckable(True);self.adapter_button.setObjectName('quiet')
        levels.addWidget(self.adapter_button,0,Qt.AlignmentFlag.AlignLeft)
        self.adapter_combo=QComboBox()
        for key,label in [('protocol','按服务 API 协议发送'),('deepseek','包含 DeepSeek thinking 开关'),('glm','包含 GLM thinking 开关'),('thinking','只发送 thinking 开关')]:self.adapter_combo.addItem(label,key)
        adapter=model.get('reasoning_adapter','protocol')
        if self.adapter_combo.findData(adapter)<0:self.adapter_combo.addItem(adapter,adapter)
        self.adapter_combo.setCurrentIndex(self.adapter_combo.findData(adapter));self.adapter_combo.hide()
        self.adapter_button.toggled.connect(self.adapter_combo.setVisible);levels.addWidget(self.adapter_combo)
        self.adapter_combo.setToolTip('通常使用服务所选协议。仅当网关还要求 thinking 开关时调整，不影响档位列表。')
        self.output_parameter_combo=QComboBox()
        for key,text in [('auto','输出上限参数：自动'),('max_completion_tokens','max_completion_tokens'),('max_tokens','max_tokens')]:self.output_parameter_combo.addItem(text,key)
        self.output_parameter_combo.setCurrentIndex(self.output_parameter_combo.findData(model.get('output_parameter','auto')))
        self.output_parameter_combo.hide();self.adapter_button.toggled.connect(self.output_parameter_combo.setVisible)
        levels.addWidget(self.output_parameter_combo)
        self.output_parameter_combo.currentIndexChanged.connect(self._changed)
        detail.addWidget(self.levels_widget);self.levels_widget.setVisible(self.reasoning_check.isChecked())
        self.error=QLabel();self.error.setWordWrap(True);self.error.setStyleSheet('color:#a74646');self.error.hide();detail.addWidget(self.error)
        outer.addWidget(self.details);self.details.setVisible(expanded);self.expand_button.setChecked(expanded)
        self.expand_button.toggled.connect(self._expand)
        self.remove_button.clicked.connect(lambda:self.removed.emit(self));self.handle.move.connect(lambda delta:self.reordered.emit(self.identity,delta))
        self.reasoning_check.toggled.connect(self._reasoning_toggled)
        for field in (self.id_edit,self.name_edit,self.context_edit,self.output_edit):field.textChanged.connect(self._changed)
        for field in (self.text_check,self.image_check):field.toggled.connect(self._changed)
        self.adapter_combo.currentIndexChanged.connect(self._changed)
        self.loading=False;self._update_level_choices()
    def _expand(self,show):
        self.details.setVisible(show);self.expand_button.setIcon(QIcon(str(Path(__file__).parent/'ui_assets'/('model-chevron-up.svg' if show else 'model-chevron-down.svg'))))
    def _reasoning_toggled(self,on):
        if on and not self.rows:
            for level in ('off','low','medium','high'):self._append_level(level,default_level_value(level))
        self.levels_widget.setVisible(on);self._update_level_choices();self._changed()
    def _append_level(self,level,wire):
        row=LevelRow(level,wire,self.rows_widget);self.rows.append(row);self.rows_layout.addWidget(row)
        row.changed.connect(self._levels_changed);row.removed.connect(self._remove_level)
    def add_level(self):
        value=next_level([row.level.currentData() for row in self.rows])
        if value is not None:self._append_level(value,default_level_value(value));self._levels_changed()
    def _remove_level(self,row):
        self.rows.remove(row);self.rows_layout.removeWidget(row);row.deleteLater();self._levels_changed()
    def _levels_changed(self):self._update_level_choices();self._changed()
    def _update_level_choices(self):
        used=[row.level.currentData() for row in self.rows]
        for row in self.rows:
            for i in range(row.level.count()):
                item=row.level.model().item(i)
                item.setEnabled(row.level.itemData(i)==row.level.currentData() or row.level.itemData(i) not in used)
        if hasattr(self,'add_level_button'):self.add_level_button.setEnabled(next_level(used) is not None)
    def value(self):
        value=deepcopy(self.original);identity=self.id_edit.text().strip()
        value.update(id=identity,name=self.name_edit.text().strip() or identity)
        for key,field in [('context_window',self.context_edit),('max_tokens',self.output_edit)]:
            try:capacity=parse_capacity(field.text())
            except ValueError:capacity=field.text()
            if capacity is None:value.pop(key,None)
            else:value[key]=capacity
        value['input']=[key for key,field in [('text',self.text_check),('image',self.image_check)] if field.isChecked()]
        if self.reasoning_check.isChecked():
            pairs=[row.value() for row in self.rows]
            value['reasoning_levels']=dict(pairs)
            if len(value['reasoning_levels'])!=len(pairs):value['reasoning_levels']={} # validation names invalid declarations
            adapter=self.adapter_combo.currentData()
            if adapter!='protocol':value['reasoning_adapter']=adapter
            else:value.pop('reasoning_adapter',None)
        else:
            value.pop('reasoning_levels',None);value.pop('reasoning_adapter',None)
        parameter=self.output_parameter_combo.currentData()
        if parameter!='auto':value['output_parameter']=parameter
        else:value.pop('output_parameter',None)
        return value
    def _changed(self,*args):
        if self.loading:return
        try:validate_model(self.value());self.error.hide()
        except ValueError as exc:self.error.setText(str(exc));self.error.setVisible(self.details.isVisible())
        self.changed.emit()
    def dragEnterEvent(self,event):
        if event.mimeData().hasFormat('application/x-reprolab-model'):event.acceptProposedAction()
    def dragMoveEvent(self,event):self.dragEnterEvent(event)
    def dropEvent(self,event):
        source=bytes(event.mimeData().data('application/x-reprolab-model')).decode('ascii',errors='ignore')
        self.dropped.emit(source,self.identity,event.position().y()>self.height()/2);event.acceptProposedAction()


class ModelCards(QWidget):
    changed=Signal()
    def __init__(self,parent=None):
        super().__init__(parent);self.cards=[];self.loading=False
        self.box=QVBoxLayout(self);self.box.setContentsMargins(0,0,0,0);self.box.setSpacing(12)
        self.empty=QLabel('尚未添加模型。获取可用模型，或在下方手动添加。')
        self.empty.setWordWrap(True);self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet('QLabel { border:1px dashed #cdd1d9; border-radius:10px; color:#8b8e96; padding:24px; }')
        self.box.addWidget(self.empty)
    def set_models(self,models):
        self.loading=True
        expanded={card.id_edit.text():card.expand_button.isChecked() for card in self.cards}
        for card in self.cards:self.box.removeWidget(card);card.deleteLater()
        self.cards=[]
        for model in models:self._append(model,expanded.get(model.get('id',''),False))
        self.empty.setVisible(not self.cards);self.loading=False
    def _append(self,model,expanded):
        card=ModelCard(model,expanded=expanded,parent=self)
        self.cards.append(card);self.box.addWidget(card)
        card.changed.connect(self._changed);card.removed.connect(self.remove);card.reordered.connect(self.move);card.dropped.connect(self.drop)
        return card
    def add_blank(self):
        if len(self.cards)>=1000:return
        card=self._append({'id':'','name':''},True);self.empty.hide();self._changed();card.id_edit.setFocus()
    def models(self):return [card.value() for card in self.cards]
    def _changed(self):
        if not self.loading:self.changed.emit()
    def remove(self,card):
        self.cards.remove(card);self.box.removeWidget(card);card.deleteLater();self.empty.setVisible(not self.cards);self._changed()
    def move(self,identity,delta):
        index=next((i for i,c in enumerate(self.cards) if c.identity==identity),-1)
        if index<0 or not 0<=index+delta<len(self.cards):return
        self.cards[index],self.cards[index+delta]=self.cards[index+delta],self.cards[index]
        self._arrange();self.cards[index+delta].handle.setFocus()
    def drop(self,source,target,after):
        mapping={c.identity:c for c in self.cards}
        if source not in mapping or target not in mapping or source==target:return
        card=mapping[source];self.cards.remove(card)
        self.cards.insert(self.cards.index(mapping[target])+(1 if after else 0),card);self._arrange()
    def _arrange(self):
        for card in self.cards:self.box.removeWidget(card)
        for card in self.cards:self.box.addWidget(card)
        self._changed()
