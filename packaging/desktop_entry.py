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
    else:
        from reprolab.desktop import main
    raise SystemExit(main())
