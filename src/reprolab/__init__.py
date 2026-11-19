"""Local before/after regression verification."""

__version__ = "0.19.0a24"

from .runner import run_case

__all__ = ["run_case", "__version__"]


def source_examples_available():
    from pathlib import Path

    return (Path(__file__).parent / 'demo_case' / 'case.json').is_file()
