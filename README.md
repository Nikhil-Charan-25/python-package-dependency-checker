# Python Package Dependency Checker

A simple command-line tool to look at what packagesre installed in your current Python environment or in a specific project. It checks for missing packages, wrong versions, version conflicts, outdated packages. Can make dependency graphs and reports.

## Setup

```powershell

py -m venv.venv

.\.venv\Scripts\Activate.ps1

python -m pip install -r requirements.txt

```

## Commands

```powershell

# Check packages and declared dependencies

python dependency_checker.py

python dependency_checker.py --package packaging

# Analyze dependency declarations in this folder (even if checker is in another location)

python "C:\PATH\TO\Python-Dependency-Checker\dependency_checker.py" --project "." --check

# You can use paths with spaces

python dependency_checker.py --project "C:\Users\User\Documents\my project" --check

# Check for issues and version conflicts

python dependency_checker.py --check

python dependency_checker.py --conflicts

# Look for updates on PyPI (needs internet)

python dependency_checker.py --outdated --package packaging

python dependency_checker.py --outdated

# Make dependency graphs

python dependency_checker.py --graph --package packaging --output packaging_graph.dot

python dependency_checker.py --graph --output all_packages.dot

# Create a diagnostic report

python dependency_checker.py --report

python dependency_checker.py --report --include-outdated

# Run all checks. Results go to test_results/

python dependency_checker.py --all

# Generate reports and graphs for a project in test_results/

python dependency_checker.py --project "." --all

python dependency_checker.py --project "." --check --conflicts

python dependency_checker.py --project "." --outdated

# Run tests

python -m unittest -v

```

## Optional: Install for easier use

Install the project in mode to run `depcheck` instead of typing `python dependency_checker.py`:

```powershell

python -m pip install -e.

depcheck --help

depcheck --version

```

## Graph images

The `--graph` command creates a Graphviz DOT file. If you have Graphviz installed turn it into an image:

```powershell

& "C:\Program Files\Graphviz\bin\dot.exe" -Tpng all_packages.dot -o all_packages.png

```

## Recommendations

Running `--all` creates `dependency_report.txt` and `recommendations.txt` in `test_results/`. Findings are marked as `ERROR` `WARNING` or `INFO`. The suggested pip commands are suggestions. The tool does not install or change anything on its own.

An update suggestion is shown when the latest version on PyPI fits the declared version rules. Conflicts are explained clearly without giving an one-command fix.

## Target-project analysis

Using `--project PATH` lets the tool read a project’s dependency list without running any code or changing files. It works with `requirements.txt` ( with `-r` includes) the `[project]` and `[project.optional-dependencies]` sections in `pyproject.toml` common Poetry format `setup.cfg` and direct `install_requires`/`extras_require` values, in `setup.py`. Optional dependencies are labeled by their group in the report.

If the target project has `.venv\Scripts\python.exe` on Windows or `.venv/bin/python` on Linux/macOS the tool checks that environment. Otherwise it checks the Python interpreter and says so in the report. A declared dependency is always reported separately from what’s installed. A missing package means it’s not in the checked interpreter not that the project never meant to use it.

Reports show the project path, which files were found how many dependencies were declared and which interpreter was checked. Graphs made with `--project` start from the project and show the declared dependencies. No guessed connections. If something is not supported, not literal, broken or local/editable it’s marked as unresolved not ignored. You only need internet when using `--outdated`.