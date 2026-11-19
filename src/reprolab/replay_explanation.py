"""Describe differences in saved pytest failure text without exposing the text."""
from collections import Counter
import re


# Match the standard *shape* of Python function and object reprs. A user-defined
# repr can produce identical text, so this is a description, never an exemption.
_NAME = r'(?:[^\W\d]\w*|<locals>|<lambda>)(?:\.(?:[^\W\d]\w*|<locals>|<lambda>))*'
_REPR = re.compile(r'<(?:function ' + _NAME + '|' + _NAME + r' object) at (0x[0-9a-fA-F]+)>')
_LABELS = {
    'repr_address_text_only': 'Only address-like tokens in Python object representations differ',
    'exception_text_changed': 'Recorded exception text differs beyond object addresses',
    'phase_or_type_changed': 'The recorded failure phase or exception type differs',
}


def _address_pattern(text, aliases):
    """Keep literal text, token positions and repeated references distinguishable."""
    pieces = []
    start = 0
    for match in _REPR.finditer(text):
        a, b = match.span(1)
        value = match[1]
        pieces.extend((text[start:a], aliases.setdefault(value, len(aliases))))
        start = b
    pieces.append(text[start:])
    return pieces


def _failure_parts(outcome):
    return [(event['phase'], event['exception']['type'], event['exception']['message'])
            for event in outcome['phases'] if event['outcome'] == 'failed']


def _classify(left, right):
    if [(phase, kind) for phase, kind, _ in left] != [(phase, kind) for phase, kind, _ in right]:
        return 'phase_or_type_changed'
    original, repeated = {}, {}
    for (_, _, before), (_, _, after) in zip(left, right):
        if _address_pattern(before, original) != _address_pattern(after, repeated):
            return 'exception_text_changed'
    return 'repr_address_text_only'


def explain_pytest_failures(result, source, replay):
    """Add optional annotations after the normal verification decision is final."""
    if result['runner'] != 'pytest':
        raise ValueError('--explain 适用于 pytest 项目重放核对。')
    left, right = ({row['test_id']: row for row in report['entries']} for report in (source, replay))
    counts = Counter()
    for row in result['entries']:
        for side in row.get('failure_fingerprint_changes', ()):
            key = row['test_id']
            basis = _classify(_failure_parts(left[key][side]), _failure_parts(right[key][side]))
            row.setdefault('failure_text_difference', {})[side] = basis
            counts[basis] += 1
    result['failure_text_differences'] = dict(counts)


def explanation_markdown(result):
    counts = result['failure_text_differences']
    lines = ['## Recorded pytest failure text', '']
    if counts:
        entries = result['entries']
        if all(row['source'] is not None and row['source'] == row['replay'] and row['status'] != 'incomplete'
               for row in entries):
            lines.extend(['The observed pass/fail outcomes repeated for every selected test. '
                          'Some recorded failure evidence differed.', ''])
        lines.extend(f'- {label} · {counts[key]} version comparison(s)'
                     for key, label in _LABELS.items() if key in counts)
        lines.extend(['', 'Counts describe the stored messages on the baseline and candidate separately. '
                      'Strict verification uses the original evidence.'])
    else:
        lines.append('No differing recorded failure-message fingerprints were available to explain.')
    return '\n'.join(lines) + '\n'
