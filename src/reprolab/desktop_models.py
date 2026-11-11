"""Custom model services, discoverable catalogs and explicit connection probes."""
import hashlib
import json
import os
import re
from pathlib import Path
from .platform_support import application_directories, private_permissions
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QSpinBox,
    QVBoxLayout, QWidget, QTreeWidget, QTreeWidgetItem, QTabBar, QFrame,
)

from .ai import API_CONFIG_FIELDS, _api_settings, generate_api
from .desktop_tasks import Task
from .desktop_reasoning import ReasoningEditor
from .desktop_model_cards import ModelCards
from .model_definitions import validate_catalog, selected_definition
from .model_reasoning import upgrade_profile_models
from .model_catalog import MAX_MODELS, discover_models, model_record

# Endpoint presets only: the actual model catalog still comes from the chosen service.
SERVICE_PRESETS = {
    'deepseek': ('DeepSeek', 'https://api.deepseek.com/v1', 'openai'),
    'openai': ('OpenAI', 'https://api.openai.com/v1', 'responses'),
    'anthropic': ('Anthropic', 'https://api.anthropic.com/v1', 'anthropic'),
    'glm': ('智谱 GLM', 'https://open.bigmodel.cn/api/paas/v4', 'openai'),
}

_TEST_PROMPT = 'Return only this JSON object: {"ok":true}.'


def _validate_profile(data, *, require_model=True, allow_ambient=False):
    if not isinstance(data, dict) or set(data) - API_CONFIG_FIELDS:
        raise ValueError('模型配置字段无效，请检查本机 JSON 文件。')
    for field in ('base_url', 'model', 'api_key', 'api_key_env', 'provider_id', 'display_name'):
        if field in data and not isinstance(data[field], str):
            raise ValueError('模型配置中的地址、名称和凭据字段必须是文本。')
    candidate = dict(data)
    if not candidate.get('api_key'):
        source = candidate.get('api_key_source', 'legacy')
        if source == 'profile' or (not candidate.get('api_key_env') and not (allow_ambient and source in ('legacy', 'environment'))):
            raise ValueError('请输入 API Key，或指定保存 Key 的环境变量名称。')
        candidate['api_key'] = 'profile-validation-placeholder'
    _api_settings(candidate, use_environment=False, require_model=require_model)
    secret = data.get('api_key', '')
    provider_id = data.get('provider_id')
    if provider_id is not None and (not isinstance(provider_id, str)
            or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', provider_id)):
        raise ValueError('Provider ID 需以小写字母开头，只使用小写字母、数字、横线或下划线，最多64字符。')
    name = data.get('display_name')
    if name is not None and model_record(name, secret=secret) is None:
        raise ValueError('显示名称需为1–200个可显示字符。')
    if secret and provider_id and secret in provider_id:
        raise ValueError('Provider ID 不能包含 API Key。')
    validate_catalog(data.get('models', []), secret=secret)


def _read_profile(path):
    path = Path(path)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('本机模型配置超过1 MiB。')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, RecursionError):
        raise ValueError('模型配置不是有效的 JSON。') from None
    if isinstance(data, dict):
        optional = {'api_key', 'api_key_env', 'provider_id', 'display_name'}
        data = {key: value for key, value in data.items() if not (key in optional and value is None)}
    _validate_profile(data, allow_ambient=True)
    if data.get('model') and not any(m['id']==data['model'] for m in data.get('models', [])):
        data.setdefault('models', []).append({'id':data['model'],'name':data['model']})
    return upgrade_profile_models(data)


def _write_profile(data, directory):
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f'model-{uuid4()}.json'
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            private_permissions(stream.fileno())
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def _test_profile(data):
    try:
        with tempfile.TemporaryDirectory(prefix='reprolab-model-test-') as folder:
            config = _write_profile(data, folder)
            content, _ = generate_api(
                _TEST_PROMPT, Path(folder), config_file=config, timeout=60,
                system_prompt='Return only the requested JSON object. This is a public connection test.',
            )
            try:
                fenced = re.fullmatch(r'```(?:json)?[ \t]*\r?\n(.*?)\r?\n```', content.strip(), re.DOTALL)
                answer = json.loads(fenced.group(1) if fenced else content)
            except (ValueError, TypeError, RecursionError):
                raise ValueError('服务已响应，但未返回测试所需的 JSON；请检查模型和 JSON 模式。') from None
            if answer != {'ok': True}:
                raise ValueError('服务已响应，但未返回预期测试结果；请检查模型和协议。')
    except ValueError as exc:
        message = str(exc)
        if data.get('api_key'):
            message = message.replace(data['api_key'], '[已隐藏]')
        raise ValueError(message) from None
    except Exception:
        raise ValueError('连接测试未完成，请检查本机文件权限、网络和模型配置。') from None
    return True


def _origin(url):
    """Compare credential destinations without treating path edits as a new host."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            return None
        port = parts.port
        return parts.scheme, parts.hostname.lower(), port if port is not None else (443 if parts.scheme == 'https' else 80)
    except (ValueError, TypeError):
        return None


def _key_identity(key):
    if not isinstance(key, str) or not key:
        return None
    return 'key', hashlib.sha256(key.encode('utf-8')).digest()


def connection_summary(path):
    """Describe a local connection without displaying credentials or file paths."""
    if not path:
        return '未连接 · AI 可选'
    try:
        data = _read_profile(path)
        name = data.get('display_name') or urlsplit(data['base_url']).hostname or '自定义服务'
        return f"{name} · {data['model']}"
    except (OSError, ValueError, TypeError, KeyError):
        return '连接配置不可用 · 请重新连接或导入'


class ModelCatalogDialog(QDialog):
    """Choose a saved catalog independently from the model used by one request."""

    def __init__(self, available, configured, *, partial=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle('选择要保存的模型')
        self.resize(580, 540)
        self.setModal(True)
        layout = QVBoxLayout(self)
        note = QLabel('勾选要保留的模型；一次 AI 请求使用其中一个。')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('搜索模型名称或 ID')
        self.select_all_button = QPushButton('全选可见')
        self.clear_button = QPushButton('取消可见')
        actions = QHBoxLayout()
        actions.addWidget(self.search_edit, 1)
        actions.addWidget(self.select_all_button)
        actions.addWidget(self.clear_button)
        layout.addLayout(actions)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        layout.addWidget(self.tree, 1)
        existing = {model['id'] for model in configured if model['id']}
        offered = {model['id'] for model in available}
        records = {model['id']: model for model in available}
        records.update({model['id']: model for model in configured if model['id']})
        for model in records.values():
            text = model['id'] if model['name'] == model['id'] else f"{model['name']} · {model['id']}"
            if model['id'] not in offered:
                text += '（已保存，本次目录未列出）'
            item = QTreeWidgetItem([text])
            item.setData(0, Qt.ItemDataRole.UserRole, dict(model))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if not existing or model['id'] in existing else Qt.CheckState.Unchecked)
            self.tree.addTopLevelItem(item)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        if partial:
            limited = QLabel('服务只返回部分目录；未列出的模型可以在连接窗口手动添加。')
            limited.setWordWrap(True)
            layout.addWidget(limited)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.apply_button = self.buttons.addButton('更新列表', QDialogButtonBox.ButtonRole.AcceptRole)
        layout.addWidget(self.buttons)
        self.buttons.rejected.connect(self.reject)
        self.buttons.accepted.connect(self.accept)
        self.search_edit.textChanged.connect(self._filter)
        self.select_all_button.clicked.connect(lambda: self._check_visible(True))
        self.clear_button.clicked.connect(lambda: self._check_visible(False))
        self.tree.itemChanged.connect(self._update_count)
        self._update_count()

    def _items(self):
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def selected_models(self):
        return [item.data(0, Qt.ItemDataRole.UserRole) for item in self._items()
                if item.checkState(0) == Qt.CheckState.Checked]

    def _filter(self, text):
        for item in self._items():
            item.setHidden(text.casefold() not in item.text(0).casefold())
        self._update_count()

    def _check_visible(self, checked):
        self.tree.blockSignals(True)
        for item in self._items():
            if not item.isHidden():
                item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        self._update_count()

    def _update_count(self, *args):
        selected = len(self.selected_models())
        hidden = sum(item.isHidden() and item.checkState(0) == Qt.CheckState.Checked for item in self._items())
        self.status_label.setText(f'已选 {selected} 项，其中 {hidden} 项被搜索隐藏。最多保存 {MAX_MODELS} 项。')
        self.apply_button.setEnabled(selected <= MAX_MODELS)

    def accept(self):
        if len(self.selected_models()) <= MAX_MODELS:
            super().accept()


class SavedModelSelector(QWidget):
    """Switch a saved model through a private profile copy, without a network call."""
    changed = Signal(str)

    def __init__(self, path_edit, output_dir, parent=None):
        super().__init__(parent)
        self.path_edit = path_edit
        self.output_dir = output_dir
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        self.combo.setMinimumContentsLength(20)
        self.combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        layout.addWidget(self.combo)
        self.reasoning_editor = ReasoningEditor(self)
        self.reasoning_editor.changed.connect(self._reasoning_changed)
        layout.addWidget(self.reasoning_editor)
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        layout.addWidget(self.status_label)
        path_edit.textChanged.connect(self.refresh)
        self.combo.activated.connect(self._select)
        self.refresh()

    def refresh(self, *args):
        self.combo.clear()
        path = self.path_edit.text().strip()
        try:
            data = _read_profile(path) if path else None
        except (OSError, ValueError, TypeError, KeyError):
            data = None
        if data is None:
            self.combo.addItem(connection_summary(path), None)
            self.combo.setEnabled(False)
            self.reasoning_editor.hide()
            return
        name = data.get('display_name') or urlsplit(data['base_url']).hostname or '自定义服务'
        records = {model['id']: model for model in data.get('models', [])}
        records.setdefault(data['model'], {'id': data['model'], 'name': data['model']})
        for model in records.values():
            self.combo.addItem(f"{name} · {model['id']}", model['id'])
        self.combo.setCurrentIndex(self.combo.findData(data['model']))
        self.combo.setEnabled(True)
        self.reasoning_editor.show()
        self.reasoning_editor.set_context(data['base_url'], data.get('protocol', 'openai'), data['model'],
                                          data.get('model_reasoning', {}).get(data['model']),
                                          selected_definition(data.get('models', []), data['model']))

    def _select(self, index):
        model = self.combo.itemData(index)
        if not model:
            return
        try:
            data = _read_profile(self.path_edit.text().strip())
            if model == data['model']:
                return
            if model not in {item['id'] for item in data.get('models', [])}:
                raise ValueError('模型目录已改变，请重新选择连接。')
            data['model'] = model
            _validate_profile(data, allow_ambient=True)
            path = _write_profile(data, self.output_dir)
        except (OSError, ValueError, TypeError, KeyError):
            self.refresh()
            self.status_label.setText('模型切换未保存，请检查连接配置和目录权限。')
            self.status_label.show()
            return
        self.path_edit.setText(str(path))
        self.status_label.hide()
        self.changed.emit(str(path))


    def _reasoning_changed(self, option):
        try:
            data = _read_profile(self.path_edit.text().strip())
            if data['model'] != self.combo.currentData():
                raise ValueError('连接已改变')
            options = dict(data.get('model_reasoning', {}))
            if option == {'adapter': 'auto', 'effort': 'default'}:
                options.pop(data['model'], None)
            else:
                options[data['model']] = option
            data['model_reasoning'] = options
            _validate_profile(data, allow_ambient=True)
            path = _write_profile(data, self.output_dir)
        except (OSError, ValueError, TypeError, KeyError):
            self.refresh()
            self.status_label.setText('思考设置未保存，请检查兼容方式、档位和目录权限。')
            self.status_label.show()
            return
        self.path_edit.setText(str(path))
        self.status_label.hide()
        self.changed.emit(str(path))


class ModelConnectionDialog(QDialog):
    """A service draft and model directory, saved only after an explicit action."""

    def __init__(self, config_path=None, output_dir=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('连接模型')
        self.resize(720, 820)
        self.output_dir = Path(output_dir) if output_dir is not None else application_directories()[0] / 'model-connections'
        self.result_data = None
        self.task = None
        self._closing = False
        self._tested_save_data = None
        self.catalog_picker = None
        self._profile = {}
        self._reasoning_by_model = {}
        self._reasoning_connection_key = None
        self._catalog_key = None
        self._catalog_credential = None
        self._catalog_outdated = False
        self._loading_profile = False
        self._credential_origins = {}
        self._environment_edited = False
        self._updating_models = False
        self._model_context = None
        self._model_explicit = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        self.mode_tabs = QTabBar()
        self.mode_tabs.addTab('第三方模型提供商'); self.mode_tabs.addTab('自定义模型 API')
        self.mode_tabs.setExpanding(False);self.mode_tabs.setCurrentIndex(1)
        self.mode_tabs.setDrawBase(False)
        self.mode_tabs.setStyleSheet('QTabBar::tab { background:#e9eaee; color:#777b84; border:none; border-radius:8px; padding:10px 22px; margin-right:4px; } QTabBar::tab:selected { background:white; color:#25282d; border:1px solid #cdd0d8; }')
        layout.addWidget(self.mode_tabs)
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.form_widget = QWidget()
        body = QVBoxLayout(self.form_widget);body.setContentsMargins(2, 10, 10, 10);body.setSpacing(16)
        self.custom_hint = QLabel('连接中转站、自部署服务或其他兼容接口，填写地址、协议和模型。')
        self.custom_hint.setWordWrap(True);self.custom_hint.setObjectName('muted');body.addWidget(self.custom_hint)
        form = self.connection_form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(9)
        self.service_combo=QComboBox();self.service_combo.addItem('新建连接',None)
        form.addRow('已保存服务', self.service_combo)
        self.preset_combo=QComboBox();self.preset_combo.addItem('自定义 API / 中转服务','custom')
        for identity,(name,_,_) in SERVICE_PRESETS.items():self.preset_combo.addItem(name,identity)
        form.addRow('服务商',self.preset_combo)
        self.provider_id_edit=QLineEdit();self.provider_id_edit.setPlaceholderText('自动生成，也可填写 acme-gateway')
        form.addRow('Provider ID',self.provider_id_edit)
        self.display_name_edit=QLineEdit();self.display_name_edit.setPlaceholderText('显示名称（可留空）')
        form.addRow('显示名称',self.display_name_edit)
        self.base_url_edit=QLineEdit();self.base_url_edit.setPlaceholderText('https://gateway.example/v1')
        form.addRow('API 地址',self.base_url_edit)
        self.protocol_combo=QComboBox()
        for label,value in [('OpenAI Chat Completions','openai'),('OpenAI Responses','responses'),('Anthropic Messages','anthropic')]:self.protocol_combo.addItem(label,value)
        form.addRow('API 协议',self.protocol_combo)
        self.destination_label=QLabel();self.destination_label.setWordWrap(True);self.destination_label.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow('',self.destination_label)
        self.api_key_edit=QLineEdit();self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_line=QWidget();key_layout=QHBoxLayout(key_line);key_layout.setContentsMargins(0,0,0,0)
        self.show_key_button=QPushButton('显示');self.show_key_button.setCheckable(True)
        self.show_key_button.toggled.connect(self._show_typed_key)
        key_layout.addWidget(self.api_key_edit,1);key_layout.addWidget(self.show_key_button)
        form.addRow('API 密钥',key_line)
        self.reuse_key_check=QCheckBox('同意将当前 Key 用于新的服务地址');self.reuse_key_check.hide()
        form.addRow('',self.reuse_key_check);body.addLayout(form)
        divider=QFrame();divider.setFrameShape(QFrame.Shape.HLine);body.addWidget(divider)
        head=QHBoxLayout();self.catalog_label=QLabel('模型目录');head.addWidget(self.catalog_label,1)
        self.restore_models_button=QPushButton('恢复已保存目录');self.restore_models_button.setObjectName('quiet')
        self.restore_models_button.clicked.connect(self.restore_models);head.addWidget(self.restore_models_button)
        self.fetch_button=QPushButton('获取可用模型');self.fetch_button.setObjectName('quiet');head.addWidget(self.fetch_button)
        body.addLayout(head)
        self.cards=ModelCards(self);self.cards.changed.connect(self._cards_changed);body.addWidget(self.cards)
        self.add_model_button=QPushButton('添加模型');self.add_model_button.clicked.connect(self.add_model)
        body.addWidget(self.add_model_button,0,Qt.AlignmentFlag.AlignLeft)
        self.catalog_status=QLabel();self.catalog_status.setWordWrap(True);self.catalog_status.setTextFormat(Qt.TextFormat.PlainText)
        self.catalog_status.setObjectName('muted');body.addWidget(self.catalog_status)
        default_row=QHBoxLayout();default_row.addWidget(QLabel('默认使用'))
        self.model_combo=QComboBox();self.model_combo.setEditable(True);self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_combo.setMaxVisibleItems(12);self.model_combo.setMinimumContentsLength(24)
        self.model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.model_edit=self.model_combo.lineEdit();self.model_edit.setPlaceholderText('选择已添加模型，或填写模型 ID')
        self.model_combo.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.model_combo.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        default_row.addWidget(self.model_combo,1);body.addLayout(default_row)
        self.manage_models_button=QPushButton('管理已选模型',self);self.manage_models_button.hide();self.manage_models_button.clicked.connect(self.manage_models)
        self.remove_model_button=QPushButton('移除当前模型',self);self.remove_model_button.hide();self.remove_model_button.clicked.connect(self.remove_model)
        self.advanced_button=QPushButton('连接高级设置');self.advanced_button.setCheckable(True);self.advanced_button.setObjectName('quiet')
        body.addWidget(self.advanced_button,0,Qt.AlignmentFlag.AlignLeft)
        self.advanced_widget=QWidget();advanced=QFormLayout(self.advanced_widget)
        self.api_key_env_edit=QLineEdit();self.api_key_env_edit.setPlaceholderText('可选，使用本机环境变量中的 Key');advanced.addRow('Key 环境变量',self.api_key_env_edit)
        self.auth_combo=QComboBox();self.auth_combo.addItem('Bearer','bearer');self.auth_combo.addItem('API Key 请求头','api-key');advanced.addRow('认证方式',self.auth_combo)
        self.json_mode_check=QCheckBox('请求 JSON 格式');advanced.addRow('JSON 模式',self.json_mode_check)
        self.max_tokens_spin=QSpinBox();self.max_tokens_spin.setRange(1,2_000_000_000);self.max_tokens_spin.setSingleStep(1024)
        advanced.addRow('未单独设置模型的输出上限',self.max_tokens_spin)
        body.addWidget(self.advanced_widget);self.advanced_widget.hide()
        self.help_button=QPushButton('AI 能做什么？');self.help_button.setObjectName('quiet');self.help_button.setCheckable(True)
        body.addWidget(self.help_button,0,Qt.AlignmentFlag.AlignLeft)
        self.help_label=QLabel('用于准备测试、验收计划和解读实际结果。模型卡片声明的能力不代表服务已验证；图片输入目前仅作能力记录，检查工作流发送文本。')
        self.help_label.setWordWrap(True);self.help_label.hide();self.help_button.toggled.connect(self.help_label.setVisible);body.addWidget(self.help_label)
        body.addStretch();self.scroll.setWidget(self.form_widget);layout.addWidget(self.scroll,1)
        self.status_label=QLabel();self.status_label.setTextFormat(Qt.TextFormat.PlainText);self.status_label.setWordWrap(True);layout.addWidget(self.status_label)
        self.buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.test_button=self.buttons.addButton('测试默认模型',QDialogButtonBox.ButtonRole.ActionRole)
        self.test_button.setToolTip('发送固定公开短句，可能产生 API 费用；仅测试当前默认模型。')
        self.save_button=self.buttons.addButton('保存并使用',QDialogButtonBox.ButtonRole.AcceptRole);self.save_button.setObjectName('primary')
        self.test_save_button=self.buttons.addButton('测试并保存',QDialogButtonBox.ButtonRole.ActionRole);self.test_save_button.hide()
        self.test_save_button.clicked.connect(self.test_and_save)
        layout.addWidget(self.buttons);self.buttons.rejected.connect(self.reject)
        self.test_button.clicked.connect(self.test_connection);self.save_button.clicked.connect(self.save_connection)
        self.fetch_button.clicked.connect(self.fetch_models)
        self.advanced_button.toggled.connect(self.advanced_widget.setVisible)
        self.advanced_button.toggled.connect(self._update_simple_fields)
        self._saved_models=[];self._syncing_cards=False
        self.protocol_combo.currentIndexChanged.connect(self._protocol_changed)
        self._apply_profile({})
        profile_warning = self._populate_services(config_path)
        self.service_combo.currentIndexChanged.connect(self._service_changed)
        self.preset_combo.currentIndexChanged.connect(self._preset_changed)
        self._service_changed()
        self.mode_tabs.currentChanged.connect(self._mode_changed)
        if profile_warning:
            self.status_label.setText(profile_warning)
        self.base_url_edit.textChanged.connect(self._destination_changed)
        self.api_key_edit.textChanged.connect(self._typed_key_changed)
        self.api_key_env_edit.textChanged.connect(self._environment_key_changed)
        for combo in (self.protocol_combo, self.auth_combo):
            combo.currentTextChanged.connect(self._connection_edited)
        self.model_combo.currentTextChanged.connect(self._model_selection_changed)
        self.json_mode_check.toggled.connect(self._connection_edited)
        self.max_tokens_spin.valueChanged.connect(self._connection_edited)
        self.reuse_key_check.toggled.connect(self._connection_edited)

    def _sync_preset(self):
        address = self.base_url_edit.text().strip().rstrip('/')
        protocol = self.protocol_combo.currentData()
        identity = next((key for key, (_, url, api) in SERVICE_PRESETS.items()
                         if (address, protocol) == (url, api)), 'custom')
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentIndex(self.preset_combo.findData(identity))
        self.preset_combo.blockSignals(False)
        self._update_simple_fields()

    def _show_typed_key(self, visible):
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        self.show_key_button.setText('隐藏' if visible else '显示')

    def _mode_changed(self, index):
        if index == 0 and self.preset_combo.currentData() == 'custom':
            self.preset_combo.setCurrentIndex(1)
        elif index == 1:
            self.preset_combo.setCurrentIndex(0)
        self._update_simple_fields()

    def _update_simple_fields(self, *args):
        custom = self.preset_combo.currentData() == 'custom'
        self.mode_tabs.blockSignals(True);self.mode_tabs.setCurrentIndex(1 if custom else 0);self.mode_tabs.blockSignals(False)
        self.custom_hint.setVisible(custom)
        show_details=custom or self.advanced_button.isChecked()
        for field in (self.provider_id_edit,self.display_name_edit,self.base_url_edit,self.protocol_combo):
            self.connection_form.setRowVisible(field,show_details)
        self.connection_form.setRowVisible(self.preset_combo,not custom)
        self.destination_label.setText(self.base_url_edit.text().strip())
        self.connection_form.setRowVisible(self.destination_label,not show_details)
        self.connection_form.setRowVisible(self.service_combo,self.service_combo.count()>1)
        self.restore_models_button.setVisible(bool(self._saved_models))

    def restore_models(self):
        self._set_models(self._saved_models,self._profile.get('model',''))
        self._reasoning_by_model=dict(self._profile.get('model_reasoning',{}))
        self._sync_reasoning()
        self.status_label.setText('已恢复本次打开时的模型目录；保存后生效。')

    def _cards_changed(self):
        if self._syncing_cards:return
        models=self.cards.models()
        reset=False
        for model in models:
            option=self._reasoning_by_model.get(model['id'])
            if option and option.get('adapter') == 'catalog' and option.get('effort') not in ('default', *model.get('reasoning_levels', {})):
                self._reasoning_by_model.pop(model['id'],None);reset=True
        index=self.model_combo.currentIndex()
        selected=self._selected_model()
        if selected not in {model['id'] for model in models}:
            selected=models[index]['id'] if 0<=index<len(models) else ''
        self._set_models(models,selected,update_cards=False)
        self._catalog_key,self._catalog_credential=self._current_catalog_context()
        self._model_context=self._current_catalog_context();self._model_explicit=True
        self._connection_edited()
        if reset:self.status_label.setText('当前选择的推理档位已移除，已恢复服务默认。')

    def _preset_changed(self):
        identity = self.preset_combo.currentData()
        if identity == 'custom':
            self._update_simple_fields()
            return
        name, address, protocol = SERVICE_PRESETS[identity]
        # Selecting another provider starts a new draft; never carry a key or catalog across.
        self.service_combo.blockSignals(True)
        self.service_combo.setCurrentIndex(0)
        self.service_combo.blockSignals(False)
        self._apply_profile({'display_name': name, 'base_url': address, 'protocol': protocol, 'api_key_source': 'profile'})
        self.status_label.setText('填写该服务的 Key，获取模型或直接填写模型 ID。')
        self.api_key_edit.setFocus()

    def _sync_reasoning(self):
        key = self._current_catalog_key()
        if self._reasoning_connection_key is not None and self._reasoning_connection_key != key:
            self._reasoning_by_model = {}
        self._reasoning_connection_key = key
        model = self._selected_model()


    def _connection_edited(self, *args):
        if not self.task and not self._closing:
            self.status_label.setText('连接配置已修改，可以测试当前选择并保存。')

    def _active_credential_ids(self):
        typed = self.api_key_edit.text().strip()
        if typed:
            return _key_identity(typed), None
        env_name = self.api_key_env_edit.text().strip()
        source = self._profile.get('api_key_source', 'legacy')
        use_env = bool((source == 'environment' and (env_name or not self._environment_edited))
                       or (env_name and (self._environment_edited
                           or env_name != self._profile.get('api_key_env', '')
                           or not self._profile.get('api_key'))))
        if source == 'legacy' and os.environ.get(env_name or 'REPROLAB_API_KEY'):
            use_env = True
        if use_env:
            name = env_name or 'REPROLAB_API_KEY'
            env_id = ('environment', name)
            return _key_identity(os.environ.get(name)) or env_id, env_id
        return _key_identity(self._profile.get('api_key')), None

    def _active_credential_origin(self):
        primary, fallback = self._active_credential_ids()
        if primary in self._credential_origins:
            return self._credential_origins[primary]
        return self._credential_origins.get(fallback)

    def _requires_credential_consent(self):
        bound_origin = self._active_credential_origin()
        current_origin = _origin(self.base_url_edit.text().strip())
        return bool(bound_origin and current_origin and bound_origin != current_origin)

    def _destination_changed(self, *args):
        if not self._loading_profile:
            self._sync_preset()
            self._sync_reasoning()
            self._credential_inputs_changed()

    def _typed_key_changed(self, *args):
        if not self._loading_profile:
            identity = _key_identity(self.api_key_edit.text().strip())
            if identity is not None:
                self._credential_origins.setdefault(identity, _origin(self.base_url_edit.text().strip()))
            self._credential_inputs_changed()

    def _environment_key_changed(self, *args):
        if not self._loading_profile:
            env_name = self.api_key_env_edit.text().strip()
            if env_name != (self._profile.get('api_key_env') or ''):
                self._environment_edited = True
            if self._environment_edited and env_name:
                identity = ('environment', env_name)
                self._credential_origins.setdefault(identity, _origin(self.base_url_edit.text().strip()))
                key_id = _key_identity(os.environ.get(env_name))
                if key_id is not None:
                    self._credential_origins.setdefault(key_id, self._credential_origins[identity])
            self._credential_inputs_changed()

    def _model_selection_changed(self, *args):
        if not self._loading_profile and not self._updating_models:
            index = self.model_combo.currentIndex()
            from_catalog = index >= 0 and self.model_combo.currentText() == self.model_combo.itemText(index)
            self._model_context = ((self._catalog_key, self._catalog_credential)
                                   if from_catalog else self._current_catalog_context())
            self._model_explicit = not from_catalog
            self._sync_reasoning()
            self._connection_edited()

    def _credential_inputs_changed(self, *args):
        self.reuse_key_check.setChecked(False)
        self.reuse_key_check.setVisible(self._requires_credential_consent())
        if (self._model_explicit and self._model_context
                and self._model_context[0] == self._current_catalog_key()
                and self._catalog_key != self._current_catalog_key()):
            self._model_context = self._current_catalog_context()
        self._update_catalog_status()
        self._connection_edited()

    def _current_catalog_key(self):
        return self.base_url_edit.text().strip().rstrip('/'), self.protocol_combo.currentData()

    def _current_catalog_context(self):
        return self._current_catalog_key(), self._active_credential_ids()[0]

    def _update_catalog_status(self):
        if self._catalog_key is None:
            return
        if (self._catalog_key, self._catalog_credential) != self._current_catalog_context():
            self._catalog_outdated = True
            self.catalog_status.setText('服务地址、协议或凭据已改变，当前目录来自先前连接；获取新目录后将更新。')
        elif self._catalog_outdated:
            self._catalog_outdated = False
            self.catalog_status.setText('当前目录属于所选连接，可重新获取或手动添加模型。')

    def _populate_services(self, config_path):
        dated = []
        for path in self.output_dir.glob('model-*.json'):
            try:
                dated.append((path.stat().st_mtime, path))
            except OSError:
                continue
        paths = [path for _, path in sorted(dated, key=lambda item: item[0], reverse=True)]
        if config_path:
            current = Path(config_path).absolute()
            paths = [current] + [path for path in paths if path.absolute() != current]
        seen = set()
        warning = ''
        for path in paths:
            try:
                data = _read_profile(path)
                identity = data.get('provider_id', str(path))
                if identity in seen:
                    continue
                if len(seen) == 100:
                    break
                seen.add(identity)
                name = data.get('display_name') or urlsplit(data['base_url']).hostname or '自定义服务'
                self.service_combo.addItem(f"{name} · {data['model']}", str(path))
                if config_path and path.absolute() == Path(config_path).absolute():
                    self.service_combo.setCurrentIndex(self.service_combo.count() - 1)
            except (OSError, ValueError, TypeError, KeyError):
                if config_path and path.absolute() == Path(config_path).absolute():
                    warning = '原配置无法读取，请重新填写连接信息。'
        return warning

    def _service_changed(self):
        path = self.service_combo.currentData()
        try:
            self._apply_profile(_read_profile(path) if path else {})
        except (OSError, ValueError, TypeError):
            self._apply_profile({})
            self.service_combo.blockSignals(True)
            self.service_combo.setCurrentIndex(0)
            self.service_combo.blockSignals(False)
            self.status_label.setText('所选配置无法读取，请选择其他服务或新建。')

    def _apply_profile(self, data):
        self._loading_profile = True
        try:
            self._profile = dict(data)
            self._reasoning_by_model = dict(data.get("model_reasoning", {}))
            self._reasoning_connection_key = None
            self.display_name_edit.setText(data.get('display_name', ''))
            self.provider_id_edit.setText(data.get('provider_id', 'custom-' + uuid4().hex[:12]))
            protocol = data.get('protocol', 'openai')
            self.protocol_combo.setCurrentIndex(self.protocol_combo.findData(protocol))
            self.base_url_edit.setText(data.get('base_url', ''))
            self.show_key_button.setChecked(False)
            self.api_key_edit.clear()
            self.api_key_edit.setPlaceholderText('留空保留已有 Key' if data.get('api_key') else '填写服务提供的 API Key')
            self.api_key_env_edit.setText(data.get('api_key_env', ''))
            self.auth_combo.setCurrentIndex(self.auth_combo.findData(data.get('auth_type', 'api-key' if protocol == 'anthropic' else 'bearer')))
            self.json_mode_check.setChecked(data.get('json_mode', True))
            self.max_tokens_spin.setValue(data.get('max_tokens', 8192))
            self.max_tokens_spin.setEnabled(protocol != 'openai')
            models = list(data.get('models', []))
            selected = data.get('model', '')
            if selected and not any(model['id'] == selected for model in models):
                models.append({'id': selected, 'name': selected})
            self._saved_models=[dict(model) for model in models]
            self._set_models(models, selected)
        finally:
            self._loading_profile = False
        origin = _origin(self.base_url_edit.text().strip())
        saved_key = _key_identity(data.get('api_key'))
        source = data.get('api_key_source', 'legacy')
        configured_env = data.get('api_key_env') or 'REPROLAB_API_KEY'
        active_env = ((source == 'environment')
                      or (source == 'legacy' and bool(os.environ.get(configured_env))))
        if saved_key is not None and origin is not None:
            if not active_env:
                self._credential_origins[saved_key] = origin
            else:
                self._credential_origins.setdefault(saved_key, origin)
        env_names = set()
        if source in ('environment', 'legacy') and data.get('api_key_env'):
            env_names.add(data['api_key_env'])
        if source == 'legacy' or (source == 'environment' and not data.get('api_key_env')):
            env_names.add('REPROLAB_API_KEY')
        for name in env_names:
            if origin is None:
                continue
            env_id = ('environment', name)
            explicitly_saved_env = source == 'environment' and bool(data.get('api_key_env'))
            if active_env and name == configured_env and explicitly_saved_env:
                self._credential_origins[env_id] = origin
            else:
                self._credential_origins.setdefault(env_id, origin)
            key_id = _key_identity(os.environ.get(name))
            if key_id is not None:
                if active_env and name == configured_env and explicitly_saved_env:
                    self._credential_origins[key_id] = origin
                else:
                    self._credential_origins.setdefault(key_id, origin)
        self._environment_edited = False
        self._catalog_key = self._current_catalog_key()
        self._catalog_credential = self._active_credential_ids()[0]
        self._model_context = self._current_catalog_context()
        self._model_explicit = False
        self._catalog_outdated = False
        self._credential_inputs_changed()
        self.catalog_status.setText('已加载保存的模型目录，可点选或重新获取。' if models else '填写地址和 Key 后，点击“获取可用模型”。')
        self.status_label.setText('选择模型后可测试并保存。')
        self._sync_preset()
        self._sync_reasoning()

    def _protocol_changed(self):
        protocol = self.protocol_combo.currentData()
        self.max_tokens_spin.setEnabled(protocol != 'openai')
        self.auth_combo.setCurrentIndex(self.auth_combo.findData('api-key' if protocol == 'anthropic' else 'bearer'))
        self._update_catalog_status()
        if not self._loading_profile:
            self._sync_preset()
            self._sync_reasoning()

    def _models(self):
        return [self.model_combo.itemData(index) for index in range(self.model_combo.count())]

    def _selected_model(self):
        current = self.model_combo.currentIndex()
        if current >= 0 and self.model_combo.currentText() == self.model_combo.itemText(current):
            return self.model_combo.itemData(current)['id']
        return self.model_combo.currentText().strip()

    def _set_models(self, models, selected='', *, update_cards=True):
        self._updating_models = True
        try:
            self.model_combo.clear()
            selected_index = -1
            for model in models:
                label = model['id'] if model['name'] == model['id'] else f"{model['name']} · {model['id']}"
                self.model_combo.addItem(label, dict(model))
                if model['id'] == selected:
                    selected_index = self.model_combo.count() - 1
            if selected_index >= 0:
                self.model_combo.setCurrentIndex(selected_index)
            elif selected:
                self.model_edit.setText(selected)
            self.catalog_label.setText(f'模型目录 · {len(models)} 项')
        finally:
            self._updating_models = False
        if update_cards:
            self._syncing_cards=True
            self.cards.set_models(models)
            self._syncing_cards=False
        self._sync_reasoning()

    def add_model(self):
        selected=self._selected_model()
        if not selected or (any(model['id']==selected for model in self._models()) and (self._catalog_key,self._catalog_credential)==self._current_catalog_context()):
            self.cards.add_blank()
            self.scroll.ensureWidgetVisible(self.cards.cards[-1])
            return
        model = model_record(self._selected_model(), secret=self.api_key_edit.text().strip() or self._profile.get('api_key', ''))
        if model is None:
            self.status_label.setText('请在模型框填写有效的模型 ID 后添加。')
            return
        current = self._current_catalog_context()
        changed_service = (self._catalog_key, self._catalog_credential) != current
        models = {} if changed_service else {item['id']: item for item in self._models()}
        if model['id'] not in models and len(models) == MAX_MODELS:
            self.status_label.setText('模型目录已达到1000项，请先移除不需要的模型。')
            return
        models.setdefault(model['id'], model)
        self._set_models(list(models.values()), model['id'])
        self._catalog_key, self._catalog_credential = current
        self._model_context = current
        self._model_explicit = True
        self._catalog_outdated = False
        if changed_service:
            self.catalog_status.setText('已为当前连接创建手动模型目录，可继续获取或添加模型。')
        self.status_label.setText('模型已加入当前目录，点击保存连接后保留。')

    def remove_model(self):
        selected = self._selected_model()
        self._set_models([model for model in self._models() if model['id'] != selected])
        self._model_context = (self._catalog_key, self._catalog_credential)
        self._model_explicit = False

    def _connection_data(self, *, require_model=True):
        current = self._current_catalog_context()
        models = self._models() if (self._catalog_key, self._catalog_credential) == current else []
        data = dict(self._profile)
        data.update(base_url=self.base_url_edit.text().strip(), model=self._selected_model(),
                    protocol=self.protocol_combo.currentData(), auth_type=self.auth_combo.currentData(),
                    json_mode=self.json_mode_check.isChecked(), max_tokens=self.max_tokens_spin.value(),
                    provider_id=self.provider_id_edit.text().strip(),
                    display_name=self.display_name_edit.text().strip() or urlsplit(self.base_url_edit.text().strip()).hostname or '自定义服务',
                    models=models)
        new_key = self.api_key_edit.text().strip()
        env_name = self.api_key_env_edit.text().strip()
        if env_name:
            data['api_key_env'] = env_name
        else:
            data.pop('api_key_env', None)
        if new_key:
            data['api_key'] = new_key
            data['api_key_source'] = 'profile'
            data.pop('api_key_env', None)
        elif env_name and (self._environment_edited or env_name != self._profile.get('api_key_env') or not data.get('api_key')):
            data['api_key_source'] = 'environment'
            if self._environment_edited or env_name != self._profile.get('api_key_env'):
                data.pop('api_key', None)
        elif not env_name and self._environment_edited and data.get('api_key_source') == 'environment':
            data['api_key_source'] = 'profile'
        if data['model'] and not any(model['id'] == data['model'] for model in data['models']):
            data['models'].append({'id': data['model'], 'name': data['model']})
        model_ids = {item['id'] for item in data['models']}
        options = {model: option for model, option in self._reasoning_by_model.items() if model in model_ids}
        if options:
            data['model_reasoning'] = options
        else:
            data.pop('model_reasoning', None)
        if not require_model:
            data['model']='';data['models']=[];data.pop('model_reasoning',None)
        _validate_profile(data, require_model=require_model, allow_ambient=bool(self._profile))
        if self._requires_credential_consent() and not self.reuse_key_check.isChecked():
            raise ValueError('服务地址的域名、端口或 HTTP/HTTPS 方式已改变；请重新填写 Key、选择新的 Key 环境变量，或勾选确认将当前 Key 用于新服务。')
        if require_model and data['model'] and self._model_context and self._model_context != current:
            raise ValueError('当前模型来自先前连接；请获取新目录，或点击“添加当前模型”确认使用此模型 ID。')
        primary, _ = self._active_credential_ids()
        bound_origin = self._active_credential_origin()
        current_origin = _origin(self.base_url_edit.text().strip())
        if bound_origin is None or (bound_origin != current_origin and self.reuse_key_check.isChecked()):
            if primary is not None:
                self._credential_origins[primary] = current_origin
            self.reuse_key_check.setChecked(False)
            self.reuse_key_check.hide()
        return data

    def save_connection(self):
        if self.task or self._closing:
            return
        try:
            data = self._connection_data()
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return
        self._persist_connection(data)

    def _persist_connection(self, data):
        try:
            destination = _write_profile(data, self.output_dir)
        except OSError:
            self.status_label.setText('无法保存连接，请检查输出目录的写入权限。')
            return
        self.result_data = {'config': str(destination), 'model': data['model'], 'protocol': data['protocol']}
        self.api_key_edit.clear()
        self.accept()

    def _start_request(self, action, success, message):
        if self.task or self._closing:
            return
        self.form_widget.setEnabled(False)
        self.test_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.test_save_button.setEnabled(False)
        self.status_label.setText(message)
        self.task = Task(action, self)
        self.task.succeeded.connect(success, Qt.ConnectionType.QueuedConnection)
        self.task.failed.connect(self._request_failed, Qt.ConnectionType.QueuedConnection)
        self.task.finished.connect(self._request_finished, Qt.ConnectionType.QueuedConnection)
        self.task.start()

    def fetch_models(self):
        if self.task or self._closing:
            return
        try:
            data = self._connection_data(require_model=False)
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return
        catalog_context = self._current_catalog_context()
        self._start_request(lambda control: discover_models(data),
                            lambda result: self._choose_models(result, catalog_context), '正在获取服务公布的模型目录…')

    def manage_models(self):
        if self.task or self._closing:
            return
        context = self._current_catalog_context()
        if context != (self._catalog_key, self._catalog_credential):
            self.status_label.setText('连接已改变，请获取新目录或手动确认当前模型。')
            return
        self._choose_models({'models': self._models(), 'partial': False}, context)

    def _choose_models(self, result, catalog_context):
        if self._closing:
            return
        if not result['models']:
            self._models_received(result, catalog_context)
            return
        same = (self._catalog_key, self._catalog_credential) == catalog_context
        configured = list(self._models()) if same else []
        selected = self._selected_model()
        if (selected and self._model_context == catalog_context
                and not any(model['id'] == selected for model in configured)):
            record = model_record(selected, secret=self.api_key_edit.text().strip() or self._profile.get('api_key', ''))
            if record:
                configured.append(record)
        picker = self.catalog_picker = ModelCatalogDialog(result['models'], configured,
                                                          partial=result['partial'], parent=self)
        self._picker_existing = configured
        self._picker_context = catalog_context
        self._picker_selected = selected
        picker.accepted.connect(self._apply_catalog)
        picker.finished.connect(self._catalog_finished)
        picker.open()

    def _apply_catalog(self):
        picker = self.catalog_picker
        catalog_context = self._picker_context
        if picker is None or self._closing or catalog_context != self._current_catalog_context():
            return
        chosen_models=picker.selected_models()
        identities={item['id'] for item in chosen_models}
        old_ids={item['id'] for item in self._picker_existing}
        models=[item for item in self._picker_existing if not item['id'] or item['id'] in identities]+[item for item in chosen_models if item['id'] not in old_ids]
        selected = self._picker_selected
        chosen = selected if selected in {model['id'] for model in models} else ''
        self._set_models(models, chosen)
        self._catalog_key, self._catalog_credential = catalog_context
        self._model_context = catalog_context
        self._model_explicit = False
        self._catalog_outdated = False
        self.catalog_status.setText(f'已选择 {len(models)} 个模型；保存连接后可在 AI 入口切换。')
        self.status_label.setText('选择本次默认模型，然后保存连接。' if models else '尚未选择模型，可重新获取或手动填写模型 ID。')

    def _catalog_finished(self, result):
        picker, self.catalog_picker = self.catalog_picker, None
        if picker is not None:
            picker.deleteLater()

    def _models_received(self, result, catalog_context):
        if self._closing:
            return
        selected = self._selected_model()
        existing = self._models()
        changed_service = (self._catalog_key, self._catalog_credential) != catalog_context
        if (changed_service and self._model_context != catalog_context
                and selected not in {model['id'] for model in result['models']}):
            selected = ''
        if not result['models']:
            if changed_service:
                self._set_models([], selected)
                self._catalog_key, self._catalog_credential = catalog_context
                self._model_context = catalog_context
                self._model_explicit = bool(selected)
                self._catalog_outdated = False
                self.catalog_status.setText('新服务返回空目录；原服务模型未沿用，可手动填写模型 ID。')
            else:
                self.catalog_status.setText('服务返回空目录。可保留当前模型，或手动添加模型 ID。')
            self.status_label.setText('目录已返回，未发现可选模型。')
            return
        models = {} if changed_service else {model['id']: model for model in existing}
        for model in result['models']:
            if len(models) < MAX_MODELS or model['id'] in models:
                models.setdefault(model['id'], model)
        self._set_models(list(models.values()), selected)
        self._catalog_key, self._catalog_credential = catalog_context
        self._model_context = catalog_context
        self._model_explicit = bool(selected and selected not in {model['id'] for model in result['models']})
        self._catalog_outdated = False
        partial = ' 当前只显示部分目录，其余模型可手动添加。' if result['partial'] or len(models) == MAX_MODELS else ''
        self.catalog_status.setText(f"获取到 {len(result['models'])} 个模型，可输入名称搜索或展开点选。" + partial)
        self.status_label.setText('模型目录已获取。选中模型后点击“测试连接”确认实际可用。')

    def test_connection(self):
        if self.task or self._closing:
            return
        try:
            data = self._connection_data()
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return
        self._start_request(lambda control: _test_profile(data), self._test_succeeded, '正在发送固定公开测试请求…')

    def test_and_save(self):
        if self.task or self._closing:
            return
        try:
            data = self._connection_data()
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return
        self._tested_save_data = None
        def succeeded(result):
            if not self._closing:
                self._tested_save_data = data
        self._start_request(lambda control: _test_profile(data), succeeded,
                            '正在测试所选模型，通过后保存连接…')

    def _test_succeeded(self, result):
        if not self._closing:
            self.status_label.setText('连接成功，所选模型返回了完整测试结果，可以保存连接。')

    def _request_failed(self, error):
        if not self._closing:
            self.status_label.setText(error)

    def _request_finished(self):
        task, self.task = self.task, None
        task.deleteLater()
        if self._closing:
            super().reject()
            return
        self.form_widget.setEnabled(True)
        self.test_button.setEnabled(True)
        self.save_button.setEnabled(True)
        self.test_save_button.setEnabled(True)
        data, self._tested_save_data = self._tested_save_data, None
        if data is not None:
            self._persist_connection(data)

    def reject(self):
        if self.catalog_picker is not None:
            self.catalog_picker.reject()
        if self.task:
            self._closing = True
            self.task.control.cancel()
            self.buttons.setEnabled(False)
            self.status_label.setText('正在等待请求结束，随后关闭…')
        else:
            super().reject()

    def closeEvent(self, event):
        if self.task:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
