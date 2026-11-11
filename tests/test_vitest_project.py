from reprolab.vitest_project import _case_counts, _compare, _compare_legacy, _derive_version


def test_vitest_file_scope_uses_actual_results_and_test_inventory(tmp_path):
    selected = ['fixed.test.ts', 'changed.test.ts', 'skip.test.ts', 'broken.test.ts']
    execution = {'status': 'completed', 'phase': 'vitest', 'exit_code': 0}

    def case(name, status='passed'):
        return {'name': name, 'status': status, 'expected_failure': False, 'errors': []}

    def derive(side, cases_by_file, blocked=None):
        files = [
            {'path': name, 'tests': cases,
             'errors': [{'type': 'Error', 'message': 'module unavailable'}] if name == blocked else []}
            for name, cases in zip(selected, cases_by_file)
        ]
        (tmp_path / (side + '.log')).write_text('')
        return _derive_version(tmp_path, side,
                               {'schema_version': 1, 'files': files, 'errors': [], 'vitest_version': '4.1.11'},
                               execution, selected, '4.1.11')

    before = derive('before', [[case('price', 'failed')], [case('old')], [case('skippable')], [case('load')]])
    after = derive('after', [[case('price')], [case('new', 'failed')], [case('skippable', 'skipped')], []],
                   blocked='broken.test.ts')
    status, entries, counts = _compare_legacy(before, after, selected)

    assert status == 'attention'
    assert counts == {'improved': 1, 'inconclusive': 3}
    assert entries[1]['reason'] == '两版实际收集的测试名称不同。'
    assert entries[2]['after']['reason'] == '文件中有跳过或未完成的测试。'
    assert entries[3]['after']['exception']['message'] == 'module unavailable'


def test_vitest_reports_mixed_file_and_retried_pass(tmp_path):
    selected = ['mixed.test.js', 'retry.test.js', 'ambiguous.test.js', 'project.test.js']
    execution = {'status': 'completed', 'phase': 'vitest', 'exit_code': 0}

    def case(name, status, retries=0):
        return {'name': name, 'status': status, 'expected_failure': False, 'errors': [],
                'retry_count': retries, 'flaky': bool(retries)}

    def version(side, files, project):
        (tmp_path / (side + '.log')).write_text('')
        raw = {'schema_version': 2, 'errors': [], 'vitest_version': '4.1.11',
               'files': [{'path': name, 'project': project if name == selected[-1] else 'unit',
                          'errors': [], 'tests': cases} for name, cases in zip(selected, files)]}
        return _derive_version(tmp_path, side, raw, execution, selected, '4.1.11', 2)

    duplicate = [case('same', 'passed'), case('same', 'failed')]
    before = version('before', [[case('A', 'failed'), case('B', 'passed')], [case('retry', 'passed')],
                                duplicate, [case('A', 'passed')]], 'browser')
    after = version('after', [[case('B', 'failed'), case('A', 'passed')], [case('retry', 'passed', retries=1)],
                              duplicate, [case('A', 'failed')]], 'node')
    status, entries, counts = _compare(before, after, selected)
    assert status == 'regression_detected'
    assert counts == {'regression_detected': 1, 'inconclusive': 3}
    assert _case_counts(entries) == {'improved': 1, 'regression_detected': 1, 'inconclusive': 1}
    assert {case['test_id']: case['status'] for case in entries[0]['case_entries']} == {
        'A': 'improved', 'B': 'regression_detected',
    }
    assert entries[1]['case_entries'][0]['after']['retry_passed'] is True
    assert entries[1]['case_entries'][0]['after']['status'] == 'passed'
    assert entries[2]['case_entries'] == []
    assert entries[3]['reason'] == '两版测试所属的 Vitest 项目不同。'
