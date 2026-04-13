from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_deploy_production_entrypoint_exists() -> None:
    script = (REPO_ROOT / "scripts" / "deploy_production.sh").read_text(encoding="utf-8")
    assert "git checkout main" in script
    assert "git pull --ff-only origin main" in script
    assert "npm run build:frontend" in script
    assert "packages/frontend/dist-data" in script
    assert "rsync -a --delete" in script
    assert "start_backend.sh" in script
    assert "https://data.openecon.ai/api/health" in script


def test_start_backend_uses_dynamic_project_root_and_repo_logs() -> None:
    script = (REPO_ROOT / "scripts" / "start_backend.sh").read_text(encoding="utf-8")
    assert 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' in script
    assert 'PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"' in script
    assert ".omx/logs" in script
    assert 'HEALTH_MAX_WAIT_SECONDS="${HEALTH_MAX_WAIT_SECONDS:-180}"' in script
    assert "/tmp/backend-" not in script


def test_frontend_public_assets_use_data_openecon_ai() -> None:
    for rel_path in [
        "packages/frontend/index.html",
        "packages/frontend/public/robots.txt",
        "packages/frontend/public/sitemap.xml",
        "packages/frontend/public/llms.txt",
        "packages/frontend/public/.well-known/ai-plugin.json",
        "packages/frontend/public/.well-known/security.txt",
        "packages/frontend/src/components/LandingPage.tsx",
        "packages/frontend/src/pages/ExamplesPage.tsx",
        "packages/frontend/src/pages/DocsPage.tsx",
    ]:
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        assert "data.openecon.ai" in text
