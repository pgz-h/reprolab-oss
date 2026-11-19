from reprolab.project_test_changes import changed_test_definitions


def test_changed_definitions_keep_parameter_ids_class_scope_and_candidate_lines(tmp_path):
    before, after = tmp_path / 'before', tmp_path / 'after'
    before.mkdir()
    after.mkdir()
    original = ('class TestPrice:\n'
                '    @mark(1)\n'
                '    def test_amount(self, value): assert value > 0\n'
                'class TestReceipt:\n'
                '    def test_amount(self, value): assert value > 0\n')
    (before / 'test_price.py').write_text(original)
    (after / 'test_price.py').write_text('# relocated\n' + original.replace('@mark(1)', '@mark(2)')
                                         + 'async def test_new(): pass\n')
    (after / 'test_more.py').write_text('def test_another(): pass\n')
    (before / 'test_broken.py').write_text('invalid (')
    (after / 'test_broken.py').write_text('def test_unknown(): pass\n')
    changes = [{'kind': kind, 'path': file} for file, kind in
               [('test_price.py', 'modified'), ('test_more.py', 'added'), ('test_broken.py', 'modified')]]
    tests = ['test_price.py::TestPrice::test_amount[a::b]', 'test_price.py::TestPrice::test_amount[c]',
             'test_price.py::TestReceipt::test_amount', 'test_price.py::test_new', 'test_more.py::test_another',
             'test_broken.py::test_unknown', 'test_price.py::test_synthesized']
    decisions = changed_test_definitions(changes, tests, before, after)
    assert decisions == {
        **{item: {'status': 'modified', 'path': 'test_price.py', 'line': 4} for item in tests[:2]},
        'test_price.py::test_new': {'status': 'added', 'path': 'test_price.py', 'line': 7},
        'test_more.py::test_another': {'status': 'added', 'path': 'test_more.py', 'line': 1},
    }


def test_conditionally_defined_baseline_is_not_reported_as_a_new_test(tmp_path):
    before, after = tmp_path / 'before', tmp_path / 'after'
    before.mkdir()
    after.mkdir()
    (before / 'test_mode.py').write_text('if ENABLED:\n    def test_mode(): pass\n')
    (after / 'test_mode.py').write_text('def test_mode(): pass\n')
    assert changed_test_definitions([{'kind': 'modified', 'path': 'test_mode.py'}],
                                    ['test_mode.py::test_mode'], before, after) == {}


def test_receiver_scope_counts_ids_without_disclosing_changed_names():
    from reprolab.project_run_scope import public_run_scope, pytest_run_scope

    tests = ['test_order.py::test_shipping[standard]', 'test_order.py::test_shipping[express]',
             'test_order.py::test_billing']
    report = {'selected_tests': tests[1:]}
    collection = {'tests': tests, 'changed_test_definitions': {
        target: {'status': 'added' if i < 2 else 'modified', 'line': 3, 'path': 'test_order.py'}
        for i, target in enumerate(tests)}}
    scope = public_run_scope(pytest_run_scope(report, collection=collection))
    assert scope['changed_test_ids'] == {'added': 2, 'modified': 1, 'selected': 2, 'omitted': 1}
    assert 'test_order.py' not in str(scope)
    for invalid in ({}, {'changed_test_definitions': {tests[0]: {'status': 'added', 'path': 'wrong.py', 'line': 3}}}):
        assert 'changed_test_ids' not in pytest_run_scope(report, collection={'tests': tests, **invalid})


def test_redefined_class_cannot_attribute_an_inherited_method_to_shadowed_source(tmp_path):
    before, after = tmp_path / 'before', tmp_path / 'after'
    before.mkdir()
    after.mkdir()
    code = ('class Base:\n    def test_discount(self): assert True\n'
            'class TestCart:\n    def test_discount(self): assert False\n'
            'class TestCart(Base): pass\n')
    (before / 'test_cart.py').write_text(code)
    (after / 'test_cart.py').write_text(code.replace('assert False', 'assert 10 == 11'))
    assert changed_test_definitions([{'kind': 'modified', 'path': 'test_cart.py'}],
                                    ['test_cart.py::TestCart::test_discount'], before, after) == {}


def test_missing_generated_file_requires_matching_declaration_and_absence(tmp_path):
    from reprolab.project_collection import declared_version_file
    config = tmp_path / 'pyproject.toml'
    template = '[tool.hatch.build.hooks.vcs]\nversion-file = "{}"\n'
    config.write_text(template.format('src/pkg/_version.py'))
    assert declared_version_file(tmp_path, ['pkg._version']) == {
        'module': 'pkg._version', 'path': 'src/pkg/_version.py', 'setting': 'tool.hatch.build.hooks.vcs.version-file'}
    assert declared_version_file(tmp_path, ['unrelated']) is None
    for raw in ('/tmp/pkg/_version.py', '../pkg/_version.py', 'pkg/_version.txt'):
        config.write_text(template.format(raw))
        assert declared_version_file(tmp_path, ['pkg._version']) is None
    config.write_text(template.format('src/pkg/_version.py'))
    file = tmp_path / 'src/pkg/_version.py'
    file.parent.mkdir(parents=True)
    file.write_text('__version__ = "1"')
    assert declared_version_file(tmp_path, ['pkg._version']) is None


def test_saved_generated_module_hint_remains_bound_to_the_missing_import():
    from reprolab.project_collection import collection_diagnosis
    record = {'status': 'collection_failed', 'collection_errors': [
        "E   ModuleNotFoundError: No module named 'pkg._version'"],
        'generated_version_file': {'module': 'pkg._version', 'setting': 'tool.hatch.build.hooks.vcs.version-file',
                                   'path': 'src/pkg/_version.py'}}
    diagnosis = collection_diagnosis(record)
    assert diagnosis['code'] == 'collection_failed'
    assert 'project verify-wheels' in diagnosis['summary']
    for bad in ({}, {'path': '/tmp/wrong.py'}, {'path': 'src/pkg/other.py'}, {'path': None}):
        changed = dict(record, generated_version_file={**record['generated_version_file'], **bad})
        if not bad:
            changed['generated_version_file'] = {}
        assert 'generated_version_file' not in collection_diagnosis(changed)
