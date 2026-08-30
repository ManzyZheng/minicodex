from pathlib import Path

from minicodex.shell_analysis import ShellCommandAnalyzer


def test_analyzer_identifies_common_command_operations(tmp_path: Path) -> None:
    analyzer = ShellCommandAnalyzer(tmp_path)

    assert analyzer.analyze("python -m pytest -q").operations == ("verification.test",)
    assert analyzer.analyze("npm install").operations == ("package.install",)
    assert analyzer.analyze("git push origin main").operations == ("git.push",)
    assert analyzer.analyze("python scripts/task.py").operations == ("process.execute",)


def test_analyzer_resolves_delete_targets_and_marks_dynamic_commands(tmp_path: Path) -> None:
    analyzer = ShellCommandAnalyzer(tmp_path)

    local = analyzer.analyze("Remove-Item -LiteralPath '.tmp-pytest' -Recurse -Force")
    outside = analyzer.analyze("Remove-Item -LiteralPath '..\\outside' -Recurse -Force")
    dynamic = analyzer.analyze("Remove-Item -LiteralPath $target -Recurse -Force")

    assert local.operations == ("filesystem.delete",)
    assert local.targets[0].relation == "inside_workspace"
    assert local.targets[0].generated
    assert outside.targets[0].relation == "outside_workspace"
    assert dynamic.targets[0].relation == "dynamic"
    assert not dynamic.fully_analyzed


def test_analyzer_splits_compound_commands_without_splitting_quoted_text(tmp_path: Path) -> None:
    analysis = ShellCommandAnalyzer(tmp_path).analyze(
        'python -c "print(\'a;b\')" && git status'
    )

    assert analysis.operations == ("process.execute", "git.read")
    assert len(analysis.segments) == 2


def test_analyzer_keeps_posix_absolute_delete_target_and_flags_parent_traversal(tmp_path: Path) -> None:
    absolute = ShellCommandAnalyzer(tmp_path).analyze("rm -rf /tmp/minicodex-cache")
    traversal = ShellCommandAnalyzer(tmp_path).analyze("Set-Content ..\\outside.txt value")

    assert absolute.targets[0].raw == "/tmp/minicodex-cache"
    assert absolute.targets[0].relation == "outside_workspace"
    assert "relative_parent_path" in traversal.signals


def test_analyzer_keeps_all_delete_targets_and_recognizes_common_wrappers(tmp_path: Path) -> None:
    analyzer = ShellCommandAnalyzer(tmp_path)

    multiple = analyzer.analyze("Remove-Item important.txt .tmp-pytest -Force")
    nested = analyzer.analyze("bash -lc 'echo ok'")
    substitution = analyzer.analyze("echo $(curl https://example.com)")

    assert [target.raw for target in multiple.targets] == ["important.txt", ".tmp-pytest"]
    assert nested.operations == ("shell.nested",)
    assert not substitution.fully_analyzed


def test_analyzer_recognizes_dangerous_git_variants_and_delete_aliases(tmp_path: Path) -> None:
    analyzer = ShellCommandAnalyzer(tmp_path)

    assert analyzer.analyze("git -C . reset --hard").operations == ("git.reset_hard",)
    assert analyzer.analyze("git clean -d -f").operations == ("git.clean_force",)
    assert analyzer.analyze("rd /s /q old_module").operations == ("filesystem.delete",)
    assert analyzer.analyze("ri old_module -Recurse -Force").operations == ("filesystem.delete",)


def test_analyzer_conservatively_recognizes_indirect_deletes_and_shell_wrappers(tmp_path: Path) -> None:
    analyzer = ShellCommandAnalyzer(tmp_path)

    foreach = analyzer.analyze("Get-ChildItem old | ForEach-Object { Remove-Item -Recurse -Force $_ }")
    xargs = analyzer.analyze("find old -type f | xargs rm -f")
    find_delete = analyzer.analyze("find old -delete")
    wrapped = analyzer.analyze("env bash -c 'rm -rf old'")

    assert "filesystem.delete" in foreach.operations
    assert "filesystem.delete" in xargs.operations
    assert find_delete.operations == ("filesystem.delete",)
    assert wrapped.operations == ("shell.nested",)
