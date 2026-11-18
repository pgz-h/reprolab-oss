"""Check the frozen runtime before it is packaged for distribution."""
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader


def verify_desktop_payload(executable):
    archive = CArchiveReader(str(executable))
    modules = archive.open_embedded_archive('PYZ.pyz').toc
    qa_names = {path.stem for path in Path(__file__).parent.glob('*_smoke.py')}
    bundled_qa = qa_names.intersection(modules)
    bundled_qa.update(name for name in archive.toc if Path(name).stem in qa_names)
    bundled_qa.update(n for n in modules if n == 'reprolab.project_demo')
    root = Path(executable).parent
    if root.name == 'MacOS':
        root = root.parent
    forbidden = {'demo_case', 'demo_recheck', 'history_cases'}
    bundled_qa.update(str(p.relative_to(root)) for p in root.rglob('*') if p.name in forbidden)
    if bundled_qa:
        raise RuntimeError('Desktop release contains development QA modules: ' + ', '.join(sorted(bundled_qa)))
