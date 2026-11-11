"""Check the frozen runtime before it is packaged for distribution."""
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader


def verify_desktop_payload(executable):
    archive = CArchiveReader(str(executable))
    modules = archive.open_embedded_archive('PYZ.pyz').toc
    qa_names = {path.stem for path in Path(__file__).parent.glob('*_smoke.py')}
    bundled_qa = qa_names.intersection(modules)
    bundled_qa.update(name for name in archive.toc if Path(name).stem in qa_names)
    if bundled_qa:
        raise RuntimeError('Desktop release contains development QA modules: ' + ', '.join(sorted(bundled_qa)))
