"""Vanilla 웹의 정적 제공, 외부 의존성 및 위험한 HTML 삽입을 검증한다."""

import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.main import create_app

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class Assets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: list[str] = []
        self.tabs: list[dict] = []
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        values = dict(attrs)
        if tag == "script":
            assert values.get("type") == "module"
            self.assets.append(values.get("src", ""))
        if tag == "link" and values.get("rel") == "stylesheet":
            self.assets.append(values.get("href", ""))
        assert not any(key.startswith("on") for key in values)
        assert tag not in ("iframe", "img")
        if values.get("role") == "tab":
            self.tabs.append(values)
        if "id" in values:
            self.ids.append(values["id"])


def test_static_web_assets_and_accessibility() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        parsed = Assets()
        parsed.feed(response.text)
        assert not parsed.tabs
        assert {"subject-bar", "conversations", "history-content", "turns", "chat-form",
                "composer", "sidebar-toggle", "sidebar-close", "drawer-backdrop", "set-picker"} <= set(parsed.ids)
        assert not {"evidence-panel", "evidence-drawer", "subject-picker", "change-subject"} & set(parsed.ids)
        assert re.search(r'<aside[^>]*id="sidebar"[^>]*hidden', response.text)
        assert 'aria-expanded="false"' in response.text
        assert all(label in response.text for label in ("대화 기록", "내가 가진 레고", "이 세트 이야기 기록"))
        assert len(parsed.ids) == len(set(parsed.ids))
        assert 'aria-live="polite"' in response.text
        assert 'lang="ko"' in response.text
        for asset in parsed.assets:
            assert asset.startswith("/") and not asset.startswith("//")
            assert client.get(asset).status_code == 200
        for path in (WEB / "js").glob("*.js"):
            result = client.get(f"/js/{path.name}")
            assert result.status_code == 200
            assert "javascript" in result.headers["content-type"]


def test_no_html_injection_external_code_or_dependencies() -> None:
    for path in (WEB / "js").glob("*.js"):
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|DOMParser|\beval\s*\(", source)
        imports = re.findall(r'\bfrom\s+[\'"]([^\'"]+)', source)
        assert all(item.startswith("./") and item.endswith(".js") for item in imports)
        assert not re.search(r"https?://|cdn\.", source)
    css = (WEB / "css/app.css").read_text(encoding="utf-8")
    assert "@import" not in css and "url(" not in css
    assert "prefers-color-scheme: dark" in css
    assert "max-width: 820px" in css
    assert "height: 100dvh" in css
    assert "#workspace { flex: 1 1 0; min-height: 0; overflow-y: auto; }" in css
    assert "#composer { position: sticky; bottom: 0; flex: 0 0 auto;" in css
    assert "width: min(400px, 92vw)" in css
    assert "grid-template-columns: minmax(0, 1fr)" in css
    assert "min-width: 0" in css and "overflow-wrap: anywhere" in css
    # 실제 렌더링은 PM이 실측하고, 여기서는 가로 넘침을 막는 CSS 계약을 확인한다.
    assert not re.search(r"min-width:\s*[3-9]\d\dpx|width:\s*(?:100vw|1[2-9]\d\dpx)", css)
    assert not (WEB / "package.json").exists()
    assert not (WEB / "node_modules").exists()


def test_node_web_suite() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node가 없어 JS 테스트만 건너뜁니다.")
    # Node 24는 디렉터리 인수를 테스트 파일로 취급하므로 파일을 명시한다.
    files = sorted(str(path) for path in (WEB / "tests").glob("*.test.mjs"))
    assert files
    result = subprocess.run([node, "--test", *files], cwd=ROOT, capture_output=True,
                            text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
