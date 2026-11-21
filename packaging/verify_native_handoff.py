"""End-to-end handoff for fixed CTest inputs, run with the installed CLI."""
import argparse
import json
import os
from pathlib import Path
import subprocess


TEST = 'receipt'
DEFINITIONS = ('CMakeLists.txt', 'probe.cmake')
INPUT = 'expectation.txt'


def fixture(repo):
    repo.mkdir()
    git_env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_AUTHOR_DATE='2000-01-01T00:00:00+00:00',
                   GIT_COMMITTER_DATE='2000-01-01T00:00:00+00:00')
    git = ['git', '-C', str(repo), '-c', 'user.name=ReproLab CI',
           '-c', 'user.email=ci@example.invalid', '-c', 'commit.gpgsign=false',
           '-c', 'core.autocrlf=false']

    def write(name, value):
        path = repo / name
        path.write_bytes(value.encode('utf-8'))
        path.chmod(0o644)

    def commit(message):
        subprocess.run([*git, 'add', '.'], check=True, env=git_env)
        subprocess.run([*git, 'commit', '-qm', message], check=True, env=git_env)
        return subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True, env=git_env).strip()

    subprocess.run([*git, 'init', '-q', '--template=', '--object-format=sha1', '-b', 'main'],
                   check=True, env=git_env)
    write('CMakeLists.txt', 'cmake_minimum_required(VERSION 3.29)\n'
          'project(native_handoff LANGUAGES C)\nenable_testing()\n'
          'add_executable(result result.c)\n'
          'add_test(NAME receipt COMMAND "${CMAKE_COMMAND}" "-DPROGRAM=$<TARGET_FILE:result>" '
          '-P "${CMAKE_CURRENT_SOURCE_DIR}/probe.cmake")\n')
    write('probe.cmake', 'execute_process(COMMAND "${PROGRAM}" RESULT_VARIABLE status OUTPUT_VARIABLE actual)\n'
          'file(READ "${CMAKE_CURRENT_LIST_DIR}/expectation.txt" expected)\n'
          'if(NOT status EQUAL 0 OR NOT actual STREQUAL expected)\n'
          '  message(FATAL_ERROR "unexpected result")\nendif()\n')
    write('result.c', '#include <stdio.h>\nint main(void) { return puts("2") == EOF; }\n')
    write(INPUT, '1\n')
    before = commit('Incorrect result')
    write(INPUT, '2\n')
    wrong = commit('Change the expected value')
    write(INPUT, '1\n')
    write('result.c', '#include <stdio.h>\nint main(void) { return puts("1") == EOF; }\n')
    after = commit('Correct the result')
    return before, after, wrong


def cli(executable, *args, expected=0):
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    run = subprocess.run([str(executable), 'project', *map(str, args)], capture_output=True, text=True, env=env)
    if run.returncode != expected:
        raise SystemExit(f'Expected {expected}, got {run.returncode} from {args[0]}\n{run.stdout}\n{run.stderr}')
    return run.stdout


def write_plan(plan, before, after):
    plan.write_text(json.dumps({
        'schema_version': 1, 'runner': 'cmake', 'commits': {'before': before, 'after': after},
        'selected_tests': [TEST], 'required_fixes': [TEST], 'shared_test_sources': list(DEFINITIONS),
        'shared_test_inputs': [INPUT],
    }, indent=2) + '\n', encoding='utf-8', newline='\n')


def send(executable, work, bundles):
    before, after, wrong = fixture(work / 'sender-git')
    bundles.mkdir(parents=True, exist_ok=True)
    plan = work / 'trusted-plan.json'
    write_plan(plan, before, after)
    accepted = work / 'accepted'
    run = cli(executable, 'native', work / 'sender-git', '--review-plan', plan,
              '--attest-test-inputs', '--confirm-run', '--output', accepted)
    (work / 'accepted.json').write_text(run, encoding='utf-8')
    if json.loads(run)['review_plan_acceptance']['targets'] != {TEST: 'improved'}:
        raise SystemExit('The sender did not observe the fixed fixture red-to-green transition.')
    cli(executable, 'pack-native', accepted, '--output', bundles / 'accepted.zip')
    for name, candidate in (('missing-input', after), ('wrong-candidate', wrong)):
        output = work / name
        native = json.loads(cli(executable, 'native', work / 'sender-git', '--runner', 'cmake',
                                '--before', before, '--after', candidate, '--test', TEST,
                                '--shared-test-source', DEFINITIONS[0], '--shared-test-source', DEFINITIONS[1],
                                '--attest-test-inputs', '--require-fix', TEST, '--confirm-run', '--output', output))
        if native['fix_acceptance']['targets'] != {TEST: 'improved'}:
            raise SystemExit('The negative sender fixture did not observe red-to-green.')
        cli(executable, 'pack-native', output, '--output', bundles / f'{name}.zip')


def receive(executable, work, bundles, *, verify_git=False):
    before, after, _ = fixture(work / 'receiver-git')
    plan = work / 'trusted-plan.json'
    write_plan(plan, before, after)
    sections = []
    for name, title, expected, fields in (
        ('accepted', 'Implementation fixed with stable expectations', 0, []),
        ('missing-input', 'Sender omitted the expectation file', 2, ['shared_test_inputs']),
        ('wrong-candidate', 'Sender compared a different candidate', 2, ['commits', 'shared_test_inputs']),
    ):
        args = ('check-native', bundles / f'{name}.zip', '--review-plan', plan)
        raw = cli(executable, *args, expected=expected)
        (work / f'{name}.json').write_text(raw, encoding='utf-8')
        acceptance = json.loads(raw)['review_plan_acceptance']
        reason = 'required_fixes_observed' if expected == 0 else 'review_plan_scope_mismatch'
        if acceptance['reason'] != reason or acceptance.get('mismatched_fields', []) != fields:
            raise SystemExit(f'Unexpected review-plan result for {name}.')
        if verify_git:
            args += ('--verify-git-repository', work / 'receiver-git')
            inspected = cli(executable, *args, expected=expected)
            (work / f'{name}-git.json').write_text(inspected, encoding='utf-8')
            provenance = json.loads(inspected)['git_verification']
            expected_git = 'blocked' if name == 'wrong-candidate' else 'matched'
            if provenance['status'] != expected_git:
                raise SystemExit(f'Unexpected local Git verification result for {name}.')
        section = cli(executable, *args, '--format', 'markdown', expected=expected)
        outcome = 'pass' if expected == 0 else 'block'
        sections.append(f'## Fixture: {title} (expected {outcome}, exit {expected})\n\n{section}')
    (work / 'native-summary.md').write_text('\n'.join(sections), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('send', 'receive'))
    parser.add_argument('--reprolab', required=True, type=Path)
    parser.add_argument('--work', required=True, type=Path)
    parser.add_argument('--bundles', required=True, type=Path)
    parser.add_argument('--verify-git', action='store_true', help='Verify the rebuilt receiver repository too')
    args = parser.parse_args()
    if args.verify_git and args.mode != 'receive':
        parser.error('--verify-git is only available in receive mode')
    args.work.mkdir(parents=True)
    paths = args.reprolab.resolve(), args.work.resolve(), args.bundles.resolve()
    if args.mode == 'send':
        send(*paths)
    else:
        receive(*paths, verify_git=args.verify_git)


if __name__ == '__main__':
    main()
