"""Explicit multi-case execution with a responsive queue and durable summaries."""
from pathlib import Path
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QTreeWidgetItem, QProgressBar, QLineEdit, QFileDialog

from .batch import run_selected_cases, SUCCESS_STATUSES
from .desktop_tasks import Task
from .desktop_store import input_stamp, read_json, LABELS
from .retry import plan_retry, run_retry
from .environment import read_report_environment


class BatchDialog(QDialog):
    def __init__(self, store, paths, *, python_executable="", timeout=15, parent=None, retry_plan=None, suite_directory=None, suite_ids=None):
        super().__init__(parent)
        from .desktop import label, button, row, tree, text_view
        self.store = store
        self.retry_plan = retry_plan
        self.suite_directory = suite_directory
        self.suite_ids = suite_ids
        self.paths = list(dict.fromkeys(str(Path(p).absolute()) for p in paths))
        self.python_executable = python_executable or sys.executable
        self.timeout = timeout
        self.task = None
        self.result_data = None
        self.summary_dir = None
        self._close_after = False
        self.setWindowTitle("批量验收 · 按顺序运行选中的案例")
        self.resize(980, 680)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.addWidget(label(f"本批次共 {len(self.paths)} 个案例。每个案例仍只运行一份测试；单项错误后继续其他案例。"))
        self.python_edit = QLineEdit(python_executable)
        self.python_edit.setPlaceholderText("应用内置 Python；需要项目依赖时选择对应环境")
        self.python_button = button("选择 Python…", self.choose_python)
        layout.addWidget(row(label("本批次环境"), self.python_edit, self.python_button))
        if retry_plan:
            layout.addWidget(label("来源批次：" + retry_plan["source_batch_id"] + "\n" + retry_plan["notice"]))
        self.queue = tree(["案例", "状态", "说明"])
        self.queue.currentItemChanged.connect(self.show_details)
        self.items = {}
        for path in self.paths:
            try:
                title = read_json(path).get("title", Path(path).stem)
            except (OSError, ValueError):
                title = Path(path).stem
            item = QTreeWidgetItem([title, "等待开始", "尚未执行"])
            item.setData(0, Qt.ItemDataRole.UserRole, {"file": path})
            self.queue.addTopLevelItem(item)
            self.items[path] = item
        layout.addWidget(self.queue, 1)
        self.details = text_view()
        self.details.setMaximumHeight(140)
        layout.addWidget(self.details)
        self.status_label = label("请确认清单，再开始。本地代码需要可信；不会调用模型。")
        layout.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, len(self.paths))
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.start_button = button("开始批次", self.start, True)
        self.cancel_button = button("取消批次", self.cancel)
        self.cancel_button.setEnabled(False)
        self.open_button = button("打开批次总览", self.open_summary)
        self.open_button.setEnabled(False)
        self.retry_button = button("重跑未通过项…", lambda: self.retry("not_verified"))
        self.resume_button = button("继续未完成项…", lambda: self.retry("unfinished"))
        self.retry_button.setVisible(False)
        self.resume_button.setVisible(False)
        self.close_button = button("关闭", self.reject)
        layout.addWidget(row(self.start_button, self.cancel_button, self.open_button,
                             self.retry_button, self.resume_button, self.close_button))

    def show_details(self, current, previous=None):
        if current and hasattr(self, "details"):
            entry = current.data(0, Qt.ItemDataRole.UserRole)
            lines = [entry.get("explanation", "等待运行。")]
            lines += ["下一步：" + step for step in entry.get("diagnosis", {}).get("next_steps", [])[:2]]
            comparison = entry.get("environment_comparison") or {}
            if comparison.get("status") == "changed":
                lines.append("环境提醒：" + comparison.get("notice", ""))
            lines.append(entry["file"])
            self.details.setPlainText("\n".join(lines))

    def start(self):
        if self.task or self.result_data:
            return
        if not self.paths or len(self.paths) > 50:
            self.status_label.setText("请勾选 1–50 个案例。")
            return
        output = self.store.settings["reports"]
        self.python_executable = self.python_edit.text().strip() or sys.executable
        self.store.settings["python"] = self.python_edit.text().strip()
        references = {}
        for path in self.paths:
            old = self.store.activity.get(path, {})
            references[path] = old.get("report") or old.get("reference_report")
            self.store.activity[path] = {"status": "pending", "reference_report": references[path]}
        try:
            self.store.save()
        except OSError as exc:
            self.status_label.setText("无法保存批次活动记录：" + str(exc))
            return
        self.start_button.setEnabled(False)
        self.python_edit.setEnabled(False)
        self.python_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status_label.setText("正在检查案例，准备批次…")
        def action(control):
            stamps = {}
            for path in self.paths:
                try:
                    stamps[path] = input_stamp(path)
                except (OSError, ValueError, KeyError, TypeError):
                    stamps[path] = None
            if self.suite_directory:
                from .repair_suite import run_suite
                summary = run_suite(self.suite_directory, output, confirmed=True, selected_ids=self.suite_ids,
                                    timeout=self.timeout, python_executable=self.python_executable, control=control)
            elif self.retry_plan:
                summary = run_retry(self.retry_plan, output, timeout=self.timeout,
                                    python_executable=self.python_executable, control=control)
            else:
                previous = {path: read_report_environment(report) for path, report in references.items()}
                summary = run_selected_cases(self.paths, output, timeout=self.timeout,
                                             python_executable=self.python_executable, control=control,
                                             previous_environments=previous)
            for path, stamp in list(stamps.items()):
                try:
                    if input_stamp(path) != stamp:
                        stamps[path] = None
                except (OSError, ValueError, KeyError, TypeError):
                    stamps[path] = None
            return summary, stamps
        self.task = Task(action, self)
        self.task.progress.connect(self.on_progress)
        self.task.succeeded.connect(self.completed)
        self.task.failed.connect(self.failed)
        self.task.cancelled.connect(lambda: self.failed("批次已取消，没有新的验收结论。"))
        self.task.finished.connect(self._task_finished)
        self.task.start()

    def on_progress(self, phase, payload):
        if phase == "batch_start":
            self.summary_dir = payload["summary_dir"]
        elif phase in {"batch_case_start", "batch_case_done"}:
            entry = payload["entry"]
            status = "running" if phase == "batch_case_start" else entry["status"]
            item = self.items.get(entry["file"])
            if item:
                item.setText(1, LABELS.get(status, status))
                item.setText(2, entry.get("explanation", "") if status != "running" else "正在运行前后版本")
                shown = dict(entry)
                if status == "running":
                    shown["explanation"] = "正在独立副本中运行修复前后版本。"
                item.setData(0, Qt.ItemDataRole.UserRole, shown)
                self.queue.setCurrentItem(item)
                self.show_details(item)
            old = self.store.activity.get(entry["file"], {})
            self.store.activity[entry["file"]] = {"status": status, "reference_report": old.get("report") or old.get("reference_report")}
            if entry.get("report") and self.summary_dir:
                self.store.activity[entry["file"]]["report"] = str((Path(self.summary_dir) / entry["report"]).parent)
            try:
                self.store.save()
            except OSError:
                self.status_label.setText("活动记录暂时无法保存；已生成报告仍保留在输出目录。")
            self.status_label.setText(f"正在处理 {payload['index']} / {payload['total']}：{entry['title']}")
            if phase == "batch_case_done":
                self.progress.setValue(payload["index"])
        elif phase == "batch_progress":
            label = {"preflight": "预检输入与环境", "before": "运行旧版", "after": "运行新版", "preparing": "准备副本",
                     "before_done": "旧版已结束", "after_done": "新版已结束", "complete": "保存单项结果"}
            self.status_label.setText(f"{payload['index']} / {payload['total']} · {label.get(payload['phase'], '正在处理')}")
        elif phase == "batch_done":
            self.cancel_button.setEnabled(False)

    def completed(self, payload):
        summary, stamps = payload
        self.result_data = summary
        self.summary_dir = summary["summary_dir"]
        for entry in summary["entries"]:
            path = entry["file"]
            item = self.items.get(path)
            if item:
                item.setText(1, LABELS.get(entry["status"], entry["status"]))
                item.setText(2, entry["explanation"])
                item.setData(0, Qt.ItemDataRole.UserRole, dict(entry))
            old = self.store.activity.get(path, {})
            record = {"reference_report": old.get("report") or old.get("reference_report"), "status": entry["status"], "stamp": stamps.get(path), "at": summary["updated_at"]}
            if entry.get("report"):
                record["report"] = str((Path(summary["summary_dir"]) / entry["report"]).parent)
            self.store.activity[path] = record
        title = {"checks_passed": "所选回归检查通过", "verified": "全部案例验证通过", "attention": "批次完成，有案例需要查看",
                 "cancelled": "批次已取消", "interrupted": "批次未完成"}[summary["status"]]
        self.status_label.setText(f"{title} · 已完成 {summary['completed']} / {summary['total']}，通过 {summary.get('passed', summary['counts']['verified'])}。")
        try:
            self.store.save()
        except OSError:
            self.status_label.setText(self.status_label.text() + " 活动记录保存失败，报告仍在输出目录。")
        self.show_details(self.queue.currentItem())
        self.progress.setValue(summary["completed"])
        self.open_button.setEnabled(True)
        self.start_button.setText("本批次已结束")
        failed = any(e["status"] not in SUCCESS_STATUSES for e in summary["entries"])
        unfinished = any(e["status"] in {"pending", "cancelled"} for e in summary["entries"])
        self.retry_button.setVisible(failed)
        self.resume_button.setVisible(unfinished)

    def failed(self, message):
        self.status_label.setText("未完成：" + message)
        for path in self.paths:
            if self.store.activity.get(path, {}).get("status") == "running":
                self.store.activity[path] = {"status": "interrupted"}
        try:
            self.store.save()
        except OSError:
            pass
        self.open_button.setEnabled(bool(self.summary_dir and (Path(self.summary_dir) / "summary.html").is_file()))
        self.start_button.setEnabled(True)

    def _task_finished(self):
        old = self.task
        self.task = None
        old.deleteLater()
        self.cancel_button.setEnabled(False)
        self.python_edit.setEnabled(True)
        self.python_button.setEnabled(True)
        if self._close_after:
            super().reject()

    def cancel(self):
        if self.task:
            accepted = self.task.control.cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("正在取消当前运行；已完成项会保留。" if accepted else "本批次已完成，正在保存总览。")

    def choose_python(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择项目 Python 环境", self.python_edit.text(),
                                             options=QFileDialog.Option.DontResolveSymlinks)
        if path:
            self.python_edit.setText(path)

    def retry(self, mode):
        if self.task or not self.summary_dir:
            return
        try:
            plan = plan_retry(self.summary_dir, mode=mode)
        except (OSError, ValueError) as exc:
            self.status_label.setText("无法准备重跑：" + str(exc))
            return
        if not plan["paths"]:
            self.status_label.setText("没有当前模式需要处理的案例。")
            return
        dialog = BatchDialog(self.store, plan["paths"], python_executable=self.python_edit.text().strip(),
                             timeout=self.timeout, parent=self, retry_plan=plan)
        dialog.exec()
        self.python_edit.setText(self.store.settings["python"])
        self.status_label.setText("后续操作已结束；当前清单仍是原批次，新结果保存在独立批次中。")

    def open_summary(self):
        if self.summary_dir:
            from .desktop import open_local
            try:
                open_local(Path(self.summary_dir) / "summary.html")
            except (OSError, ValueError) as exc:
                self.status_label.setText(str(exc))

    def reject(self):
        if self.task:
            self._close_after = True
            self.cancel()
            return
        super().reject()

    def closeEvent(self, event):
        if self.task:
            self.reject()
            event.ignore()
        else:
            event.accept()
