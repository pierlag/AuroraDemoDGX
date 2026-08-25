"""Contrôle rapide de la configuration tunnel + GitHub.

Exécution : .venv/bin/python scripts/check_share.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import DEMO_ISSUE_TITLE, GITHUB_CLIENT_ID, GITHUB_TOKEN_FILE  # noqa: E402
from backend.github_client import REPO_RE, session  # noqa: E402
from backend.tunnel import tunnel  # noqa: E402


def main() -> int:
    print("=== Configuration ===")
    print(f"  GITHUB_CLIENT_ID : {GITHUB_CLIENT_ID or 'ABSENT'}")
    print(f"  Titre de l'issue : {DEMO_ISSUE_TITLE}")
    print(f"  Jeton stocké     : {GITHUB_TOKEN_FILE}")

    snap = session.snapshot()
    print()
    print("=== Session GitHub ===")
    for key in ("configured", "authenticated", "pending", "user"):
        print(f"  {key:15} : {snap[key]}")

    print()
    print("=== Fournisseurs de tunnel ===")
    for provider in tunnel.available_providers():
        state = "disponible" if provider["available"] else "absent"
        detail = provider["path"] or provider["install"]
        print(f"  {provider['id']:12} {state:11} {detail}")

    print()
    print("=== Validation des noms de dépôt ===")
    cases = [
        ("octocat/hello-world", True),
        ("mon-org/mon.repo_2", True),
        ("../../etc/passwd", False),
        ("owner/repo/issues", False),
        ("owner", False),
        ("owner/repo?a=1", False),
    ]
    ok = True
    for value, expected in cases:
        got = bool(REPO_RE.match(value))
        flag = "OK " if got == expected else "ÉCHEC"
        if got != expected:
            ok = False
        print(f"  [{flag}] {value:24} accepté={got}")

    print()
    print("RÉSULTAT :", "conforme" if ok else "des écarts subsistent")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
