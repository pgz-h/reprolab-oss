"""Frozen Git comparisons for small native test runners."""
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from .git_source import excluded_source_path, excluded_tracked_changes, export_revision, resolve_commit
from .persistence import save_json
from .project_archive import _MAX_BYTES, _MAX_ENTRIES, _archive_path, _extract
from .recipes import repository_root
from .runner import source_entries, source_fingerprint, source_mode_fingerprint

SIDES = ('before', 'after')
RUNNERS = ('cargo', 'cmake')
_RECORD = 'native-comparison.json'
_OUTCOMES = {'passed', 'failed', 'skipped', 'blocked'}
_EXIT = {'passed': 0, 'failed': 1, 'blocked': 2}
_RECORD_LIMIT = 16 * 1024 * 1024
_COMMIT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')
_REVIEW_PLAN_FIELDS = {
    'schema_version', 'runner', 'commits', 'selected_tests', 'required_fixes',
    'shared_test_sources', 'shared_test_inputs',
}


def _adapter(runner, *, replay):
    if runner == 'cargo':
        from .native_cargo import read_cargo, run_cargo
        return read_cargo if replay else run_cargo
    if runner == 'cmake':
        from . import native_cmake
        return native_cmake.read_cmake if replay else native_cmake.run_cmake
    raise ValueError('原生执行器须为 cargo 或 cmake。')


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _RECORD_LIMIT:
        raise ValueError('请选择有效的原生比较证据记录。')
    with path.open('rb') as stream:
        raw = stream.read(_RECORD_LIMIT + 1)
    if len(raw) > _RECORD_LIMIT:
        raise ValueError('原生比较证据记录过大。')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('原生比较证据记录需要 JSON 对象。')
    return value


def _targets(selected):
    if selected is None:
        return None
    if (not isinstance(selected, list) or not 1 <= len(selected) <= 500
            or any(not isinstance(name, str) or not name.strip() or len(name) > 1000
                   or any(ord(char) < 32 for char in name) for name in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError('请选择 1–500 个唯一的完整测试名。')
    return selected


def _definitions(paths):
    if (not isinstance(paths, list) or len(paths) > 100
            or any(not isinstance(path, str) or _archive_path(path).as_posix() != path for path in paths)
            or len(set(paths)) != len(paths)):
        raise ValueError('共享测试定义清单须为最多 100 个唯一的仓库相对文件路径。')
    return paths


def _source_record(tree):
    if tree.is_symlink() or not tree.is_dir():
        raise ValueError('固定源码目录不存在或不是普通目录。')
    entries, digest = source_fingerprint(tree)
    return {'sha256': digest, 'mode_sha256': source_mode_fingerprint(tree, entries)}


def _files(tree):
    if tree.is_symlink() or not tree.is_dir():
        raise ValueError('原生运行证据目录无效。')
    files, total = {}, 0
    def fail_walk(error):
        raise error
    for current, dirs, names in os.walk(tree, followlinks=False, onerror=fail_walk):
        directory = Path(current)
        for name in dirs + names:
            path = directory / name
            if path.is_symlink() or not (path.is_dir() if name in dirs else path.is_file()):
                raise ValueError('原生证据须为普通文件和目录。')
        for name in sorted(names):
            path = directory / name
            total += path.stat().st_size
            if len(files) >= _MAX_ENTRIES or total > _MAX_BYTES:
                raise ValueError('原生证据超过归档限制。')
            relative = _archive_path(path.relative_to(tree).as_posix()).as_posix()
            files[relative] = _sha(path)
    return dict(sorted(files.items()))


def _run_facts(result):
    if not isinstance(result, dict) or result.get('status') not in ('completed', 'blocked'):
        raise ValueError('原生执行器返回无效状态。')
    tests = result.get('tests')
    if (not isinstance(tests, dict) or len(tests) > 10000
            or any(not isinstance(name, str) or not name.strip() or len(name) > 1000
                   or any(ord(char) < 32 for char in name) or outcome not in _OUTCOMES
                   for name, outcome in tests.items())):
        raise ValueError('原生测试结果包含无效身份或结果。')
    # Adapters may include structured diagnostic metadata. Keep raw logs bound separately.
    return result


def _definition_record(root, paths):
    records = []
    for path in paths:
        sides = {}
        for side in SIDES:
            tree = root / 'inputs' / side
            source = tree / path
            if source.is_file() and not source.is_symlink() and source.resolve().is_relative_to(tree.resolve()):
                sides[side] = {'sha256': _sha(source), 'mode': stat.S_IMODE(source.stat().st_mode)}
            else:
                sides[side] = None
        records.append({'path': path, **sides, 'matches': sides['before'] is not None and sides['before'] == sides['after']})
    return records


def _compare(runs, definitions, selected, *, runner=None):
    source_status = ('matched' if definitions and all(row['matches'] for row in definitions)
                     else 'changed_or_missing' if definitions else 'undeclared')
    before, after = (runs[side]['tests'] for side in SIDES)
    scope = selected if selected is not None else sorted(before.keys() | after.keys())
    unexpected = {side: sorted(runs[side]['tests'].keys() - set(scope)) for side in SIDES}
    transitions = {('failed', 'passed'): 'improved', ('passed', 'failed'): 'regression_detected',
                   ('passed', 'passed'): 'regression_passed', ('failed', 'failed'): 'baseline_failure'}
    entries, counts = [], {}
    for identity in scope:
        left, right = before.get(identity), after.get(identity)
        shared = source_status == 'matched' and left is not None and right is not None
        configuration = None
        if runner == 'cmake' and left is not None and right is not None:
            settings = []
            for side in SIDES:
                recorded = runs[side].get('test_settings')
                settings.append(recorded.get(identity) if isinstance(recorded, dict) else None)
            if not all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) for value in settings):
                configuration = 'unverified'
            else:
                configuration = 'matched' if settings[0] == settings[1] else 'changed'
            shared = shared and configuration == 'matched'
        if left is None or right is None:
            observation = 'missing_both' if left is right else 'only_after' if left is None else 'only_before'
        elif not shared:
            observation = 'native_only'
        else:
            observation = transitions.get((left, right), 'inconclusive')
        entry = {'test_id': identity, 'before': left, 'after': right, 'shared': shared, 'status': observation}
        if configuration:
            entry['test_configuration'] = configuration
        entries.append(entry)
        counts[observation] = counts.get(observation, 0) + 1
    if counts.get('regression_detected'):
        status = 'failed'
    elif (not scope or source_status != 'matched' or any(unexpected.values())
          or any(runs[side]['status'] != 'completed' for side in SIDES)
          or set(counts) - {'improved', 'regression_passed'}):
        status = 'blocked'
    else:
        status = 'passed'
    return {
        'shared_test_sources': source_status, 'shared_count': sum(row['shared'] for row in entries),
        'unexpected_tests': unexpected, 'counts': counts, 'entries': entries,
        'gate': {'status': status, 'exit_code': _EXIT[status]},
    }


def _snapshot_scope(commits, sources, excluded):
    if type(excluded) is not int or excluded < 0 or (commits['before'] == commits['after'] and excluded):
        raise ValueError('原生证据记录的 Git 遗漏改动数无效。')
    same = sources['before'] == sources['after']
    return {
        'requested': 'git_commit_pair',
        'excluded_tracked_changes': excluded,
        'excluded_changes_only': same and excluded > 0,
        'snapshots_match': same,
        'git_diff_evidence': 'sender_reported',
    }


def _scoped_comparison(runs, definitions, selected, *, runner, scope, inputs, attested):
    comparison = _compare(runs, definitions, selected, runner=runner)
    if scope['snapshots_match']:
        for row in comparison['entries']:
            if {row['before'], row['after']} == {'passed', 'failed'}:
                row['status'] = 'same_snapshot_divergence'
        comparison['counts'] = dict(Counter(row['status'] for row in comparison['entries']))
    if scope['excluded_changes_only'] or comparison['counts'].get('same_snapshot_divergence'):
        comparison['gate'] = {'status': 'blocked', 'exit_code': 2}
    check_files = definitions + inputs
    if any(not row['matches'] for row in check_files):
        coverage = 'changed_or_missing'
    else:
        coverage = 'sender_attested' if attested and definitions else 'unknown'
    comparison['test_input_coverage'] = coverage
    comparison['review_summary'] = (
        '测试结果属于声明范围内的观察。指定修复验收需要 --require-fix 和匹配的测试输入证据。'
    )
    return comparison


def native_fix_acceptance(record, required):
    required = _targets(required)
    comparison = record['comparison']
    scope = record.get('snapshot_scope')
    rows = {row['test_id']: row['status'] for row in comparison['entries']}
    targets = {name: rows.get(name, 'missing') for name in required}
    if comparison['gate']['status'] == 'failed':
        status, reason = 'failed', 'regression_detected'
    elif record['schema_version'] == 1:
        status, reason = 'blocked', 'historical_scope_unknown'
    elif scope['excluded_tracked_changes']:
        status, reason = 'blocked', 'excluded_tracked_changes'
    elif comparison['test_input_coverage'] != 'sender_attested':
        status, reason = 'blocked', 'test_inputs_' + comparison['test_input_coverage']
    elif comparison['gate']['status'] != 'passed':
        status, reason = 'blocked', 'comparison_blocked'
    elif any(outcome != 'improved' for outcome in targets.values()):
        status, reason = 'blocked', 'required_improvement_missing'
    else:
        status, reason = 'passed', 'required_fixes_observed'
    return {'status': status, 'reason': reason, 'targets': targets, 'exit_code': _EXIT[status]}


def _read_review_plan(path):
    if not path.is_file() or path.stat().st_size > _RECORD_LIMIT:
        raise ValueError('请选择有效的原生验收计划 JSON 文件。')
    with path.open('rb') as stream:
        raw = stream.read(_RECORD_LIMIT + 1)
    if len(raw) > _RECORD_LIMIT:
        raise ValueError('原生验收计划文件过大。')

    plan = _parse_plan_json(raw)
    _validate_review_plan(plan)
    return plan, hashlib.sha256(raw).hexdigest()


def _parse_plan_json(raw):
    def unique_keys(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError('原生验收计划或策略的 JSON 对象含有重复键。')
        return result

    return json.loads(raw, object_pairs_hook=unique_keys)


def _validate_review_plan(plan):
    if not isinstance(plan, dict) or type(plan.get('schema_version')) is not int:
        raise ValueError('原生验收计划需指定 schema_version。')
    version = plan['schema_version']
    fields = _REVIEW_PLAN_FIELDS | ({'baseline_failure_markers'} if version == 2 else set())
    if (version not in (1, 2) or plan.keys() - {'pr_context'} != fields
            or plan['runner'] not in RUNNERS or not isinstance(plan['commits'], dict)
            or set(plan['commits']) != set(SIDES)
            or any(not isinstance(commit, str) or not _COMMIT.fullmatch(commit)
                   for commit in plan['commits'].values())):
        raise ValueError('原生验收计划需要受支持的执行器和两端完整 Git 提交。')
    if 'pr_context' in plan:
        pr = plan['pr_context']
        if (not isinstance(pr, dict) or pr.keys() != {'mode', 'base', 'head'}
                or pr['mode'] not in ('head', 'merge')
                or any(not isinstance(pr[key], str) or not _COMMIT.fullmatch(pr[key]) for key in ('base', 'head'))
                or (pr['mode'] == 'head' and pr['head'] != plan['commits']['after'])
                or (pr['mode'] == 'merge' and pr['base'] != plan['commits']['before'])):
            raise ValueError('原生验收计划的 PR 上下文与固定提交不一致。')
    selected = _targets(plan['selected_tests'])
    required = _targets(plan['required_fixes'])
    if not (selected and required and set(required).issubset(selected)):
        raise ValueError('原生验收计划需要非空的选测和必验修复列表，必验修复必须包含在选测中。')
    sources = _definitions(plan['shared_test_sources'])
    inputs = _definitions(plan['shared_test_inputs'])
    if not sources:
        raise ValueError('原生验收计划至少需要一个共同测试定义文件。')
    if any(excluded_source_path(Path(path)) for path in sources + inputs):
        raise ValueError('原生验收计划中的测试文件须在冻结快照范围内。')
    if version == 2:
        markers = plan['baseline_failure_markers']
        if (not isinstance(markers, dict) or set(markers) != set(required)
                or any(not isinstance(marker, str) or not marker.strip() or len(marker) > 500
                       or not marker.isprintable() for marker in markers.values())):
            raise ValueError('原生验收计划须为每个必验目标提供 1–500 个可打印字符的基线故障标记。')
    return plan


def _verify_native_git(record, commits, repository, *, control):
    receipt = {'trust': 'receiver_supplied_local_git'}
    if record['commits'] != commits:
        return {**receipt, 'status': 'blocked', 'reason': 'declared_commits_mismatch'}
    repo = Path(repository).expanduser().resolve()
    snapshots, resolved = {}, set()
    with tempfile.TemporaryDirectory(prefix='reprolab-native-git-') as temporary:
        for side in SIDES:
            control.check()
            try:
                commit = commits[side]
                if resolve_commit(repo, commit, local_only=True) != commit:
                    raise ValueError('Git did not resolve the exact commit.')
                resolved.add(side)
                snapshot = Path(temporary) / side
                export_revision(repo, commit, snapshot, local_only=True)
                source = _source_record(snapshot)
            except (OSError, ValueError):
                snapshots[side] = {'content': 'unavailable', 'mode': 'unavailable'}
                continue
            saved = record['source_snapshots'][side]
            snapshots[side] = {
                'content': 'matched' if source['sha256'] == saved['sha256'] else 'mismatch',
                'mode': 'matched' if source['mode_sha256'] == saved['mode_sha256'] else 'mismatch',
            }

    control.check()
    recorded = record['snapshot_scope']['excluded_tracked_changes']
    observed = None
    if len(resolved) == len(SIDES):
        try:
            observed = excluded_tracked_changes(repo, commits['before'], commits['after'], local_only=True)
        except (OSError, ValueError):
            pass
    if observed is None:
        omission = 'unavailable'
    elif recorded is None:
        omission = 'unrecorded'
    else:
        omission = 'matched' if observed == recorded else 'mismatch'

    checks = {value for row in snapshots.values() for value in row.values()}
    if 'unavailable' in checks or omission == 'unavailable':
        status, reason = 'blocked', 'git_objects_unavailable'
    elif 'mismatch' in checks:
        status, reason = 'blocked', 'git_snapshot_mismatch'
    elif omission == 'mismatch':
        status, reason = 'blocked', 'git_omitted_changes_mismatch'
    elif omission == 'unrecorded':
        status, reason = 'partial', 'git_omitted_changes_unrecorded'
    else:
        status, reason = 'matched', 'git_snapshots_and_omissions_matched'
    return {**receipt, 'status': status, 'reason': reason, 'snapshots': snapshots,
            'excluded_tracked_changes': {'status': omission, 'recorded': recorded, 'observed': observed}}


def _review_native_result(record, plan, sha256):
    markers = plan.get('baseline_failure_markers')
    marker_checks = record.pop('baseline_marker_checks', None)
    result = {'plan_sha256': sha256, 'plan_schema_version': plan['schema_version'],
              'status': 'blocked', 'exit_code': 2}
    if 'pr_context' in plan:
        result['pr_context'] = plan['pr_context']
    if record['schema_version'] == 1:
        result['reason'] = 'historical_scope_unknown'
    elif 'review_plan_sha256' in record and record['review_plan_sha256'] != sha256:
        result['reason'] = 'review_plan_digest_mismatch'
    elif 'pr_context' in plan and 'review_plan_sha256' not in record:
        result['reason'] = 'pr_review_plan_unrecorded'
    else:
        definitions = record['shared_test_files']
        inputs = record['shared_test_inputs']
        # check_native has already recomputed each declared file's bytes and permissions from both snapshots.
        matches = {
            'runner': record['runner'] == plan['runner'],
            'commits': record['commits'] == plan['commits'],
            'selected_tests': set(record['selected_tests'] or ()) == set(plan['selected_tests']),
            'shared_test_sources': {row['path'] for row in definitions} == set(plan['shared_test_sources']),
            'shared_test_inputs': {row['path'] for row in inputs} == set(plan['shared_test_inputs']),
        }
        if not all(matches.values()):
            result.update(reason='review_plan_scope_mismatch',
                          mismatched_fields=[field for field, match in matches.items() if not match])
        elif unmatched := [row['path'] for row in definitions + inputs if not row['matches']]:
            result.update(reason='review_plan_test_files_changed_or_missing', unmatched_test_files=unmatched)
        else:
            result.update(native_fix_acceptance(record, plan['required_fixes']))
            if markers and result['exit_code'] == 0:
                if marker_checks is None:
                    result.update(status='blocked', exit_code=2, reason='baseline_failure_markers_unsupported')
                else:
                    result['baseline_marker_checks'] = marker_checks
                    if any(status != 'matched' for status in marker_checks.values()):
                        result.update(status='blocked', exit_code=2, reason='baseline_failure_marker_unverified')
    return result


def review_native(location, plan_file, *, git_repository=None, control):
    path = Path(location).expanduser().resolve()
    plan_path = Path(plan_file).expanduser().resolve()
    if plan_path.is_relative_to(path):
        raise ValueError('原生验收计划须位于证据目录或 ZIP 之外，由接收方提供。')
    plan, sha256 = _read_review_plan(plan_path)
    return review_native_plan(location, plan, sha256, git_repository=git_repository, control=control)


def review_native_plan(location, plan, sha256, *, git_repository=None, control):
    if git_repository is not None and Path(git_repository).expanduser().resolve().is_relative_to(
            Path(location).expanduser().resolve()):
        raise ValueError('用于核对的 Git 仓库须位于证据目录或 ZIP 之外，由接收方提供。')
    markers = plan.get('baseline_failure_markers')
    record = check_native(location, baseline_markers=markers, control=control)
    result = _review_native_result(record, plan, sha256)
    if git_repository is not None:
        git_check = _verify_native_git(record, plan['commits'], git_repository, control=control)
        record = {**record, 'git_verification': git_check}
        if git_check['status'] != 'matched' and result['exit_code'] != 2:
            result.update(status='blocked', exit_code=2, reason=git_check['reason'])
    return {**record, 'review_plan_acceptance': result}, result['exit_code']


def _reported(record):
    if record['schema_version'] == 1:
        return {**record, 'snapshot_scope': {'excluded_tracked_changes': None, 'git_diff_evidence': 'unknown'},
                'test_input_coverage': 'unknown',
                'review_summary': '历史证据按原规则复核。Git 改动遗漏及测试输入覆盖情况未知。'}
    return record


def run_native_plan(repository, plan_file, output, *, timeout, cargo, attest_test_inputs, control):
    plan, sha256 = _read_review_plan(Path(plan_file).expanduser())
    return run_native(
        repository, plan['commits']['before'], plan['commits']['after'], output,
        runner=plan['runner'], selected=plan['selected_tests'], shared_sources=plan['shared_test_sources'],
        test_inputs=plan['shared_test_inputs'], timeout=timeout, cargo=cargo, confirmed=True,
        attest_test_inputs=attest_test_inputs, review_plan=(plan, sha256), control=control,
    )


def run_native(repository, before, after, output, *, runner, selected=None, shared_sources=(),
               timeout=120, cargo=None, confirmed=False, test_inputs=(), attest_test_inputs=False,
               review_plan=None, control):
    selected = _targets(selected)
    paths = _definitions(list(shared_sources))
    input_paths = _definitions(list(test_inputs))
    if runner not in RUNNERS or cargo is not None and runner != 'cargo':
        raise ValueError('请选择有效的原生执行器；--cargo 仅用于 cargo。')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .1 <= timeout <= 3600:
        raise ValueError('原生运行时限须为 0.1–3600 秒。')
    if runner == 'cargo' and paths and not any(Path(path).suffix == '.rs' for path in paths):
        raise ValueError('Cargo 的共享测试定义清单须至少声明一个 .rs 文件。')
    root = repository_root(repository)
    commits = {side: resolve_commit(root, rev) for side, rev in (('before', before), ('after', after))}
    if review_plan and commits != review_plan[0]['commits']:
        raise ValueError('本地 Git 未解析到验收计划指定的完整提交。')
    excluded = excluded_tracked_changes(root, commits['before'], commits['after'])
    if not confirmed:
        return {'status': 'ready', 'runner': runner, 'commits': commits, 'selected_tests': selected,
                'excluded_tracked_changes': excluded}, 0
    requested = Path(output).expanduser()
    target = requested.resolve()
    if target.is_relative_to(root) or target.exists() or requested.is_symlink():
        raise ValueError('请选择仓库外尚不存在的证据目录。')
    adapter = _adapter(runner, replay=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.native-stage-', dir=target.parent) as temporary:
        stage = Path(temporary) / 'native-comparison'
        (stage / 'inputs').mkdir(parents=True)
        sources, runs, hashes = {}, {}, {}
        for side in SIDES:
            control.check()
            fixed = stage / 'inputs' / side
            export_revision(root, commits[side], fixed)
            sources[side] = _source_record(fixed)
            evidence = stage / side
            evidence.mkdir()
            with tempfile.TemporaryDirectory(prefix='reprolab-native-run-') as run_directory:
                disposable = Path(run_directory) / 'source'
                shutil.copytree(fixed, disposable)
                options = {'cargo': cargo} if runner == 'cargo' else {}
                runs[side] = _run_facts(adapter(disposable, evidence, selected=selected,
                                                timeout=timeout, control=control, **options))
            hashes[side] = _files(evidence)
        definitions = _definition_record(stage, paths)
        scope = _snapshot_scope(commits, sources, excluded)
        inputs = _definition_record(stage, input_paths)
        comparison = _scoped_comparison(runs, definitions, selected, runner=runner,
                                        scope=scope, inputs=inputs, attested=attest_test_inputs)
        record = {
            'kind': 'project_native_comparison', 'schema_version': 2,
            'runner': runner, 'commits': commits, 'selected_tests': selected,
            'source_snapshots': sources, 'shared_test_files': definitions, 'runs': runs,
            'snapshot_scope': scope, 'shared_test_inputs': inputs, 'test_inputs_attested': attest_test_inputs,
            'evidence_sha256': hashes, 'comparison': comparison,
        }
        plan, sha256 = review_plan if review_plan else (None, None)
        if plan is not None:
            record['review_plan_sha256'] = sha256
        save_json(stage / _RECORD, record)
        checked = _checked_native(stage, control, baseline_markers=plan.get('baseline_failure_markers') if plan else None)
        if plan is not None:
            acceptance = _review_native_result(checked, plan, sha256)
            result, code = {**record, 'integrity': 'consistent', 'review_plan_acceptance': acceptance}, acceptance['exit_code']
        else:
            result, code = record, comparison['gate']['exit_code']
        control.commit()
        if target.exists() or target.is_symlink():
            raise ValueError('证据目标已存在。')
        stage.rename(target)
    return {**result, 'directory': str(target)}, code


def _checked_native(folder, control, *, baseline_markers=None):
    folder = Path(folder).resolve()
    record = _read_json(folder / _RECORD)
    if (record.get('kind') != 'project_native_comparison' or type(record.get('schema_version')) is not int
            or record['schema_version'] not in (1, 2) or record.get('runner') not in RUNNERS
            or set(record['commits']) != set(SIDES)
            or any(not isinstance(commit, str) or not _COMMIT.fullmatch(commit)
                   for commit in record['commits'].values())):
        raise ValueError('原生比较证据格式无效。')
    if 'review_plan_sha256' in record:
        digest = record['review_plan_sha256']
        if (record['schema_version'] != 2 or not isinstance(digest, str)
                or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise ValueError('原生比较证据中的验收计划指纹无效。')
    selected = _targets(record['selected_tests'])
    paths = _definitions([row['path'] for row in record['shared_test_files']])
    reader = _adapter(record['runner'], replay=True)
    if record['runner'] == 'cargo' and paths and not any(Path(path).suffix == '.rs' for path in paths):
        raise ValueError('Cargo 共享源证据缺少 Rust 测试定义。')
    if (folder / 'inputs').is_symlink():
        raise ValueError('原生源码快照目录不能是链接。')
    schema = record['schema_version']
    reader_options = {'schema_version': schema} if record['runner'] == 'cmake' else {}
    for side in SIDES:
        control.check()
        if (_source_record(folder / 'inputs' / side) != record['source_snapshots'][side]
                or _files(folder / side) != record['evidence_sha256'][side]):
            raise ValueError('原生比较源码或运行证据指纹不一致。')
        facts = _run_facts(record['runs'][side])
        if _run_facts(reader(folder / side, selected=selected, **reader_options)) != facts:
            raise ValueError('原生运行记录与原始证据重算结果不一致。')
    definitions = _definition_record(folder, paths)
    if schema == 1:
        comparison = _compare(record['runs'], definitions, selected, runner=record['runner'])
    else:
        scope = _snapshot_scope(record['commits'], record['source_snapshots'],
                                record['snapshot_scope']['excluded_tracked_changes'])
        inputs = _definition_record(folder, _definitions([row['path'] for row in record['shared_test_inputs']]))
        attested = record['test_inputs_attested']
        if (scope != record['snapshot_scope'] or inputs != record['shared_test_inputs']
                or type(attested) is not bool):
            raise ValueError('原生测试输入或快照范围与固定记录不一致。')
        comparison = _scoped_comparison(record['runs'], definitions, selected, runner=record['runner'],
                                        scope=scope, inputs=inputs, attested=attested)
    if definitions != record['shared_test_files'] or comparison != record['comparison']:
        raise ValueError('原生对照结果与固定记录不一致。')
    if baseline_markers and schema == 2 and record['runner'] == 'cargo':
        from .native_cargo import check_failure_markers

        record['baseline_marker_checks'] = check_failure_markers(
            folder / 'before', record['evidence_sha256']['before'], record['runs']['before'], baseline_markers,
        )
    if baseline_markers and schema == 2 and record['runner'] == 'cmake':
        from .native_cmake import check_failure_markers

        record['baseline_marker_checks'] = check_failure_markers(
            folder / 'before' / 'results.xml', record['evidence_sha256']['before'].get('results.xml'),
            record['runs']['before']['tests'], baseline_markers,
        )
    return record


def check_native(location, *, baseline_markers=None, control):
    path = Path(location).expanduser()
    if path.is_symlink():
        raise ValueError('请选择普通证据目录或 ZIP。')
    if path.is_dir():
        return {**_reported(_checked_native(path, control, baseline_markers=baseline_markers)), 'integrity': 'consistent'}
    if not path.is_file() or path.stat().st_size > _MAX_BYTES:
        raise ValueError('请选择有效的原生证据目录或 ZIP。')
    with tempfile.TemporaryDirectory(prefix='reprolab-native-evidence-') as temporary:
        try:
            root = _extract(path, Path(temporary), control, _MAX_BYTES)
        except (BadZipFile, NotImplementedError) as exc:
            raise ValueError('无法读取原生证据 ZIP。') from exc
        return {**_reported(_checked_native(root, control, baseline_markers=baseline_markers)), 'integrity': 'consistent'}


def pack_native(directory, output, *, control):
    folder = Path(directory).expanduser().resolve()
    target = Path(output).expanduser().resolve()
    if target.exists() or Path(output).expanduser().is_symlink() or target.is_relative_to(folder):
        raise ValueError('请选择证据目录外尚不存在的 ZIP 路径。')
    record = _checked_native(folder, control)
    members = [Path(_RECORD)]
    for side in SIDES:
        tree = Path('inputs') / side
        members.extend([tree, *(tree / entry for entry in source_entries(folder / tree))])
        members.extend([Path(side), *(Path(side) / name for name in record['evidence_sha256'][side])])
    if len(members) > _MAX_ENTRIES or sum((folder / path).stat().st_size for path in members
                                          if (folder / path).is_file()) > _MAX_BYTES:
        raise ValueError('原生证据超过 ZIP 交付限制。')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.native-pack-', dir=target.parent) as temporary:
        stage = Path(temporary) / 'evidence.zip'
        with ZipFile(stage, 'x', compression=ZIP_DEFLATED) as archive:
            for path in members:
                control.check()
                name = 'native-evidence/' + path.as_posix()
                _archive_path(name)
                archive.write(folder / path, name)
        restored = check_native(stage, control=control)
        if restored != {**_reported(record), 'integrity': 'consistent'}:
            raise ValueError('打包期间原生证据发生变化。')
        digest = _sha(stage)
        control.commit()
        try:
            os.link(stage, target)
        except FileExistsError as exc:
            raise ValueError('证据 ZIP 已存在。') from exc
    return {'status': 'packed', 'output': str(target), 'sha256': digest}
