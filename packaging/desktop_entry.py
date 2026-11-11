"""Frozen dispatch: worker imports no Qt and never opens a second window."""
import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--reprolab-process-gate":
        del sys.argv[1]
        from reprolab._process_gate import main
    elif len(sys.argv) > 1 and sys.argv[1] == "--reprolab-worker":
        del sys.argv[1]
        from reprolab._worker import main
    elif len(sys.argv) > 1 and sys.argv[1] == '--reprolab-project-worker':
        del sys.argv[1]
        from reprolab._project_worker import main
    elif len(sys.argv) > 1 and sys.argv[1] == "--cli":
        del sys.argv[1]
        from reprolab.cli import main
    elif len(sys.argv) > 1 and sys.argv[1] == "--input-smoke":
        del sys.argv[1]
        from desktop_input_smoke import main
    elif len(sys.argv) > 1 and sys.argv[1] == '--project-smoke':
        del sys.argv[1]
        from project_workbench_smoke import main
    elif len(sys.argv) > 1 and sys.argv[1] == "--suite-smoke":
        del sys.argv[1]
        from desktop_suite_smoke import main
    elif len(sys.argv) > 1 and sys.argv[1] == "--probe-smoke":
        del sys.argv[1]
        from desktop_probe_smoke import main
    elif len(sys.argv) > 1 and sys.argv[1] == "--workspace-smoke":
        del sys.argv[1]
        from desktop_workspace_smoke import main
    elif len(sys.argv) > 1 and sys.argv[1] == "--reliability-smoke":
        del sys.argv[1]
        from desktop_reliability_smoke import main
    elif len(sys.argv) > 1 and sys.argv[1] == "--runtime-smoke":
        del sys.argv[1]
        from desktop_runtime_smoke import main
    elif len(sys.argv) > 1 and sys.argv[1] == "--onboarding-smoke":
        del sys.argv[1]
        from desktop_onboarding_smoke import main
    elif len(sys.argv) > 1 and sys.argv[1] == "--history-smoke":
        del sys.argv[1]
        from desktop_history_smoke import main
    elif len(sys.argv) > 1 and sys.argv[1] == "--config-smoke":
        del sys.argv[1]
        from desktop_config_smoke import main
    elif len(sys.argv) > 1 and sys.argv[1] == "--progress-smoke":
        del sys.argv[1]
        from desktop_progress_smoke import main
    elif len(sys.argv) > 1 and sys.argv[1] == "--diagnostics-smoke":
        del sys.argv[1]
        from desktop_diagnostics_smoke import main
    elif len(sys.argv) > 1 and sys.argv[1] == "--workflow-smoke":
        del sys.argv[1]
        from desktop_workflow_smoke import main
    elif len(sys.argv) > 1 and sys.argv[1] == "--adaptation-smoke":
        del sys.argv[1]
        from desktop_adaptation_smoke import main
    elif len(sys.argv) > 1 and sys.argv[1] == "--repair-smoke":
        del sys.argv[1]
        from desktop_repair_smoke import main
    elif len(sys.argv) > 1 and sys.argv[1] == "--desktop-smoke":
        del sys.argv[1]
        from desktop_smoke import main
    else:
        from reprolab.desktop import main
    raise SystemExit(main())
