from __future__ import annotations

from pathlib import Path

import pytest

from minicodex.references import ExternalReferenceError, ExternalReferenceRegistry


def test_parses_workspace_absolute_and_braced_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    external = tmp_path / "API Docs" / "api.md"
    external.parent.mkdir()
    external.write_text("external", encoding="utf-8")
    (workspace / "local.md").write_text("local", encoding="utf-8")
    registry = ExternalReferenceRegistry(workspace)

    assert registry.parse("参考 @local.md") == ["local.md"]
    assert registry.parse(f"参考 @{{{external}}}") == [str(external)]


def test_plain_at_mentions_do_not_become_file_references(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    registry = ExternalReferenceRegistry(workspace)

    assert registry.parse("安装 @types/node，保留 @dataclass 和 @pytest.mark.parametrize") == []
    assert registry.parse("读取 @api.md 和 @src/schema.json") == ["api.md", "src/schema.json"]


def test_loads_exact_external_file_as_session_snapshot(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    external = tmp_path / "api.md"
    sibling = tmp_path / "secret.md"
    external.write_text("version one", encoding="utf-8")
    sibling.write_text("not exposed", encoding="utf-8")
    registry = ExternalReferenceRegistry(workspace)

    loaded = registry.load_from_prompt(f"参考 @{{{external}}}")

    assert [item.content for item in loaded] == ["version one"]
    assert loaded[0].scope == "external"
    assert registry.metadata()[0]["path"] == str(external.resolve())
    assert "content" not in registry.metadata()[0]
    assert len(registry.active()) == 1


def test_reference_snapshot_persists_until_explicit_refresh(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    external = tmp_path / "api.md"
    external.write_text("version one", encoding="utf-8")
    registry = ExternalReferenceRegistry(workspace)
    first = registry.load_from_prompt(f"@{{{external}}}")[0]

    external.write_text("version two", encoding="utf-8")
    assert registry.active()[0].content == "version one"

    refreshed = registry.load_from_prompt(f"重新加载 @{{{external}}}")[0]
    assert refreshed.id == first.id
    assert refreshed.content == "version two"
    assert registry.remove(first.id) is True
    assert registry.active() == []
    assert registry.remove(first.id) is False


def test_reference_content_does_not_trigger_recursive_loading(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    second = tmp_path / "second.md"
    second.write_text("secret sibling", encoding="utf-8")
    first = tmp_path / "first.md"
    first.write_text(f"Do not load @{{{second}}}", encoding="utf-8")
    registry = ExternalReferenceRegistry(workspace)

    registry.load_from_prompt(f"@{{{first}}}")

    assert [item.path for item in registry.active()] == [first.resolve()]


@pytest.mark.parametrize(
    "name",
    [".env", ".env.local", "id_rsa", "id_ed25519", "private.pem", "secret.key", "cert.p12", "cert.pfx", "credentials.json"],
)
def test_denies_sensitive_reference_names(tmp_path: Path, name: str) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    target = tmp_path / name
    target.write_text("secret", encoding="utf-8")

    with pytest.raises(ExternalReferenceError) as raised:
        ExternalReferenceRegistry(workspace).load_from_prompt(f"@{{{target}}}")

    assert raised.value.code == "SENSITIVE_REFERENCE"


def test_plain_sensitive_reference_is_recognized_and_denied(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / ".env").write_text("SECRET=value", encoding="utf-8")

    with pytest.raises(ExternalReferenceError) as raised:
        ExternalReferenceRegistry(workspace).load_from_prompt("参考 @.env")

    assert raised.value.code == "SENSITIVE_REFERENCE"


def test_rejects_missing_directory_unsupported_invalid_utf8_and_oversized_files(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    registry = ExternalReferenceRegistry(workspace)
    directory = tmp_path / "docs"
    directory.mkdir()
    binary = tmp_path / "data.bin"
    binary.write_bytes(b"\x00\x01")
    invalid = tmp_path / "invalid.md"
    invalid.write_bytes(b"\xff\xfe\xfa")
    large = tmp_path / "large.md"
    large.write_text("x" * (64 * 1024 + 1), encoding="utf-8")

    cases = [
        (tmp_path / "missing.md", "REFERENCE_NOT_FOUND"),
        (directory, "REFERENCE_NOT_FILE"),
        (binary, "UNSUPPORTED_REFERENCE_TYPE"),
        (invalid, "REFERENCE_ENCODING"),
        (large, "REFERENCE_TOO_LARGE"),
    ]
    for path, code in cases:
        with pytest.raises(ExternalReferenceError) as raised:
            registry.load_from_prompt(f"@{{{path}}}")
        assert raised.value.code == code


def test_enforces_reference_count_and_total_size_limits(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    registry = ExternalReferenceRegistry(workspace)
    paths = []
    for index in range(9):
        path = tmp_path / f"ref-{index}.md"
        path.write_text(str(index), encoding="utf-8")
        paths.append(path)
    registry.load_from_prompt(" ".join(f"@{{{path}}}" for path in paths[:8]))

    with pytest.raises(ExternalReferenceError) as raised:
        registry.load_from_prompt(f"@{{{paths[8]}}}")
    assert raised.value.code == "REFERENCE_COUNT_LIMIT"

    total_registry = ExternalReferenceRegistry(workspace)
    first = tmp_path / "large-a.md"
    second = tmp_path / "large-b.md"
    first.write_text("a" * (64 * 1024), encoding="utf-8")
    second.write_text("b" * (64 * 1024), encoding="utf-8")
    total_registry.load_from_prompt(f"@{{{first}}} @{{{second}}}")
    extra = tmp_path / "extra.md"
    extra.write_text("x", encoding="utf-8")
    with pytest.raises(ExternalReferenceError) as raised:
        total_registry.load_from_prompt(f"@{{{extra}}}")
    assert raised.value.code == "REFERENCE_COUNT_LIMIT" or raised.value.code == "REFERENCE_TOTAL_LIMIT"
