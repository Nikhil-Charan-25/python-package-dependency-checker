import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError

from dependency_checker import (
    DependencyGraph,
    build_dependency_graph,
    build_diagnostics,
    analyze_project,
    build_project_diagnostics,
    build_project_graph,
    graph_as_dot,
    recommendations_report,
    summary_counts,
    write_report,
)


class FakeDistribution:
    def __init__(self, name, version, requirements=None):
        self.metadata = {"Name": name}
        self.version = version
        self.requires = requirements or []


class RecommendationTests(unittest.TestCase):
    def test_missing_dependency_has_error_and_install_command(self):
        findings, _ = build_diagnostics([FakeDistribution("app", "1.0", ["requests>=2.25.0"])])

        finding = findings[0]
        self.assertEqual(finding.severity, "ERROR")
        self.assertEqual(finding.title, "Missing dependency")
        self.assertEqual(finding.command, 'python -m pip install "requests>=2.25.0"')

    def test_incompatible_dependency_recommends_declared_constraint(self):
        packages = [
            FakeDistribution("app", "1.0", ["library<2"]),
            FakeDistribution("library", "2.1"),
        ]
        findings, _ = build_diagnostics(packages)

        finding = findings[0]
        self.assertEqual(finding.installed_version, "2.1")
        self.assertEqual(finding.command, 'python -m pip install "library<2"')

    def test_compatible_update_has_warning_and_upgrade_command(self):
        packages = [
            FakeDistribution("app", "1.0", ["library>=1,<4"]),
            FakeDistribution("library", "1.0"),
        ]
        findings, _ = build_diagnostics(packages, True, lambda name: "2.0")

        update = next(f for f in findings if f.title == "Compatible update available")
        self.assertEqual(update.severity, "WARNING")
        self.assertEqual(update.command, "python -m pip install --upgrade library")

    def test_unconfirmed_update_is_info_without_command(self):
        findings, _ = build_diagnostics([FakeDistribution("library", "1.0")], True, lambda name: "2.0")

        self.assertEqual(findings[0].severity, "INFO")
        self.assertIsNone(findings[0].command)

    def test_network_problem_becomes_warning(self):
        def unavailable(name):
            raise URLError("offline")

        findings, _ = build_diagnostics([FakeDistribution("library", "1.0")], True, unavailable)

        self.assertEqual(findings[0].title, "Unable to check package update")
        self.assertEqual(findings[0].severity, "WARNING")

    def test_conflict_has_no_misleading_pip_command(self):
        packages = [
            FakeDistribution("app-a", "1.0", ["library<2"]),
            FakeDistribution("app-b", "1.0", ["library>=2"]),
        ]
        findings, _ = build_diagnostics(packages)

        conflict = next(finding for finding in findings if finding.title == "Dependency version conflict")
        self.assertIsNone(conflict.command)

    def test_summary_and_recommendation_report(self):
        findings, _ = build_diagnostics([FakeDistribution("app", "1.0", ["library>=2"])])

        self.assertEqual(summary_counts(findings), {"ERROR": 1, "WARNING": 0, "INFO": 0})
        report = recommendations_report(findings)
        self.assertIn("Errors: 1", report)
        self.assertIn("Suggested command (review before running):", report)

    def test_recommendations_file_is_written(self):
        findings, _ = build_diagnostics([FakeDistribution("app", "1.0", ["library>=2"])])
        output = Path("test_results") / "test_recommendations.txt"
        output.parent.mkdir(exist_ok=True)
        try:
            write_report(output, recommendations_report(findings), "recommendations report")
            self.assertTrue(output.exists())
            self.assertIn("Missing dependency", output.read_text(encoding="utf-8"))
        finally:
            if output.exists():
                output.unlink()


class GraphTests(unittest.TestCase):
    def test_graph_is_preserved(self):
        packages = [
            FakeDistribution("app", "1.0", ["library>=1"]),
            FakeDistribution("library", "1.0"),
        ]
        graph = build_dependency_graph(packages, "app")

        self.assertEqual(set(graph.nodes), {"app", "library"})
        self.assertIn('"app" -> "library" [label=">=1"]', graph_as_dot(graph))


class ProjectAnalysisTests(unittest.TestCase):
    def make_project(self, name="project"):
        test_root = Path(__file__).parent / "test_results"
        test_root.mkdir(exist_ok=True)
        directory = TemporaryDirectory(dir=test_root)
        path = Path(directory.name) / name
        path.mkdir()
        self.addCleanup(directory.cleanup)
        return path

    def test_requirements_includes_and_missing_dependency(self):
        path = self.make_project()
        (path / "requirements.txt").write_text("# base\nrequests>=999\n-r extra.txt\n", encoding="utf-8")
        (path / "extra.txt").write_text("flask>=3\n", encoding="utf-8")
        analysis = analyze_project(path)
        self.assertEqual({item.requirement.name for item in analysis.requirements}, {"requests", "flask"})
        findings = build_project_diagnostics(analysis, [])
        self.assertEqual(len([f for f in findings if f.title == "Missing declared dependency"]), 2)

    def test_pyproject_optional_dependencies_and_version_violation(self):
        path = self.make_project()
        (path / "pyproject.toml").write_text('[project]\ndependencies = ["library<2"]\n[project.optional-dependencies]\ntest = ["pytest>=8"]\n', encoding="utf-8")
        analysis = analyze_project(path)
        self.assertEqual({item.group for item in analysis.requirements}, {"default", "test"})
        findings = build_project_diagnostics(analysis, [FakeDistribution("library", "2.1")])
        self.assertTrue(any(f.title == "Declared version constraint violation" for f in findings))

    def test_incompatible_declarations_are_conflicts(self):
        path = self.make_project()
        (path / "requirements.txt").write_text("library<2\n", encoding="utf-8")
        (path / "pyproject.toml").write_text('[project]\ndependencies = ["library>=2"]\n', encoding="utf-8")
        findings = build_project_diagnostics(analyze_project(path), [])
        self.assertTrue(any(f.title == "Declared dependency conflict" for f in findings))

    def test_no_file_spaces_venv_and_invalid_declaration(self):
        path = self.make_project("project with spaces")
        (path / ".venv" / "Scripts").mkdir(parents=True)
        (path / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
        analysis = analyze_project(path)
        self.assertEqual(analysis.environment_python.name, "python.exe")
        self.assertTrue(any("No supported dependency file" in notice for notice in analysis.notices))
        (path / "requirements.txt").write_text("this is not a valid requirement !!!\n", encoding="utf-8")
        analysis = analyze_project(path)
        self.assertTrue(any("invalid declaration" in notice for notice in analysis.notices))

    def test_target_graph_is_declaration_based(self):
        path = self.make_project()
        (path / "requirements.txt").write_text("demo>=1\n", encoding="utf-8")
        graph = build_project_graph(analyze_project(path), [])
        self.assertIn(path.name, graph.nodes)
        self.assertEqual(graph.nodes["demo"], "declared (not installed)")

    def test_nonexistent_project_path_raises(self):
        with self.assertRaises(ValueError):
            analyze_project(Path("definitely-not-a-real-project"))


if __name__ == "__main__":
    unittest.main()
