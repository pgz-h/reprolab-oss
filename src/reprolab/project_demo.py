"""Create an editable teaching project, without collecting or running tests."""
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import uuid

from .project_sessions import prepare_project
from .workspace import WORKTREE


def create_project_demo(projects_root, *, control=None):
    projects_root = Path(projects_root).expanduser().resolve()
    examples = projects_root.parent / 'examples'
    examples.mkdir(parents=True, exist_ok=True)
    name = 'shopping-cart-' + uuid.uuid4().hex[:8]
    final = examples / name
    with tempfile.TemporaryDirectory(prefix='.example-', dir=examples) as temp:
        repo = Path(temp) / name
        (repo / 'tests').mkdir(parents=True)
        (repo / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n.venv/\n', encoding='utf-8')
        (repo / 'pyproject.toml').write_text(
            '[project]\nname = "reprolab-shopping-example"\nversion = "0.0.0"\n'
            'requires-python = ">=3.11"\ndependencies = []\n', encoding='utf-8')
        (repo / 'cart.py').write_text(
            'def total(prices, discount=0):\n    return sum(prices) - discount\n\n'
            'def display(value):\n    return f"{value:.2f}"\n\n'
            'def tax(value):\n    return value * 0.2\n', encoding='utf-8')
        (repo / 'tests/test_cart.py').write_text(
            'import pytest\nfrom cart import total, display, tax\n\n'
            '@pytest.mark.parametrize("amount", [10, 20, 30, 40, 50])\n'
            'def test_regular_total(amount):\n    assert total([amount, 2]) == amount + 2\n\n'
            'def test_discount_cannot_make_total_negative():\n    assert total([10], discount=20) == 0\n\n'
            'def test_money_display():\n    assert display(12) == "12.00"\n\n'
            'def test_existing_tax_problem():\n    assert tax(100) == 10\n\n'
            'def test_optional_payment_service():\n    pytest.skip("教学示例：未接入支付服务")\n', encoding='utf-8')
        shutil.copytree(Path(__file__).with_name('demo_recheck'), repo / 'recheck-scenarios')
        (repo / 'README.md').write_text(
            '# ReproLab 购物车教学项目\n\n这是人为构造的示例，不连接外部服务。\n\n'
            '基线有折扣导致负金额的问题；当前工作区修复该问题，同时故意引入金额显示回归。'
            '税率问题在两版都存在，支付服务检查被跳过。\n\n'
            '预期矩阵：5 两版通过、1 改善、1 新增失败、1 原有失败、1 受阻。'
            '创建示例只准备代码和会话，点击发现测试与运行后才会执行。\n\n'
            '报告复查场景和独立补丁见 recheck-scenarios/README.md；预期结果尚待实际运行确认。\n', encoding='utf-8')
        def git(*args):
            if control:
                control.check()
            return subprocess.check_output(
                ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', '-C', str(repo), *args],
                stderr=subprocess.STDOUT, text=True, timeout=30).strip()
        git('init', '-q')
        git('add', '.')
        git('-c', 'user.name=ReproLab example', '-c', 'user.email=example@reprolab.invalid',
            'commit', '-qm', 'teaching baseline')
        baseline = git('rev-parse', 'HEAD')
        (repo / 'cart.py').write_text(
            'def total(prices, discount=0):\n    return max(0, sum(prices) - discount)\n\n'
            'def display(value):\n    return f"{value:.1f}"\n\n'
            'def tax(value):\n    return value * 0.2\n', encoding='utf-8')
        repo.rename(final)
    if control:
        control.check()
    return prepare_project(final, baseline, WORKTREE, projects_root,
                           python_executable=sys.executable, title='购物车示例 · 项目回归入门',
                           issue='确认折扣修复，并检查常规金额、显示格式及税率相关行为。')
