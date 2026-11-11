import hashlib
import json
from pathlib import Path
import shutil
import time

import pytest
pytest.importorskip('PySide6')
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog

from reprolab.desktop_processing_history import ProcessingHistoryDialog
from reprolab.processing_history import read_processing_history
from reprolab.project_history import BUNDLED_SOURCES
from test_desktop_ui import window
from test_desktop_usability import wait_project
from test_processing_history import chain, THREE


def wait(predicate, seconds=20):
    end=time.monotonic()+seconds
    while not predicate() and time.monotonic()<end:
        QApplication.processEvents();QTest.qWait(10);time.sleep(.003)
    assert predicate(),'等待处理记录窗口超时'
    QApplication.processEvents()


def open_history(window, report):
    bench=window.project_workbench
    wait_project(bench)
    window.navigation_buttons[bench].click()
    bench._apply_report(report)
    bench.tabs.setCurrentIndex(2)
    bench.results_scroll.ensureWidgetVisible(bench.processing_history_button)
    QTest.mouseClick(bench.processing_history_button,Qt.MouseButton.LeftButton)
    wait(lambda:bench.task is None and bench.processing_dialog is not None)
    return bench,bench.processing_dialog


def test_desktop_handoff_opens_bundled_rounds_from_report_catalog(window, chain, tmp_path, monkeypatch):
    bench = window.project_workbench
    wait_project(bench)
    window.navigation_buttons[bench].click()
    bench._apply_report(chain[-1])
    bench.tabs.setCurrentIndex(2)
    wait(lambda: bench.task is None and bench.review_progress is not None)
    bundle = tmp_path / 'history.zip'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a, **kw: (str(bundle), ''))
    bench.export_sources_action.trigger()
    wait(lambda: bench.task is None and bundle.is_file())
    assert '已导出 3 份' in bench.status_label.text()
    # An old local preference must not hide the verified sources in the ZIP.
    reference = chain[-1]['review_source']
    window.store.settings['project_report_locations'] = {reference['report_sha256']: str(tmp_path / 'absent')}
    window.import_bundle_path(bundle)
    wait(lambda: bench.task is None and bench.review_progress is not None)
    assert Path(bench.report['report_dir']).is_relative_to(window.store.settings['imports'])
    assert bench.review_progress['status'] == 'compared'
    bench.processing_history_button.click()
    wait(lambda: bench.task is None and bench.processing_dialog is not None)
    assert bench.processing_dialog.data['status'] == 'complete'
    assert bench.processing_dialog.rounds_tree.topLevelItemCount() == 3
    bench.processing_dialog.reject()
    imported_source = Path(bench.report['report_dir']) / BUNDLED_SOURCES / reference['report_sha256']
    (imported_source / 'before.log').write_text('changed evidence', encoding='utf-8')
    bench.compare_source_report(show_dialog=False)
    wait(lambda: bench.task is None and bench.review_progress is not None)
    assert bench.review_progress['status'] == 'unavailable'
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *a, **kw: chain[1]['report_dir'])
    bench.review_relocate_button.click()
    wait(lambda: bench.task is None and bench.review_progress is not None)
    assert bench.review_progress['status'] == 'compared'
    bench.compare_source_report(show_dialog=False)
    wait(lambda: bench.task is None and bench.review_progress is not None)
    assert bench.review_progress['status'] == 'compared'


def test_normal_entry_shows_long_omissions_and_saved_notes_are_copied_exported_and_reopened(window,chain,tmp_path,monkeypatch):
    original=(Path(chain[-1]['report_dir'])/'report.json').read_bytes()
    bench,dialog=open_history(window,chain[-1])
    assert dialog.rounds_tree.topLevelItemCount()==3
    assert '本轮未复查 1 项' in dialog.summary.text()
    dialog.tabs.setCurrentIndex(1)
    dialog.filter.setCurrentIndex(dialog.filter.findData('omitted'))
    visible=[dialog.tests_tree.topLevelItem(i) for i in range(dialog.tests_tree.topLevelItemCount()) if not dialog.tests_tree.topLevelItem(i).isHidden()]
    assert len(visible)==1 and visible[0].text(0)==THREE and visible[0].text(3)=='第 1 轮'
    dialog.grab().save(str(tmp_path/'omitted-tests.png'))
    dialog.tabs.setCurrentIndex(0)
    dialog.note_edit.setPlainText('第二项已修复；第三项尚未复查。')
    dialog.save_note_button.click();wait(lambda:dialog.task is None)
    dialog.copy_button.click()
    assert '第二项已修复' in QApplication.clipboard().text()
    destination=tmp_path/'processing-history.json'
    monkeypatch.setattr(QFileDialog,'getSaveFileName',lambda *args,**kwargs:(str(destination),''))
    dialog.export_button.click()
    assert json.loads(destination.read_text())['rounds'][-1]['note']['text']=='第二项已修复；第三项尚未复查。'
    dialog.grab().save(str(tmp_path/'rounds-and-note.png'))
    dialog.reject();QApplication.processEvents()
    bench.processing_history_button.click()
    wait(lambda:bench.task is None and bench.processing_dialog is not None and bench.processing_dialog.isVisible())
    reopened=bench.processing_dialog
    assert reopened.note_edit.toPlainText()=='第二项已修复；第三项尚未复查。'
    assert (Path(chain[-1]['report_dir'])/'report.json').read_bytes()==original
    reopened.reject()


def test_unsaved_note_can_cancel_switch_and_application_close(window,chain,tmp_path,monkeypatch):
    bench,dialog=open_history(window,chain[-1])
    try:
        dialog.note_edit.setPlainText('不要丢失这份草稿')
        monkeypatch.setattr(dialog,'_confirm_discard',lambda:False)
        dialog.rounds_tree.setCurrentItem(dialog.rounds_tree.topLevelItem(0))
        assert dialog._active_round['number']==3 and dialog.note_edit.toPlainText()=='不要丢失这份草稿'
        dialog.reject()
        assert dialog.isVisible()
        window.close();QApplication.processEvents()
        assert window.isVisible() and not bench._closing
    finally:
        monkeypatch.setattr(dialog,'_confirm_discard',lambda:True)
        dialog.reject();QApplication.processEvents()


def test_missing_source_relocation_verifies_identity_and_remembers_location(window,chain,tmp_path,monkeypatch):
    ref=chain[-1]['review_source']
    window.store.settings['project_report_locations']={ref['report_sha256']:str(tmp_path/'missing')}
    bench,dialog=open_history(window,chain[-1])
    assert dialog.data['status']=='partial' and dialog.relocate_button.isEnabled()
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *args,**kwargs:chain[0]['report_dir'])
    dialog.relocate_button.click();wait(lambda:dialog.task is None)
    assert dialog.data['status']=='partial' and '身份不符' in dialog.status_label.text()
    moved=tmp_path/'moved';shutil.copytree(chain[1]['report_dir'],moved)
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *args,**kwargs:str(moved))
    dialog.relocate_button.click();wait(lambda:dialog.task is None)
    assert dialog.data['status']=='complete' and len(dialog.data['rounds'])==3
    assert window.store.settings['project_report_locations'][ref['report_sha256']]==str(moved)
    dialog.reject()



def test_closing_history_cancels_an_in_progress_read_and_releases_the_dialog(window, chain, monkeypatch):
    import reprolab.desktop_processing_history as module
    bench, dialog = open_history(window, chain[-1])
    started = []
    def delayed(*args, control=None, **kwargs):
        started.append(True)
        while True:
            control.check()
            time.sleep(.01)
    monkeypatch.setattr(module, 'read_processing_history', delayed)
    dialog.reload_button.click()
    wait(lambda: bool(started))
    dialog.reject()
    wait(lambda: bench.processing_dialog is None)
    assert not bench.busy
