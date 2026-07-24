"""Guard the SPA's inline JavaScript against Python string-escaping bugs.

The whole UI is one inline <script> block inside the _HTML Python string.
A single JS syntax error (e.g. a \' that Python consumes before the browser
sees it) kills every handler in the app. Regression test for the v3.4.1+
incident where the Retrieval tab shipped with invalid escapes and the entire
Explorer became unresponsive.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module

# Escapes Python resolves inside a regular (non-raw) string. A single
# backslash before any of these inside _HTML never reaches the browser —
# JS escapes must be written with a double backslash (\\' / \\n).
_PYTHON_ESCAPE_CHARS = "'\"ntrbfva0"


def _html_source_region() -> str:
    source = Path(app_module.__file__).read_text()
    match = re.search(r'_HTML = """(.*?)"""', source, re.DOTALL)
    assert match, "expected _HTML triple-quoted string in app.py"
    return match.group(1)


def test_html_source_uses_double_backslash_js_escapes():
    region = _html_source_region()
    offenders = []
    for match in re.finditer(r"(\\+)(.)", region):
        backslashes, following = match.group(1), match.group(2)
        if len(backslashes) % 2 == 1 and following in _PYTHON_ESCAPE_CHARS:
            line_no = region.count("\n", 0, match.start()) + 1
            context = region[max(0, match.start() - 40):match.start() + 40].strip()
            offenders.append(f"_HTML line {line_no}: ...{context}...")
    assert not offenders, (
        "Python will consume these escapes before the browser sees them; "
        "use a double backslash for JS escapes:\n" + "\n".join(offenders)
    )


def _inline_scripts() -> list[str]:
    response = TestClient(app_module.app).get("/")
    assert response.status_code == 200
    scripts = re.findall(r"<script>(.*?)</script>", response.text, re.DOTALL)
    assert scripts, "expected at least one inline <script> block in the SPA"
    return scripts


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_spa_scripts_parse_as_javascript(tmp_path):
    for index, script in enumerate(_inline_scripts()):
        path = tmp_path / f"spa_script_{index}.js"
        path.write_text(script)
        proc = subprocess.run(
            ["node", "--check", str(path)], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, f"inline script {index} fails to parse:\n{proc.stderr}"
