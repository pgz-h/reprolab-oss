"""Create independent, offline human revisions of fixed AI acceptance plans."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

from . import repair_suite as suite
from .drafts import draft_lock, _read_bytes
from .persistence import save_json, atomic_write
from .runner import source_fingerprint, source_mode_fingerprint, snapshot


def editable_plan(directory):
    """Return the reviewable fields without generated paths or fingerprints."""
    state = suite.read_suite(directory)
    plan = state['plan']
    data = {key: deepcopy(plan[key]) for key in ('title', 'overview')}
    data['checks'] = [{key: deepcopy(check[key]) for key in suite.CHECK_FIELDS} for check in plan['checks']]
    return suite._decode_plan(json.dumps(data, ensure_ascii=False), Path(state['directory']), state)


def revise_suite(directory, plan, output_root=None):
    """Copy the frozen inputs and publish a separately identified reviewed plan."""
    with draft_lock(directory):
        parent = suite.read_suite(directory)
        source = Path(parent['directory'])
        raw = json.dumps(plan, ensure_ascii=False)
        reviewed = suite._decode_plan(raw, source, parent)
        if reviewed == editable_plan(source):
            raise ValueError('计划没有变化，请先修改需要审阅的检查。')
        output = Path(output_root).expanduser().resolve() if output_root is not None else source.parent
        if output.is_relative_to(source) or output.is_relative_to(Path(parent['repository']).resolve()):
            raise ValueError('审阅版本必须保存在原计划和被测仓库之外。')
        output.mkdir(parents=True, exist_ok=True)
        identity = suite._new_id('suite')
        final = output / identity
        reviewed_at = datetime.now(timezone.utc).isoformat()
        revision = {'parent_suite_id': parent['suite_id'], 'parent_plan_sha256': parent['plan_sha256'],
                    'reviewed_at': reviewed_at}
        origin = {'provider': 'human-review', 'model': '人工审阅', **revision}
        with tempfile.TemporaryDirectory(prefix='.suite-review-', dir=output) as temp:
            folder = Path(temp) / identity
            folder.mkdir()
            (folder / 'inputs').mkdir()
            for label in ('before', 'after'):
                original = source / 'inputs' / label
                entries, digest = source_fingerprint(original)
                modes = source_mode_fingerprint(original, entries)
                copied = folder / 'inputs' / label
                if (digest != parent['source_sha256'][label]
                        or snapshot(original, copied, entries) != digest
                        or source_mode_fingerprint(copied) != modes):
                    raise ValueError('修订复制期间源码或权限发生变化，未保存新计划。')
            for name in ('context.json', 'prompt.txt'):
                atomic_write(folder / name, _read_bytes(source / name, limit=2 * 1024 * 1024))
            state = {key: deepcopy(value) for key, value in parent.items()
                     if key not in ('plan', 'plan_sha256', 'error', 'title', 'recovery_artifacts')}
            state.update(suite_id=identity, directory=str(final), status='prepared', revision=revision,
                         notice='人工审阅版本已另存，尚未执行验收。')
            save_json(folder / 'suite.json', state)
            suite._checkpoint_suite_response(folder, state, raw, origin)
            suite._materialize_suite(folder, state, raw, origin)
            # Check the original again after all copies and derived cases exist.
            current = suite.read_suite(source)
            if (current['plan_sha256'] != parent['plan_sha256']
                    or suite._suite_input_id(current) != suite._suite_input_id(parent)):
                raise ValueError('修订期间原计划发生变化，未保存新计划。')
            suite.read_suite(folder)
            folder.rename(final)
        return suite.read_suite(final)
