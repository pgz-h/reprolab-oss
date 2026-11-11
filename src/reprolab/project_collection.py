"""Collection scope and guidance derived from saved discovery evidence."""
from pathlib import PurePosixPath
import re


class CollectionPathUnavailable(ValueError):
    """A saved or explicitly requested path is unavailable in the frozen candidate."""


def normalize_collection_path(value):
    if not isinstance(value, str) or len(value) > 4000 or any(ord(char) < 32 for char in value):
        raise ValueError('发现范围须为项目内的相对文件或目录路径。')
    value = value.strip().replace('\\', '/')
    if not value:
        return ''
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or '..' in path.parts or ':' in value:
        raise ValueError('发现范围须为项目内的相对文件或目录路径，例如 tests/unit。')
    return path.as_posix()


def require_collection_target(snapshot, path):
    if path:
        target = snapshot / path
        if (not target.resolve().is_relative_to(snapshot)
                or not (target.is_dir() or (target.suffix == '.py' and target.is_file()))):
            raise CollectionPathUnavailable(f'固定快照中找不到此测试文件或目录：{path}。请调整发现范围后重试。')


def collection_diagnosis(record):
    if record.get('scope_mismatch'):
        code, summary = 'scope_mismatch', '收集结果包含发现范围外的测试。请检查项目的 pytest 插件或调整发现范围。'
    elif record.get('timed_out'):
        code, summary = 'timed_out', '测试发现超时。可提高上方时限后重试；固定源码保留。'
    elif record.get('worker_status') == 'worker_error':
        code, summary = 'worker_error', '测试发现进程异常结束，未获得完整清单。查看日志后重试或检查环境。'
    elif record.get('status') != 'collected':
        if (record.get('worker_record_complete') is True and record.get('exit_code') == 5
                and record.get('count') == 0 and not record.get('collection_errors')):
            code = 'empty'
            summary = ('指定范围内没有发现测试。请检查测试命名或调整发现范围。' if record.get('collection_path')
                       else '没有发现测试。检查项目的 testpaths 和测试命名；修改原项目后点击“更新源码并发现”。')
        else:
            code, summary = 'collection_failed', '测试发现未完成。查看收集错误并检查环境；修改原项目后点击“更新源码并发现”。'
    elif record.get('partial'):
        code, summary = 'partial', f'发现清单不完整：当前列出 {len(record.get("tests") or [])} 项，另有收集错误。请修复后重试，或明确勾选本次范围。'
    elif record.get('truncated'):
        code, summary = 'truncated', '发现清单已截断至 10000 项。请指定更小的发现范围后重新发现，或明确勾选本次范围。'
    else:
        code, summary = 'collected', '测试发现完成。核对勾选范围后运行两版。'
    modules = []
    if code in ('partial', 'collection_failed'):
        for error in record.get('collection_errors') or ():
            if not isinstance(error, str):
                continue
            match = re.search(r"(?m)^E[ \t]+ModuleNotFoundError: No module named ['\"]"
                              r"([A-Za-z_][A-Za-z0-9_.]{0,119})['\"]\s*$", error)
            if match and match[1] not in modules:
                modules.append(match[1])
    if modules:
        names = '、'.join(modules[:3]) + ('等' if len(modules) > 3 else '')
        detail = '导入失败：找不到 ' + names + '。请检查 Python 环境与项目导入配置。'
        summary = (f'发现清单不完整（{len(record.get("tests") or [])} 项）。' if code == 'partial' else '') + detail
    return {'code': code, 'summary': summary, **({'missing_modules': modules} if modules else {})}
