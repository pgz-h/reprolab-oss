"""A focused starting point for checking a change."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget


def _text(text, name=None):
    item = QLabel(text)
    item.setWordWrap(True)
    item.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        item.setObjectName(name)
    return item


def _glass_shadow(widget):
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(22)
    shadow.setOffset(0, 4)
    shadow.setColor(QColor(36, 47, 70, 13))
    widget.setGraphicsEffect(shadow)


class StartPage(QScrollArea):
    def __init__(self, *, project, case, ai, reports, demo, project_demo, resume, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        content = QWidget()
        self.setWidget(content)
        body = QVBoxLayout(content)
        body.setContentsMargins(8, 8, 12, 12)
        body.setSpacing(18)
        self._busy = False
        self._resume_available = False

        hero = QWidget()
        hero.setObjectName('hero')
        _glass_shadow(hero)
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(28, 27, 28, 25)
        hero_layout.setSpacing(14)
        hero_layout.addWidget(_text('检查项目改动', 'heroTitle'))
        hero_layout.addSpacing(6)
        actions = QHBoxLayout()
        self.project_button = QPushButton('开始项目检查  →')
        self.project_button.setObjectName('primary')
        self.project_button.clicked.connect(lambda: project())
        self.demo_button = QPushButton('运行内置示例')
        self.demo_button.setObjectName('quiet')
        self.demo_button.clicked.connect(lambda: demo())
        actions.addWidget(self.project_button)
        actions.addWidget(self.demo_button)
        actions.addStretch(1)
        hero_layout.addLayout(actions)
        body.addWidget(hero)

        recent = self.recent_card = QWidget()
        recent.setObjectName('card')
        _glass_shadow(recent)
        recent_layout = QHBoxLayout(recent)
        recent_layout.setContentsMargins(20, 16, 20, 16)
        recent_text = QVBoxLayout()
        recent_text.setSpacing(4)
        recent_text.addWidget(_text('继续上次检查', 'cardTitle'))
        self.recent_label = _text('', 'muted')
        recent_text.addWidget(self.recent_label)
        recent_layout.addLayout(recent_text, 1)
        self.resume_button = QPushButton('继续检查  →')
        self.resume_button.setEnabled(False)
        self.resume_button.clicked.connect(lambda: resume())
        recent_layout.addWidget(self.resume_button)
        body.addWidget(recent)
        recent.hide()

        choices = QGridLayout()
        choices.setSpacing(16)
        self.case_button = self._card(choices, 0, '修复验收', '声明旧版故障，用实际测试核对修复后的表现。', '准备修复案例', case)
        self.ai_button = self._card(choices, 1, 'AI 检查计划', '让 AI 起草测试和计划，审阅后再实际运行。', '准备检查计划', ai)
        body.addLayout(choices)

        extras = QHBoxLayout()
        extras.addStretch(1)
        self.project_demo_button = QPushButton('创建购物车示例')
        self.project_demo_button.setObjectName('quiet')
        self.project_demo_button.setToolTip('准备独立教学项目和快照，之后由你发现并执行测试。')
        self.project_demo_button.clicked.connect(lambda: project_demo())
        self.reports_button = QPushButton('打开已有报告')
        self.reports_button.setObjectName('quiet')
        self.reports_button.clicked.connect(lambda: reports())
        extras.addWidget(self.project_demo_button)
        extras.addWidget(self.reports_button)
        body.addLayout(extras)
        body.addStretch(1)

    @staticmethod
    def _card(grid, column, title, description, action, callback):
        card = QWidget()
        card.setObjectName('card')
        _glass_shadow(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 19, 20, 17)
        layout.setSpacing(10)
        layout.addWidget(_text(title, 'cardTitle'))
        card.setToolTip(description)
        layout.addStretch(1)
        button = QPushButton(action + '  →')
        button.setObjectName('quiet')
        button.clicked.connect(lambda: callback())
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
        grid.addWidget(card, 0, column)
        grid.setColumnStretch(column, 1)
        return button

    def set_busy(self, busy):
        self._busy = busy
        for button in (self.project_button, self.case_button, self.ai_button, self.reports_button,
                       self.demo_button, self.project_demo_button):
            button.setEnabled(not busy)
        self.resume_button.setEnabled(not busy and self._resume_available)

    def set_recent_project(self, title, available):
        self._resume_available = available
        self.recent_label.setText(title if available else '')
        self.recent_card.setVisible(available)
        self.resume_button.setEnabled(available and not self._busy)
