from distutils.core import setup

setup(
    name="repo_002_setup_py_div",
    version="0.1.0",
    description="Python 2 packaging style: setup.py with no pyproject.toml.",
    py_modules=["legacy_module"],
    install_requires=[
        "six",
    ],
)
