"""Exercise installed CLI entry points outside the source checkout; no model calls."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import tomllib


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True, help='New directory outside the source repository')
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    output = args.output.expanduser().resolve()
    if output.is_relative_to(source):
        parser.error('Installation checks must run outside the source repository.')
    import reprolab
    installed = Path(reprolab.__file__).resolve()
    if installed.is_relative_to(source):
        parser.error('Use a fresh environment with the built wheel installed, not an editable/source install.')
    expected = tomllib.loads((source / 'pyproject.toml').read_text())['project']['version']
    if reprolab.__version__ != expected or importlib.metadata.version('reprolab-local') != expected:
        parser.error('Installed code, metadata and source candidate versions differ.')
    output.mkdir(parents=True, exist_ok=False)
    checks = []

    def cli(*arguments, expected_code=0, json_output=True):
        command = [sys.executable, '-I', '-m', 'reprolab', *map(str, arguments)]
        result = subprocess.run(command, cwd=output, capture_output=True, text=True, timeout=90)
        if result.returncode != expected_code:
            raise RuntimeError(f'CLI failed: {arguments[0]} / exit {result.returncode}\n{result.stderr}\n{result.stdout}')
        return json.loads(result.stdout) if json_output else result.stdout

    if any((installed.parent / name).exists() for name in ('demo_case', 'demo_recheck', 'history_cases', 'project_demo.py')):
        raise RuntimeError('Installed wheel contains source-only examples.')
    checks.append('installed runtime excludes teaching payloads')

    repo = output / 'project'
    repo.mkdir()
    (repo / 'answer.py').write_text('def answer():\n    return 0\n')
    (repo / 'test_answer.py').write_text(
        'from answer import answer\n\n'
        'def test_answer():\n    assert answer() == 42\n\n'
        'def test_answer_is_int():\n    assert isinstance(answer(), int)\n',
        encoding='utf-8',
    )
    def git(*arguments):
        subprocess.run(['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                        '-c', 'user.name=ReproLab installation check', '-c', 'user.email=test@example.invalid',
                        *arguments], check=True, capture_output=True)
    git('init', '-q')
    git('add', '.')
    git('commit', '-qm', 'baseline')
    baseline = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    (repo / 'answer.py').write_text('def answer():\n    return 42\n')
    git('add', 'answer.py')
    git('commit', '-qm', 'candidate')

    fix = 'test_answer.py::test_answer'
    suggested = cli('project', 'suggest', repo, '--before', baseline, '--after', 'HEAD',
                    '--python', sys.executable, '--output', output / 'suggested review', '--format', 'json')
    if (suggested['status'] != 'ready' or not suggested['selection_file'] or not suggested['run_command']
            or fix not in [row['test_id'] for row in suggested['recommendations']]):
        raise RuntimeError('The installed CLI could not prepare an explicit suggestion from actual discovery.')
    config = repo / '.ci'
    config.mkdir()
    reviewed_plan = config / "reviewer's approved plan.json"
    created_plan = cli('project', 'plan-from-selection', suggested['directory'],
                       '--selection-file', suggested['selection_file'], '--require-improvement', fix,
                       '--output', reviewed_plan)
    suggested_run = cli('project', 'run', suggested['directory'], '--acceptance-plan', reviewed_plan,
                        '--verify-plan', '--output', output / 'suggested review/reports')
    if (suggested_run['project_id'] != suggested['project_id']
            or suggested_run['source_sha256'] != suggested['source_sha256'] or fix not in suggested_run['selected_tests']
            or suggested_run['acceptance']['acceptance_plan_sha256'] != created_plan['acceptance_plan_sha256']
            or suggested_run['acceptance']['gate']['status'] != 'passed'
            or (config / 'reports').exists()):
        raise RuntimeError('The installed CLI did not validate the reviewed plan on its original frozen session.')
    checks.append('project: discover, save reviewed selection as a plan, and verify the plan on the same session')
    selected = [fix, 'test_answer.py::test_answer_is_int']
    selection = output / 'regression-tests.txt'
    selection.write_text('\n'.join(selected) + '\n', encoding='utf-8')
    plan = output / 'acceptance-plan.json'
    plan.write_text(json.dumps({
        'kind': 'project_acceptance_plan', 'schema_version': 1, 'runner': 'pytest',
        'policy': 'no-new-failures', 'required_tests': [fix],
        'require_receipt': False, 'min_stability_rounds': None,
    }), encoding='utf-8')
    markdown = output / 'review-summary.md'
    report = cli('project', 'review', repo, '--before', baseline, '--after', 'HEAD',
                 '--python', sys.executable, '--selection-file', selection,
                 '--acceptance-plan', plan, '--verify-plan', '--summary-file', markdown,
                 '--output', output / 'review', '--progress')
    acceptance = report['acceptance']
    if (report['selected_tests'] != selected
            or report['counts'] != {'improved': 1, 'regression_passed': 1}
            or acceptance['gate']['status'] != 'passed'
            or acceptance['required_improvements'] != [{'test_id': fix, 'status': 'passed', 'outcome': 'improved'}]
            or report['summary_file'] != str(markdown)
            or acceptance['report_sha256'] not in markdown.read_text(encoding='utf-8')):
        raise RuntimeError('Installed project review did not verify the selected scope and save its CI summary.')
    if cli('project', 'check', report['report_dir'])['status'] != 'consistent':
        raise RuntimeError('Project evidence is inconsistent.')
    bundle = output / 'project.zip'
    cli('project', 'pack', report['report_dir'], '--output', bundle)
    checked = cli('project', 'check-summary', bundle, markdown, '--acceptance-plan', plan)
    if checked['status'] != 'consistent' or checked['gate'] != acceptance['gate']:
        raise RuntimeError('The installed CLI could not check the PR summary against the handoff ZIP.')
    imported = cli('project', 'unpack', bundle, output / 'imported')
    if cli('project', 'check', imported['report_dir'])['status'] != 'consistent':
        raise RuntimeError('Imported project evidence is inconsistent.')
    repo.rename(output / 'sender-project-moved')
    for language in ('en', 'zh'):
        options = ('--brief', '--language', language)
        brief = cli('project', 'summary', bundle, *options, json_output=False)
        heading = 'Gate Passed (strict)' if language == 'en' else '门禁 通过（严格）'
        if heading not in brief or acceptance['report_sha256'] not in brief or fix in brief or str(repo) in brief:
            raise RuntimeError('The installed CLI could not produce a decision brief from the receiver ZIP.')
        received = output / f'brief.{language}.md'
        received.write_text(brief, encoding='utf-8')
        checked = cli('project', 'check-summary', bundle, received, *options)
        if (checked['status'] != 'consistent' or checked['gate']['exit_code'] != 0
                or checked['coverage'] != 'rendered_aggregate_markdown'):
            raise RuntimeError('The installed CLI could not independently check the received brief.')
    checks.append('project: English and Chinese decision brief handoff from the received ZIP')
    preflight = cli('project', 'replay', bundle, '--check-environment')
    if preflight['status'] != 'ready' or preflight['source_archive'] != str(bundle):
        raise RuntimeError('The installed CLI could not preflight the evidence ZIP.')
    receipt_dir = output / 'receipt'
    replay = cli('project', 'replay', bundle, '--output', output / 'replayed',
                 '--confirm-run', '--receipt-dir', receipt_dir)
    if replay['status'] != 'regression_passed' or replay['run_id'] == report['run_id']:
        raise RuntimeError('Replay did not create a new passing record.')
    if cli('project', 'check', replay['report_dir'])['status'] != 'consistent':
        raise RuntimeError('Replayed evidence is inconsistent.')
    verification = replay['replay_verification']
    checked_receipt = cli('project', 'check-receipt', bundle, receipt_dir)
    if (verification['status'] != 'verified'
            or verification['counts'] != {'matched': 2, 'different': 0, 'incomplete': 0}
            or checked_receipt['status'] != 'consistent'
            or checked_receipt['replay_verification'] != verification):
        raise RuntimeError('Replay verification and offline verification differ.')
    receipt = (receipt_dir / 'replay-receipt.md').read_text(encoding='utf-8')
    if ('Selected outcomes reproduced' not in receipt or 'test_answer.py' in receipt
            or any(verification[key]['report_sha256'] not in receipt for key in ('source', 'replay'))):
        raise RuntimeError('The installed CLI did not produce a shareable receipt for the checked reports.')
    accepted = cli('project', 'summary', bundle, '--format', 'json',
                   '--require-improvement', fix, '--require-receipt', receipt_dir)
    if (accepted['gate']['status'] != 'passed' or accepted['required_improvements'][0]['outcome'] != 'improved'
            or accepted['receipt']['replay_report_sha256'] != verification['replay']['report_sha256']):
        raise RuntimeError('The installed CLI did not verify the required improvement and replay receipt.')
    checks.append('project: PR acceptance and CI summary, ZIP handoff, replay receipt and sender check')
    result = {'version': expected, 'installed_module': str(installed), 'python': sys.version.split()[0],
              'status': 'passed', 'checks': checks,
              'scope': 'Installed CLI and local controlled fixtures; no GUI or live model service.'}
    (output / 'installation-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
