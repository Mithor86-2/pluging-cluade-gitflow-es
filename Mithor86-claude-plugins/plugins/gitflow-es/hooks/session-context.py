#!/usr/bin/env python3
"""
gitflow-es context hook (SessionStart).

Imprime un resumen del estado git al inicio de cada sesión. Solo informativo —
nunca bloquea nada. La salida se inyecta en el contexto para que Claude arranque
sabiendo en qué rama está el usuario, si hay cambios pendientes, etc.

Si no estamos en un repo git, no imprime nada (salida vacía).
"""

from __future__ import annotations

import subprocess
import sys
from typing import List, Optional


def run(cmd: List[str]) -> Optional[str]:
    """Ejecuta un comando y devuelve stdout, o None si falla."""
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def in_git_repo() -> bool:
    return run(["git", "rev-parse", "--is-inside-work-tree"]) == "true"


def gitflow_initialized() -> bool:
    """True si el repo tiene git-flow inicializado (config gitflow.branch.develop)."""
    return bool(run(["git", "config", "--get", "gitflow.branch.develop"]))


def repo_has_commits() -> bool:
    """True si el repo tiene al menos un commit."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def branch_type(branch: str) -> Optional[str]:
    """Detecta el tipo GitFlow a partir del prefijo."""
    prefixes = {
        "feature/": "feature",
        "fix/": "fix",
        "hotfix/": "hotfix",
        "release/": "release",
        "refactor/": "refactor",
        "chore/": "chore",
    }
    for prefix, kind in prefixes.items():
        if branch.startswith(prefix):
            return kind
    return None


def ahead_behind(branch: str) -> Optional[tuple]:
    """Devuelve (ahead, behind) respecto a origin/<branch>, o None si no aplica."""
    if not branch:
        return None
    upstream = run(["git", "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"])
    if not upstream:
        return None
    counts = run(["git", "rev-list", "--left-right", "--count", f"{upstream}...HEAD"])
    if not counts:
        return None
    parts = counts.split()
    if len(parts) != 2:
        return None
    try:
        behind, ahead = int(parts[0]), int(parts[1])
        return ahead, behind
    except ValueError:
        return None


def main() -> None:
    if not in_git_repo():
        return  # Sin salida = no se inyecta nada

    # Caso especial: repo recién inicializado sin commits.
    # No tiene sentido mostrar "rama actual" con ahead/behind; guiamos al
    # usuario hacia el primer commit y luego hacia `git flow init -d`.
    if not repo_has_commits():
        out = [
            "## Estado GitFlow del repo",
            "",
            "### ⚠️ Repo recién inicializado (sin commits)",
            "",
            "Este repo todavía no tiene ningún commit. Git necesita al menos "
            "un commit inicial antes de poder crear ramas o inicializar "
            "git-flow.",
            "",
            "**Flujo sugerido** (proponérselo al usuario, pedir OK y ejecutar):",
            "",
            "```bash",
            "git add .",
            'git commit -m "chore: initial commit"',
            "git flow init -d",
            "```",
            "",
            "El hook de safety permite este primer commit en `main`/`master` "
            "como excepción explícita. A partir del segundo commit vuelve a "
            "aplicar la regla normal (solo commits desde ramas de trabajo).",
        ]
        print("\n".join(out))
        return

    branch = run(["git", "branch", "--show-current"]) or "(detached HEAD)"
    kind = branch_type(branch)
    status_lines = run(["git", "status", "--porcelain"]) or ""
    num_changes = len([l for l in status_lines.splitlines() if l.strip()])

    out = []
    out.append("## Estado GitFlow del repo")
    out.append("")
    out.append(f"- **Rama actual:** `{branch}`")

    if branch in {"main", "master", "develop"}:
        out.append(
            f"- ⚠️ Estás parado en `{branch}`. No modifiques archivos aquí; "
            f"usa `/git start <tipo> <descripcion>` para crear una rama de trabajo."
        )
    elif kind:
        out.append(f"- **Tipo GitFlow:** `{kind}`")
    elif branch != "(detached HEAD)":
        out.append(f"- La rama no usa un prefijo GitFlow reconocido.")

    if num_changes > 0:
        out.append(f"- **Cambios pendientes:** {num_changes} archivo(s)")
    else:
        out.append("- Working tree limpio")

    ab = ahead_behind(branch)
    if ab is not None:
        ahead, behind = ab
        if ahead == 0 and behind == 0:
            out.append("- Sincronizada con `origin`")
        elif ahead > 0 and behind == 0:
            out.append(f"- {ahead} commit(s) por delante de `origin`")
        elif behind > 0 and ahead == 0:
            out.append(f"- {behind} commit(s) por detrás de `origin`")
        else:
            out.append(f"- Divergida: {ahead} adelante / {behind} atrás de `origin`")

    # Aviso si git-flow no está inicializado (tono proactivo: Claude puede
    # ofrecer correr `git flow init -d` después de pedir OK al usuario).
    if not gitflow_initialized():
        out.append("")
        out.append("### ⚠️ git-flow no inicializado")
        out.append(
            "Este repo no tiene git-flow configurado todavía. Los subcomandos "
            "`git flow feature|hotfix|release start/finish` no funcionarán "
            "hasta que se inicialice."
        )
        out.append("")
        out.append(
            "**Acción sugerida:** preguntarle al usuario si quiere "
            "inicializarlo ahora con los defaults del equipo:"
        )
        out.append("")
        out.append("```bash")
        out.append("git flow init -d")
        out.append("```")
        out.append("")
        out.append(
            "Eso configura `main` como rama de producción, `develop` como "
            "rama de integración, y los prefijos estándar (`feature/`, "
            "`hotfix/`, `release/`, `support/`). Si el repo aún no tiene "
            "rama `develop`, git-flow la creará."
        )

    print("\n".join(out))


if __name__ == "__main__":
    main()
