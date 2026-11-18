"""설정 경로와 비밀 파일 비접근 회귀 검사."""

from pathlib import Path

import pytest

from rag_service.config import load_settings, project_root, resolve_path


def test_root_and_relative_paths(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    root = Path(__file__).resolve().parents[1]
    assert project_root() == root
    assert resolve_path("data/lego.sqlite") == root / "data" / "lego.sqlite"
    assert resolve_path(tmp_path) == tmp_path
    settings = load_settings()
    assert settings["scope"]["root_theme"] == "Star Wars"
    assert settings["collections"]["items"] == "set_meta"


def test_settings_only_opens_yaml(monkeypatch):
    original = Path.open
    opened = []

    def tracked_open(path, *args, **kwargs):
        opened.append(path)
        assert path.name != ".env"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    load_settings()
    assert opened == [project_root() / "config" / "settings.yaml"]


def test_invalid_settings(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("- invalid\n", encoding="utf-8")
    with pytest.raises(ValueError, match="YAML"):
        load_settings(path)


def test_server_settings_uses_shared_loader(monkeypatch):
    from rag_service import services

    expected = {"keepalive_sec": 17}
    monkeypatch.setattr(services, "load_settings", lambda: {"server": expected})
    assert services.server_settings() == expected


def test_server_does_not_import_yaml():
    import ast

    for path in (project_root() / "server").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] != "yaml" for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "yaml"
