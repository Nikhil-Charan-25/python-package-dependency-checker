"""Python Package Dependency Checker with diagnostic recommendations."""

from __future__ import annotations

import argparse
import ast
import configparser
import json
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import Distribution, distributions
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

__version__ = "1.1.0"


@dataclass(frozen=True)
class DependencyIssue:
    package: str
    requirement: str
    problem: str
    installed_version: str | None = None


@dataclass(frozen=True)
class VersionConflict:
    dependency: str
    constraints: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class OutdatedPackage:
    name: str
    installed_version: str
    latest_version: str


@dataclass(frozen=True)
class UpdateCheckError:
    package: str
    reason: str


@dataclass(frozen=True)
class DependencyGraph:
    nodes: dict[str, str]
    edges: list[tuple[str, str, str]]


@dataclass(frozen=True)
class ProjectRequirement:
    """A dependency declared by a project file (never executed to obtain it)."""
    requirement: Requirement
    source: str
    group: str = "default"


@dataclass(frozen=True)
class ProjectAnalysis:
    path: Path
    requirements: tuple[ProjectRequirement, ...]
    notices: tuple[str, ...]
    environment_python: Path | None
    environment_label: str


@dataclass(frozen=True)
class DiagnosticFinding:
    severity: str
    title: str
    package: str
    reason: str
    action: str
    required_version: str | None = None
    installed_version: str | None = None
    latest_version: str | None = None
    command: str | None = None


def installed_packages() -> list[Distribution]:
    unique: dict[str, Distribution] = {}
    for package in distributions():
        unique.setdefault(canonicalize_name(package.metadata["Name"]), package)
    return sorted(unique.values(), key=lambda package: package.metadata["Name"].lower())


def project_python(project_path: Path) -> Path | None:
    """Return a conventional project virtualenv interpreter, if one exists."""
    candidates = (project_path / ".venv" / "Scripts" / "python.exe",
                  project_path / ".venv" / "bin" / "python")
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def packages_from_python(python: Path | None) -> tuple[list[Distribution], str]:
    """Inspect package metadata in a project's interpreter without running its code."""
    if python is None:
        return installed_packages(), f"current interpreter ({sys.executable})"
    script = (
        "import importlib.metadata as m, json; "
        "print(json.dumps([(d.metadata.get('Name'), d.version, d.requires or []) for d in m.distributions()]))"
    )
    try:
        completed = subprocess.run([str(python), "-c", script], capture_output=True, text=True,
                                   timeout=20, check=True)
        result = []
        for name, version, requirements in json.loads(completed.stdout):
            result.append(_MetadataDistribution(name, version, requirements))
        return result, str(python)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        return installed_packages(), f"current interpreter ({sys.executable}; project .venv unavailable: {error})"


class _MetadataDistribution:
    """Small Distribution-compatible object used for another interpreter's metadata."""
    def __init__(self, name: str, version: str, requirements: list[str]):
        self.metadata = {"Name": name}
        self.version = version
        self.requires = requirements


def _add_requirement(raw: str, source: str, group: str, found: list[ProjectRequirement], notices: list[str]) -> None:
    try:
        requirement = Requirement(raw)
    except InvalidRequirement:
        notices.append(f"Unsupported or invalid declaration in {source}: {raw}")
        return
    if requirement.marker and not requirement.marker.evaluate(default_environment()):
        return
    found.append(ProjectRequirement(requirement, source, group))


def _read_requirements_file(path: Path, found: list[ProjectRequirement], notices: list[str], seen: set[Path]) -> None:
    path = path.resolve()
    if path in seen:
        notices.append(f"Requirement include cycle ignored: {path}")
        return
    seen.add(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        notices.append(f"Could not read {path}: {error}")
        return
    for line_number, line in enumerate(lines, 1):
        raw = line.split("#", 1)[0].strip()
        if not raw:
            continue
        if raw.startswith(("-r ", "--requirement ")):
            included = raw.split(None, 1)[1]
            _read_requirements_file(path.parent / included, found, notices, seen)
        elif raw.startswith(("-e ", "--editable ")) or raw.startswith((".", "/")):
            notices.append(f"Local/editable requirement not evaluated in {path}:{line_number}: {raw}")
        elif raw.startswith("-"):
            notices.append(f"Unsupported requirements option in {path}:{line_number}: {raw}")
        else:
            _add_requirement(raw, f"{path}:{line_number}", "default", found, notices)


def _read_pyproject(path: Path, found: list[ProjectRequirement], notices: list[str]) -> None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        notices.append(f"Could not parse {path}: {error}")
        return
    project = data.get("project", {})
    for raw in project.get("dependencies", []):
        _add_requirement(raw, str(path), "default", found, notices)
    for group, requirements in project.get("optional-dependencies", {}).items():
        for raw in requirements:
            _add_requirement(raw, str(path), group, found, notices)
    poetry = data.get("tool", {}).get("poetry", {})
    for group, requirements in (("default", poetry.get("dependencies", {})), *[(name, value.get("dependencies", {})) for name, value in poetry.get("group", {}).items()]):
        for name, value in requirements.items():
            if name.lower() == "python":
                continue
            if isinstance(value, str):
                _add_requirement(f"{name}{value if value != '*' else ''}", str(path), group, found, notices)
            else:
                notices.append(f"Unsupported Poetry declaration in {path}: {name}")
    if not project and not poetry:
        notices.append(f"No supported dependency table found in {path}")


def _read_setup_cfg(path: Path, found: list[ProjectRequirement], notices: list[str]) -> None:
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
        values = parser.get("options", "install_requires", fallback="").splitlines()
        for raw in values:
            if raw.strip(): _add_requirement(raw.strip(), str(path), "default", found, notices)
        for group, raw_values in parser.items("options.extras_require") if parser.has_section("options.extras_require") else []:
            for raw in raw_values.splitlines():
                if raw.strip(): _add_requirement(raw.strip(), str(path), group, found, notices)
    except (OSError, configparser.Error) as error:
        notices.append(f"Could not parse {path}: {error}")


def _read_setup_py(path: Path, found: list[ProjectRequirement], notices: list[str]) -> None:
    """Extract literal install_requires/extras_require only; arbitrary code is never run."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as error:
        notices.append(f"Could not parse {path}: {error}")
        return
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not (isinstance(call.func, ast.Name) and call.func.id == "setup"):
            continue
        for keyword in call.keywords:
            try: value = ast.literal_eval(keyword.value)
            except (ValueError, TypeError):
                if keyword.arg in {"install_requires", "extras_require"}: notices.append(f"Non-literal {keyword.arg} is not evaluated in {path}")
                continue
            if keyword.arg == "install_requires" and isinstance(value, list):
                for raw in value: _add_requirement(raw, str(path), "default", found, notices)
            elif keyword.arg == "extras_require" and isinstance(value, dict):
                for group, requirements in value.items():
                    for raw in requirements: _add_requirement(raw, str(path), str(group), found, notices)


def analyze_project(project: str | Path) -> ProjectAnalysis:
    path = Path(project).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Project directory does not exist: {path}")
    found: list[ProjectRequirement] = []
    notices: list[str] = []
    requirements_file = path / "requirements.txt"
    if requirements_file.is_file(): _read_requirements_file(requirements_file, found, notices, set())
    if (path / "pyproject.toml").is_file(): _read_pyproject(path / "pyproject.toml", found, notices)
    if (path / "setup.cfg").is_file(): _read_setup_cfg(path / "setup.cfg", found, notices)
    if (path / "setup.py").is_file(): _read_setup_py(path / "setup.py", found, notices)
    if not any((requirements_file.is_file(), (path / "pyproject.toml").is_file(), (path / "setup.cfg").is_file(), (path / "setup.py").is_file())):
        notices.append("No supported dependency file was found (requirements.txt, pyproject.toml, setup.cfg, setup.py).")
    python = project_python(path)
    return ProjectAnalysis(path, tuple(found), tuple(notices), python, str(python) if python else f"current interpreter ({sys.executable})")


def print_package(package: Distribution) -> None:
    """Display one package and its declared dependency metadata."""
    print(f"{package.metadata['Name']} ({package.version})")
    requirements = package.requires or []
    if requirements:
        for requirement in requirements:
            print(f"  -> {requirement}")
    else:
        print("  -> No declared dependencies")


def active_requirements(packages: Iterable[Distribution]) -> dict[str, list[tuple[str, Requirement]]]:
    """Group requirements that apply to the current environment by dependency name."""
    groups: dict[str, list[tuple[str, Requirement]]] = {}
    environment = default_environment()
    for package in packages:
        for raw_requirement in package.requires or []:
            try:
                requirement = Requirement(raw_requirement)
            except InvalidRequirement:
                continue
            if requirement.marker and not requirement.marker.evaluate(environment):
                continue
            groups.setdefault(canonicalize_name(requirement.name), []).append(
                (package.metadata["Name"], requirement)
            )
    return groups


def dependency_issues(packages: Iterable[Distribution]) -> list[DependencyIssue]:
    """Detect missing requirements, invalid metadata, and incompatible versions."""
    package_list = list(packages)
    installed = {
        canonicalize_name(package.metadata["Name"]): package for package in package_list
    }
    environment = default_environment()
    issues: list[DependencyIssue] = []

    for package in package_list:
        for raw_requirement in package.requires or []:
            try:
                requirement = Requirement(raw_requirement)
            except InvalidRequirement:
                issues.append(
                    DependencyIssue(package.metadata["Name"], raw_requirement, "has invalid requirement metadata")
                )
                continue
            if requirement.marker and not requirement.marker.evaluate(environment):
                continue
            installed_package = installed.get(canonicalize_name(requirement.name))
            if installed_package is None:
                issues.append(DependencyIssue(package.metadata["Name"], raw_requirement, "is missing"))
            elif requirement.specifier and installed_package.version not in requirement.specifier:
                issues.append(
                    DependencyIssue(
                        package.metadata["Name"], raw_requirement,
                        "has an incompatible installed version", installed_package.version,
                    )
                )
    return issues


def constraints_conflict(requirements: Iterable[Requirement]) -> bool:
    """Return true when common PEP 440 constraints have no shared version."""
    requirement_list = list(requirements)
    combined = SpecifierSet(",".join(str(requirement.specifier) for requirement in requirement_list))
    exact_versions: list[Version] = []
    lower: tuple[Version, bool] | None = None
    upper: tuple[Version, bool] | None = None

    def tighten_lower(version: Version, inclusive: bool) -> None:
        nonlocal lower
        if lower is None or version > lower[0] or (version == lower[0] and not inclusive):
            lower = (version, inclusive)

    def tighten_upper(version: Version, inclusive: bool) -> None:
        nonlocal upper
        if upper is None or version < upper[0] or (version == upper[0] and not inclusive):
            upper = (version, inclusive)

    for requirement in requirement_list:
        for specifier in requirement.specifier:
            try:
                version = Version(specifier.version.rstrip(".*"))
            except InvalidVersion:
                continue
            if specifier.operator == "==" and not specifier.version.endswith(".*"):
                exact_versions.append(version)
            elif specifier.operator == "==":
                tighten_lower(version, True)
                parts = list(version.release)
                parts[-1] += 1
                tighten_upper(Version(".".join(map(str, parts))), False)
            elif specifier.operator == ">=":
                tighten_lower(version, True)
            elif specifier.operator == ">":
                tighten_lower(version, False)
            elif specifier.operator == "<=":
                tighten_upper(version, True)
            elif specifier.operator == "<":
                tighten_upper(version, False)
            elif specifier.operator == "~=" and len(version.release) > 1:
                tighten_lower(version, True)
                parts = list(version.release[:-1])
                parts[-1] += 1
                tighten_upper(Version(".".join(map(str, parts))), False)

    if exact_versions:
        return any(not combined.contains(version, prereleases=True) for version in exact_versions)
    if lower and upper:
        return lower[0] > upper[0] or (
            lower[0] == upper[0]
            and not (lower[1] and upper[1] and combined.contains(lower[0], prereleases=True))
        )
    return False


def version_conflicts(packages: Iterable[Distribution]) -> list[VersionConflict]:
    conflicts: list[VersionConflict] = []
    for dependency, entries in active_requirements(packages).items():
        if len(entries) > 1 and constraints_conflict(requirement for _, requirement in entries):
            conflicts.append(
                VersionConflict(
                    dependency,
                    tuple((source, str(requirement.specifier)) for source, requirement in entries),
                )
            )
    return conflicts


def pypi_latest_version(package_name: str, timeout: float = 10.0) -> str:
    request = Request(
        f"https://pypi.org/pypi/{quote(package_name)}/json",
        headers={"User-Agent": "PythonDependencyChecker/1.1"},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)["info"]["version"]


def find_outdated(
    packages: Iterable[Distribution],
    latest_version_lookup: Callable[[str], str] = pypi_latest_version,
) -> tuple[list[OutdatedPackage], list[UpdateCheckError]]:
    """Check PyPI without changing installed packages."""
    package_list = list(packages)
    outdated: list[OutdatedPackage] = []
    errors: list[UpdateCheckError] = []
    with ThreadPoolExecutor(max_workers=min(10, max(1, len(package_list)))) as executor:
        futures = {executor.submit(latest_version_lookup, package.metadata["Name"]): package for package in package_list}
        for future in as_completed(futures):
            package = futures[future]
            try:
                latest = future.result()
                if Version(latest) > Version(package.version):
                    outdated.append(OutdatedPackage(package.metadata["Name"], package.version, latest))
            except (HTTPError, URLError, TimeoutError, KeyError, InvalidVersion, OSError) as error:
                errors.append(UpdateCheckError(package.metadata["Name"], str(error)))
    return sorted(outdated, key=lambda item: item.name.lower()), errors


def pip_requirement(requirement: Requirement) -> str:
    return f"{requirement.name}{requirement.specifier}"


def latest_is_compatible(
    package_name: str, latest_version: str, groups: dict[str, list[tuple[str, Requirement]]]
) -> bool:
    constraints = [requirement.specifier for _, requirement in groups.get(canonicalize_name(package_name), []) if requirement.specifier]
    if not constraints:
        return False
    return all(Version(latest_version) in constraint for constraint in constraints)


def build_diagnostics(
    packages: Iterable[Distribution],
    include_updates: bool = False,
    latest_version_lookup: Callable[[str], str] = pypi_latest_version,
) -> tuple[list[DiagnosticFinding], tuple[list[OutdatedPackage], list[UpdateCheckError]] | None]:
    """Separate findings and safe recommendations from low-level detection."""
    package_list = list(packages)
    findings: list[DiagnosticFinding] = []
    groups = active_requirements(package_list)

    for issue in dependency_issues(package_list):
        try:
            requirement = Requirement(issue.requirement)
        except InvalidRequirement:
            findings.append(DiagnosticFinding(
                "WARNING", "Invalid dependency metadata", issue.package,
                f"The declared requirement '{issue.requirement}' cannot be interpreted.",
                "Review and correct this package's dependency metadata.",
            ))
            continue
        required = str(requirement.specifier) or None
        command = f'python -m pip install "{pip_requirement(requirement)}"'
        if issue.problem == "is missing":
            findings.append(DiagnosticFinding(
                "ERROR", "Missing dependency", requirement.name,
                f"{issue.package} requires {pip_requirement(requirement)}, but it is not installed.",
                f"Install a version that satisfies {issue.package}'s declared requirement.",
                required, None, command=command,
            ))
        else:
            findings.append(DiagnosticFinding(
                "ERROR", "Incompatible installed version", requirement.name,
                f"Installed version {issue.installed_version} does not satisfy {issue.package}'s requirement {pip_requirement(requirement)}.",
                "Install a version that satisfies the declared constraint; review other constraints before applying it.",
                required, issue.installed_version, command=command,
            ))

    for conflict in version_conflicts(package_list):
        details = "; ".join(f"{package} requires {conflict.dependency}{constraint}" for package, constraint in conflict.constraints)
        findings.append(DiagnosticFinding(
            "ERROR", "Dependency version conflict", conflict.dependency,
            f"These requirements cannot be satisfied together: {details}.",
            "Check for compatible releases of the affected packages or revise the project constraints. No safe single pip command can be determined.",
        ))

    update_result = None
    if include_updates:
        update_result = find_outdated(package_list, latest_version_lookup)
        outdated, errors = update_result
        for package in outdated:
            if latest_is_compatible(package.name, package.latest_version, groups):
                findings.append(DiagnosticFinding(
                    "WARNING", "Compatible update available", package.name,
                    f"Version {package.latest_version} is available; {package.installed_version} is installed and the known declared constraints allow the newer version.",
                    "Review the release notes, then upgrade if it suits the project.",
                    package.name, package.installed_version, package.latest_version,
                    f"python -m pip install --upgrade {package.name}",
                ))
            else:
                findings.append(DiagnosticFinding(
                    "INFO", "Update available; compatibility not confirmed", package.name,
                    f"Version {package.latest_version} is available, but the checker cannot confirm that it fits every project constraint.",
                    "Review release notes and project requirements before upgrading.",
                    installed_version=package.installed_version, latest_version=package.latest_version,
                ))
        for error in errors:
            findings.append(DiagnosticFinding(
                "WARNING", "Unable to check package update", error.package,
                f"PyPI update information could not be retrieved: {error.reason or 'unknown error'}.",
                "Check the internet connection, PyPI availability, and package name, then run the check again.",
            ))
    return findings, update_result


def build_project_diagnostics(analysis: ProjectAnalysis, packages: Iterable[Distribution]) -> list[DiagnosticFinding]:
    """Compare a project's declarations to the selected interpreter's installed metadata."""
    installed = {canonicalize_name(p.metadata["Name"]): p for p in packages}
    findings: list[DiagnosticFinding] = []
    by_name: dict[str, list[ProjectRequirement]] = {}
    reported: set[tuple[str, str, str]] = set()
    for declared in analysis.requirements:
        by_name.setdefault(canonicalize_name(declared.requirement.name), []).append(declared)
        requirement = declared.requirement
        duplicate_key = (canonicalize_name(requirement.name), str(requirement.specifier), declared.group)
        if duplicate_key in reported:
            continue
        reported.add(duplicate_key)
        package = installed.get(canonicalize_name(requirement.name))
        location = f"Declared in {declared.source} ({declared.group})."
        command = f'"{analysis.environment_python or sys.executable}" -m pip install "{pip_requirement(requirement)}"'
        if package is None:
            findings.append(DiagnosticFinding("ERROR", "Missing declared dependency", requirement.name,
                f"{location} It is not installed in the inspected environment: {analysis.environment_label}.",
                "Install it with the intended project's Python interpreter after reviewing the declaration.",
                str(requirement.specifier) or None, command=command))
        elif requirement.specifier and package.version not in requirement.specifier:
            findings.append(DiagnosticFinding("ERROR", "Declared version constraint violation", requirement.name,
                f"{location} Installed version {package.version} in {analysis.environment_label} does not satisfy {pip_requirement(requirement)}.",
                "Review the declared constraint before upgrading or downgrading; it may be unrealistic or incorrect.",
                str(requirement.specifier), package.version, command=command))
    for dependency, declarations in by_name.items():
        if len(declarations) > 1 and constraints_conflict(item.requirement for item in declarations):
            details = "; ".join(f"{item.source} ({item.group}) requires {item.requirement.specifier}" for item in declarations)
            findings.append(DiagnosticFinding("ERROR", "Declared dependency conflict", declarations[0].requirement.name,
                f"Project declarations cannot be satisfied together: {details}.",
                "Revise the incompatible project constraints. No safe single install command can be determined."))
    for notice in analysis.notices:
        findings.append(DiagnosticFinding("WARNING", "Unresolved project declaration", "project", notice,
            "Review the named file or declaration; it was not silently ignored."))
    return findings


def build_project_graph(analysis: ProjectAnalysis, packages: Iterable[Distribution]) -> DependencyGraph:
    installed = {canonicalize_name(p.metadata["Name"]): p for p in packages}
    root = analysis.path.name or str(analysis.path)
    nodes = {root: "target project"}
    edges = []
    seen_edges: set[tuple[str, str]] = set()
    for item in analysis.requirements:
        name = item.requirement.name
        package = installed.get(canonicalize_name(name))
        nodes[name] = package.version if package else "declared (not installed)"
        label = str(item.requirement.specifier) or item.group
        edge_key = (canonicalize_name(name), label)
        if edge_key not in seen_edges:
            seen_edges.add(edge_key)
            edges.append((root, name, label))
    return DependencyGraph(nodes, edges)


def summary_counts(findings: Iterable[DiagnosticFinding]) -> dict[str, int]:
    counts = {"ERROR": 0, "WARNING": 0, "INFO": 0}
    for finding in findings:
        counts[finding.severity] += 1
    return counts


def format_finding(finding: DiagnosticFinding) -> list[str]:
    lines = [f"[{finding.severity}] {finding.title}", f"Package: {finding.package}"]
    if finding.required_version:
        lines.append(f"Required version: {finding.required_version}")
    lines.append(f"Installed version: {finding.installed_version or 'Not found'}")
    if finding.latest_version:
        lines.append(f"Latest version: {finding.latest_version}")
    lines.extend([f"Reason: {finding.reason}", "Recommended action:", finding.action])
    if finding.command:
        lines.extend(["Suggested command (review before running):", finding.command])
    return lines


def recommendations_report(findings: Iterable[DiagnosticFinding]) -> str:
    findings = list(findings)
    counts = summary_counts(findings)
    lines = ["PYTHON DEPENDENCY RECOMMENDATIONS", "=" * 34, "", "DIAGNOSTIC SUMMARY", "-" * 18,
             f"Errors: {counts['ERROR']}", f"Warnings: {counts['WARNING']}", f"Informational: {counts['INFO']}", ""]
    if not findings:
        lines.append("No findings. The checked dependency information is healthy.")
    for finding in findings:
        lines.extend(format_finding(finding))
        lines.append("")
    return "\n".join(lines) + "\n"


def print_findings(findings: Iterable[DiagnosticFinding]) -> None:
    for finding in findings:
        print(f"[{finding.severity}] {finding.title}: {finding.package}")
        print(f"  {finding.action}")
        if finding.command:
            print(f"  Suggested command: {finding.command}")


def build_dependency_graph(packages: Iterable[Distribution], root_package: str | None = None) -> DependencyGraph:
    package_list = list(packages)
    installed = {canonicalize_name(p.metadata["Name"]): (p.metadata["Name"], p.version) for p in package_list}
    edges: list[tuple[str, str, str]] = []
    for dependency, entries in active_requirements(package_list).items():
        if dependency in installed:
            for source, requirement in entries:
                edges.append((source, installed[dependency][0], str(requirement.specifier)))
    if root_package is None:
        return DependencyGraph({name: version for name, version in installed.values()}, edges)
    root = installed.get(canonicalize_name(root_package))
    if root is None:
        raise ValueError(f"'{root_package}' is not installed in this environment.")
    reachable = {root[0]}
    pending = [root[0]]
    while pending:
        source = pending.pop()
        for edge_source, target, _ in edges:
            if edge_source == source and target not in reachable:
                reachable.add(target)
                pending.append(target)
    return DependencyGraph({name: version for name, version in installed.values() if name in reachable}, [edge for edge in edges if edge[0] in reachable and edge[1] in reachable])


def graph_as_dot(graph: DependencyGraph) -> str:
    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')
    lines = ["digraph dependencies {", "  rankdir=LR;", '  node [shape=box, style="rounded,filled", fillcolor="#E8F0FE"];']
    for name, version in sorted(graph.nodes.items(), key=lambda item: item[0].lower()):
        lines.append(f'  "{escape(name)}" [label="{escape(name)}\\n{escape(version)}"];')
    for source, target, constraint in graph.edges:
        label = f' [label="{escape(constraint)}"]' if constraint else ""
        lines.append(f'  "{escape(source)}" -> "{escape(target)}"{label};')
    return "\n".join(lines + ["}"]) + "\n"


def write_graph(packages: Iterable[Distribution], output_file: Path, root_package: str | None = None) -> None:
    graph = build_dependency_graph(packages, root_package)
    output_file.write_text(graph_as_dot(graph), encoding="utf-8")
    print(f"Created {output_file} with {len(graph.nodes)} package(s) and {len(graph.edges)} dependency link(s).")


def create_png(dot_file: Path, png_file: Path) -> None:
    dot = shutil.which("dot") or (r"C:\Program Files\Graphviz\bin\dot.exe" if Path(r"C:\Program Files\Graphviz\bin\dot.exe").exists() else None)
    if dot is None:
        print("PNG skipped: Graphviz was not found. The DOT graph was still created.")
        return
    result = subprocess.run([dot, "-Tpng", str(dot_file), "-o", str(png_file)], capture_output=True, text=True)
    print(f"Created PNG graph: {png_file}" if result.returncode == 0 else f"PNG graph could not be created: {result.stderr.strip()}")


def diagnostic_report(packages: Iterable[Distribution], findings: Iterable[DiagnosticFinding], include_updates: bool) -> str:
    package_list = list(packages)
    findings = list(findings)
    counts = summary_counts(findings)
    lines = ["PYTHON PACKAGE DEPENDENCY CHECKER REPORT", "=" * 40,
             f"Generated: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
             f"Packages scanned: {len(package_list)}", "", "DIAGNOSTIC SUMMARY", "-" * 18,
             f"Errors: {counts['ERROR']}", f"Warnings: {counts['WARNING']}", f"Informational: {counts['INFO']}",
             "", "FINDINGS AND RECOMMENDATIONS", "-" * 28]
    if findings:
        for finding in findings:
            lines.extend(format_finding(finding))
            lines.append("")
    else:
        lines.append("No findings. Dependency health, conflict, and update checks completed without issues." if include_updates else "No local dependency health or conflict issues found.")
    return "\n".join(lines) + "\n"


def project_diagnostic_report(analysis: ProjectAnalysis, packages: Iterable[Distribution], findings: Iterable[DiagnosticFinding]) -> str:
    content = diagnostic_report(packages, findings, False)
    def source_file(source: str) -> str:
        filename, separator, line = source.rpartition(":")
        return filename if separator and line.isdigit() else source
    sources = sorted({source_file(item.source) for item in analysis.requirements})
    header = ["TARGET PROJECT ANALYSIS", "=" * 23, f"Target project: {analysis.path}",
              f"Inspected environment: {analysis.environment_label}",
              f"Dependency files: {', '.join(sources) if sources else 'none'}",
              f"Declarations analyzed: {len(analysis.requirements)}", "",
              "Installed-package findings apply only to the inspected environment; declarations are project data.", ""]
    return "\n".join(header) + content


def write_report(output_file: Path, content: str, label: str) -> None:
    output_file.write_text(content, encoding="utf-8")
    print(f"Created {label}: {output_file}")


def run_all(packages: Iterable[Distribution], analysis: ProjectAnalysis | None = None) -> None:
    package_list = list(packages)
    output_folder = Path("test_results")
    output_folder.mkdir(exist_ok=True)
    print("Running complete dependency check...\n")
    findings, _ = build_diagnostics(package_list, include_updates=True)
    if analysis:
        findings = build_project_diagnostics(analysis, package_list) + findings
    print_findings(findings)
    if analysis:
        graph = build_project_graph(analysis, package_list)
        (output_folder / "all_packages.dot").write_text(graph_as_dot(graph), encoding="utf-8")
        print(f"Created {output_folder / 'all_packages.dot'} with target project declarations.")
    else:
        write_graph(package_list, output_folder / "all_packages.dot")
    create_png(output_folder / "all_packages.dot", output_folder / "all_packages.png")
    report = project_diagnostic_report(analysis, package_list, findings) if analysis else diagnostic_report(package_list, findings, True)
    write_report(output_folder / "dependency_report.txt", report, "diagnostic report")
    write_report(output_folder / "recommendations.txt", recommendations_report(findings), "recommendations report")
    print(f"\nAll generated files are in: {output_folder.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect installed Python packages and dependency health.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--package", help="Inspect one installed package.")
    parser.add_argument("--project", metavar="PATH", help="Analyze dependency declarations for this project directory.")
    parser.add_argument("--check", action="store_true", help="Check local dependency health.")
    parser.add_argument("--conflicts", action="store_true", help="Find declared version conflicts.")
    parser.add_argument("--outdated", action="store_true", help="Check PyPI for updates.")
    parser.add_argument("--graph", action="store_true", help="Create a Graphviz DOT graph.")
    parser.add_argument("--output", default="dependency_graph.dot")
    parser.add_argument("--report", action="store_true", help="Create a diagnostic report.")
    parser.add_argument("--include-outdated", action="store_true")
    parser.add_argument("--all", action="store_true", help="Run checks and create reports plus DOT/PNG graphs.")
    args = parser.parse_args()
    try:
        analysis = analyze_project(args.project) if args.project else None
    except ValueError as error:
        parser.error(str(error))
    packages, environment = packages_from_python(analysis.environment_python) if analysis else (installed_packages(), "current interpreter")
    if analysis:
        analysis = ProjectAnalysis(analysis.path, analysis.requirements, analysis.notices, analysis.environment_python, environment)
    selected = [p for p in packages if p.metadata["Name"].lower() == args.package.lower()] if args.package else packages
    if args.package and not selected:
        parser.error(f"'{args.package}' is not installed in this environment.")
    if args.all:
        run_all(selected, analysis)
    elif args.check:
        findings = (build_project_diagnostics(analysis, selected) if analysis else [finding for finding in build_diagnostics(selected)[0] if finding.title != "Dependency version conflict"])
        if findings:
            print_findings(findings)
        else:
            print("Dependency health check passed: no missing or incompatible requirements.")
    elif args.conflicts:
        findings = build_project_diagnostics(analysis, selected) if analysis else build_diagnostics(selected)[0]
        print_findings([f for f in findings if "conflict" in f.title.lower()])
    elif args.outdated:
        findings = build_diagnostics(selected, include_updates=True)[0]
        print_findings((build_project_diagnostics(analysis, selected) if analysis else []) + findings)
    elif args.graph:
        if analysis:
            output = Path(args.output)
            output.write_text(graph_as_dot(build_project_graph(analysis, selected)), encoding="utf-8")
            print(f"Created {output} with declared dependencies for {analysis.path}.")
        else:
            write_graph(selected, Path(args.output), args.package)
    elif args.report:
        findings, _ = build_diagnostics(selected, args.include_outdated)
        if analysis: findings = build_project_diagnostics(analysis, selected) + findings
        content = project_diagnostic_report(analysis, selected, findings) if analysis else diagnostic_report(selected, findings, args.include_outdated)
        write_report(Path("dependency_report.txt"), content, "diagnostic report")
        write_report(Path("recommendations.txt"), recommendations_report(findings), "recommendations report")
    elif args.package:
        print_package(selected[0])
    elif analysis:
        print(f"Target project: {analysis.path}\nInspected environment: {analysis.environment_label}\nDeclarations: {len(analysis.requirements)}")
    else:
        print(f"Found {len(packages)} installed packages:\n")
        for package in packages:
            print_package(package)


if __name__ == "__main__":
    main()
