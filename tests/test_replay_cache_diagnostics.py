"""Derive cache recovery hints only from saved npm error records."""
import pytest

from reprolab.project_diagnostics import VITEST_OFFLINE_CACHE_MISSING, vitest_offline_cache_misses
from reprolab.project_report import write_project_report


@pytest.mark.parametrize(('runner', 'offline', 'phase', 'status', 'log', 'expected'), [
    ('vitest', True, 'dependencies', 'dependencies_failed', 'npm error code ENOTCACHED\n', ('before',)),
    ('vitest', True, 'dependencies', 'dependencies_failed', 'npm ERR! code ENOTCACHED\r\n', ('before',)),
    ('vitest', False, 'dependencies', 'dependencies_failed', 'npm error code ENOTCACHED', ()),
    ('go', True, 'dependencies', 'dependencies_failed', 'npm error code ENOTCACHED', ()),
    ('vitest', True, 'vitest', 'dependencies_failed', 'npm error code ENOTCACHED', ()),
    ('vitest', True, 'dependencies', 'timed_out', 'npm error code ENOTCACHED', ()),
    ('vitest', True, 'dependencies', 'dependencies_failed', 'npm error code E401\nmessage: ENOTCACHED', ()),
    ('vitest', True, 'dependencies', 'dependencies_failed', 'npm error code ENOTCACHED-other', ()),
])
def test_cache_miss_requires_offline_install_failure_and_an_exact_error_code(runner, offline, phase, status, log, expected):
    report = {'runner': runner, 'offline': offline,
              'before': {'phase': phase, 'status': status, 'log_excerpt': log},
              'after': {'phase': 'vitest', 'status': 'completed'}}
    assert vitest_offline_cache_misses(report) == expected


def test_html_shows_only_the_affected_side_and_preserves_the_saved_report(tmp_path):
    report = {'runner': 'vitest', 'offline': True, 'report_dir': str(tmp_path), 'status': 'attention',
              'before': {'phase': 'dependencies', 'status': 'dependencies_failed',
                         'log_excerpt': 'npm error code ENOTCACHED\nnpm error request to https://example.invalid/'},
              'after': {'phase': 'dependencies', 'status': 'dependencies_failed',
                        'log_excerpt': 'npm error code E401'}}
    saved = tmp_path / 'report.json'
    saved.write_text('original record')
    write_project_report(report)
    html = (tmp_path / 'report.html').read_text()
    before, after = html.split('<p class="eyebrow">AFTER</p>', 1)
    assert before.count(VITEST_OFFLINE_CACHE_MISSING) == 1
    assert VITEST_OFFLINE_CACHE_MISSING not in after
    assert 'npm ci 未完成' in after
    assert 'https://example.invalid/' not in before.split('<summary>查看日志摘要</summary>', 1)[0]
    assert saved.read_text() == 'original record'
