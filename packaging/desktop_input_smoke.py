"""Verify macOS activation and typed input through the normal connection entry."""
import argparse
import ctypes
import json
from pathlib import Path
import sys
import time
import traceback

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit

from reprolab.desktop import MainWindow
from reprolab.desktop_models import ModelConnectionDialog
from reprolab.desktop_store import DesktopStore


def activation_policy():
    """Read AppKit's policy for this process, without posting system events."""
    ctypes.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit')
    objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    pointer = ctypes.c_void_p
    send = ctypes.CFUNCTYPE(pointer, pointer, pointer)(('objc_msgSend', objc))
    read_integer = ctypes.CFUNCTYPE(ctypes.c_long, pointer, pointer)(('objc_msgSend', objc))
    app = send(objc.objc_getClass(b'NSApplication'), objc.sel_registerName(b'sharedApplication'))
    return read_integer(app, objc.sel_registerName(b'activationPolicy'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    root = args.directory.absolute()
    root.mkdir(parents=True, exist_ok=False)
    app = QApplication(sys.argv[:1])
    store = DesktopStore(root / 'state')
    store.settings.update(cases=str(root/'cases'), drafts=str(root/'drafts'),
                          reports=str(root/'reports'), imports=str(root/'imports'),
                          python='', model_config='')
    window = MainWindow(store)
    window.show()
    result = {'frozen': bool(getattr(sys, 'frozen', False)), 'checks': []}
    dialog_errors = []

    def type_text(edit, text):
        assert edit.isEnabled() and not edit.isReadOnly()
        QTest.mouseClick(edit, Qt.MouseButton.LeftButton)
        focus = app.focusWidget()
        # An editable QComboBox owns focus and forwards keys to its line edit.
        assert focus is edit or (focus is edit.parentWidget() and isinstance(focus, QComboBox))
        QTest.keyClicks(app.focusWidget(), text)
        assert edit.text() == text

    def exercise_dialog():
        dialog = app.activeModalWidget()
        try:
            assert isinstance(dialog, ModelConnectionDialog)
            assert QTest.qWaitForWindowActive(dialog, 5000)
            assert activation_policy() == 0
            type_text(dialog.display_name_edit, 'Typed service')
            # Exercise the input-method commit path separately from ASCII keys.
            QTest.keyClick(dialog.display_name_edit, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            event = QInputMethodEvent()
            event.setCommitString('键盘输入验证')
            app.sendEvent(app.focusWidget(), event)
            assert dialog.display_name_edit.text() == '键盘输入验证'
            QTest.keyClick(dialog.display_name_edit, Qt.Key.Key_Tab)
            assert app.focusWidget() is dialog.base_url_edit
            type_text(dialog.base_url_edit, 'https://example.invalid/v1')
            type_text(dialog.api_key_edit, 'synthetic-keyboard-key')
            assert dialog.api_key_edit.echoMode() == QLineEdit.EchoMode.Password
            type_text(dialog.model_edit, 'typed-model')
            dialog.grab().save(str(root / 'typed-connection.png'))
            result['checks'].extend(['normal_settings_entry_opens_active_modal',
                                      'mouse_focus_and_tab_navigation',
                                      'ascii_keyboard_events_and_chinese_input_method_commit',
                                      'name_url_key_and_model_accept_typed_input'])
            dialog.save_button.click()
            assert dialog.result_data['model'] == 'typed-model'
            result['checks'].append('typed_profile_saves_through_normal_entry')
        except Exception:
            dialog_errors.append(traceback.format_exc())
            if dialog:
                dialog.reject()

    try:
        deadline = time.monotonic() + 20
        while window.task is not None and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
        assert window.task is None
        result['activation_policy'] = activation_policy()
        assert result['activation_policy'] == 0, 'macOS must allow a regular foreground application'
        window.raise_()
        window.activateWindow()
        assert QTest.qWaitForWindowActive(window, 5000)
        result['checks'].append('macos_regular_activation_policy_and_active_main_window')
        window.tabs.setCurrentWidget(window.settings_page)
        QTimer.singleShot(50, exercise_dialog)
        window.model_connection_button.click()
        assert not dialog_errors, '\n'.join(dialog_errors)
        assert Path(store.settings['model_config']).is_file()
        result['ok'] = True
    except Exception:
        result.update(ok=False, error=traceback.format_exc())
    finally:
        window.close()
    (root/'input-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
