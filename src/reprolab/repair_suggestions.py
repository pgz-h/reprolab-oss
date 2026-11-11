"""Conservative message candidates, never confirmed failure conditions."""
import re


def message_suggestions(message):
    def usable(text):
        return (4 <= len(text) <= 240 and text not in ('assert False', 'assert True', 'None', 'False', 'True')
                and not re.search(r'(?:[/\\]|0x[0-9a-fA-F]+|\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}|\d{7,}|[0-9a-fA-F]{16,}|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,})', text)
                and bool(re.search(r'[A-Za-z\u4e00-\u9fff]|\d+\.\d+', text)))
    # Quoted literals in assertion comparisons are suggestions, not inferred intent.
    quoted = [m.group(2) for m in re.finditer(r"(['\"])([^'\"\n]+)\1", message)]
    lines = [line.strip() for line in message.splitlines() if not line.lstrip().startswith(('E ', '+', '-', '?', 'where ', '>'))]
    candidates = list(dict.fromkeys(text for text in [*reversed(quoted), *lines] if usable(text)))[:6]
    return {'candidates': candidates,
            'notice': '以下片段来自历史异常，仅为建议；请核对稳定性和故障含义后确认。' if candidates else
                      '未提取到可建议的稳定片段。请查看历史异常，人工选取非空片段；临时路径、时间和随机值不宜作为条件。'}
