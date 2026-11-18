"""Keep the source-only shopping-cart generator out of installed packages."""
from setuptools import setup
from setuptools.command.build_py import build_py


class RuntimeBuild(build_py):
    def find_package_modules(self, package, package_dir):
        modules = super().find_package_modules(package, package_dir)
        return [item for item in modules if (item[0], item[1]) != ('reprolab', 'project_demo')]


setup(cmdclass={'build_py': RuntimeBuild})
