#!/usr/bin/env python3
"""
gitflow-es safety hook (PreToolUse).

Intercepta y bloquea operaciones peligrosas antes de que se ejecuten. Cubre
dos familias de herramientas:

  (1) Bash — comandos git destructivos o prohibidos por la política del equipo.
  (2) Write / Edit / MultiEdit / NotebookEdit — modificaciones a archivos del
      repo mientras se está parado en `main` / `master` (regla: nunca modificar
      archivos sobre la rama de producción).

Si detecta una violación, sale con código 2 y escribe el motivo en stderr;
Claude Code bloquea la ejecución y reenvía el mensaje al modelo para que
informe al usuario y reconsidere.

Reglas aplicadas:
  Para Bash:
    1. git push con --force/--force-with-lease/-f sobre main/master/develop
    2. git commit estando en main/master
    3. git commit --no-verify (cualquier rama)
    4. git commit --author=... (regla del equipo)
    5. git reset --hard
    6. git clean -f / -fd / -xf (cualquier combinación con -f)
    7. git add/commit que incluya archivos .env o de credenciales
    8. git flow <feature|hotfix|release|support|bugfix> <sub> en un repo
       que no tiene git-flow inicializado (falta `git flow init`)

  Para Write/Edit/MultiEdit/NotebookEdit:
    9. Cualquier edición de archivo cuando la rama del repo que contiene el
       archivo es main o master (develop se permite — la excepción de commit
       directo en develop la maneja el skill `git` cuando el usuario lo
       solicita explícitamente).

Diseño:
  - Puro stdlib, compatible con Python 3.7+.
  - Si algo falla internamente, permite la acción (fail-open) para no
    convertir el hook en un bloqueador accidental de todo.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
PROTECTED_BRANCHES = {"main", "master"}


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #


def block(reason: str, hint: str = "") -> None:
    """Bloquea la acción y sale. Código 2 = deny para Claude Code."""
    print(f"[gitflow-es safety] {reason}", file=sys.stderr)
    if hint:
        print(hint, file=sys.stderr)
    else:
        print(
            "Si realmente necesitas ejecutar esto, pídele al usuario que "
            "lo haga directamente en su terminal.",
            file=sys.stderr,
        )
    sys.exit(2)


def allow() -> None:
    """Permite la acción."""
    sys.exit(0)


def current_branch(cwd: Optional[str] = None) -> Optional[str]:
    """Devuelve la rama actual, o None si no estamos en un repo git."""
    cmd = ["git", "branch", "--show-current"]
    if cwd:
        cmd = ["git", "-C", cwd, "branch", "--show-current"]
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
        branch = result.stdout.strip()
        return branch or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def gitflow_initialized(cwd: Optional[str] = None) -> bool:
    """
    Devuelve True si el repo tiene git-flow inicializado.
    Detectado por la presencia de `gitflow.branch.develop` en la config de git,
    que es la key que escribe `git flow init`.
    """
    cmd = ["git", "config", "--get", "gitflow.branch.develop"]
    if cwd:
        cmd = ["git", "-C", cwd, "config", "--get", "gitflow.branch.develop"]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def repo_has_commits(cwd: Optional[str] = None) -> bool:
    """
    Devuelve True si el repo tiene al menos un commit en HEAD.
    Devuelve False si HEAD está vacío (repo recién inicializado) o si algo
    falla. Se usa para permitir el commit inicial en `main` / `master`.
    """
    cmd = ["git", "rev-parse", "--verify", "--quiet", "HEAD"]
    if cwd:
        cmd = ["git", "-C", cwd, "rev-parse", "--verify", "--quiet", "HEAD"]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def branch_of_file(file_path: str) -> Optional[str]:
    """
    Devuelve la rama del repo git que contiene `file_path`, o None si el
    archivo no está en un repo git. Resuelve paths relativos a absolutos.
    """
    if not file_path:
        return None
    try:
        path = Path(file_path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None

    directory = str(path if path.is_dir() else path.parent)
    return current_branch(cwd=directory)


# --------------------------------------------------------------------------- #
# Bash checks
# --------------------------------------------------------------------------- #


def check_force_push(command: str, branch: Optional[str]) -> None:
    """Bloquea force-push sobre main/develop."""
    if not re.search(r"\bgit\s+push\b", command):
        return

    has_force = re.search(
        r"(?:^|\s)(?:-f\b|-[a-zA-Z]*f\b|--force(?:-with-lease)?\b)",
        command,
    )
    if not has_force:
        return

    target_match = re.search(
        r"\bgit\s+push\b.*?\s(origin|upstream)?\s*(main|master|develop)\b",
        command,
    )
    target = target_match.group(2) if target_match else branch

    if target in {"main", "master", "develop"}:
        block(
            f"Bloqueado: `git push --force` sobre `{target}` está prohibido por "
            f"la política del equipo. Rehace los commits o haz revert en una "
            f"rama de trabajo en vez de reescribir el historial compartido."
        )


def check_commit_on_main(command: str, branch: Optional[str]) -> None:
    """
    Bloquea git commit cuando la rama actual es main/master.

    Excepción: si el repo aún no tiene commits (primer commit), se permite
    para poder hacer el `chore: initial commit` antes de arrancar git-flow.
    """
    if not re.search(r"\bgit\s+commit\b", command):
        return
    if branch not in PROTECTED_BRANCHES:
        return
    # Excepción: repo sin commits todavía — permitir el primer commit
    if not repo_has_commits():
        return
    block(
        f"Bloqueado: commit directo en `{branch}`. El flujo del equipo "
        f"exige trabajar en una rama (`feature/*`, `fix/*`, `hotfix/*`, "
        f"etc.) y cerrarla con `/git finish`."
    )


def check_no_verify(command: str, _branch: Optional[str]) -> None:
    """Bloquea --no-verify en commits."""
    if re.search(r"\bgit\s+commit\b.*--no-verify\b", command):
        block(
            "Bloqueado: `git commit --no-verify` está prohibido. Los "
            "pre-commit hooks existen por una razón — si uno está roto, "
            "arréglalo en lugar de saltártelo."
        )


def check_explicit_author(command: str, _branch: Optional[str]) -> None:
    """Bloquea --author=... (el autor debe ser el usuario configurado)."""
    if re.search(r"\bgit\s+commit\b.*--author\b", command):
        block(
            "Bloqueado: `git commit --author=...` está prohibido. El autor "
            "del commit siempre debe ser el usuario configurado en git."
        )


def check_reset_hard(command: str, _branch: Optional[str]) -> None:
    """Bloquea git reset --hard."""
    if re.search(r"\bgit\s+reset\b.*--hard\b", command):
        block(
            "Bloqueado: `git reset --hard` destruye cambios sin aviso. Si "
            "realmente lo necesitas, córrelo manualmente en tu terminal."
        )


def check_clean_force(command: str, _branch: Optional[str]) -> None:
    """Bloquea git clean con -f."""
    if re.search(r"\bgit\s+clean\b(?:.*\s)?-[a-zA-Z]*f[a-zA-Z]*\b", command):
        block(
            "Bloqueado: `git clean -f` elimina archivos sin confirmación. "
            "Si realmente lo necesitas, córrelo manualmente o usa `git "
            "clean -n` primero para previsualizar."
        )


def check_sensitive_files(command: str, _branch: Optional[str]) -> None:
    """Bloquea git add / commit con archivos sensibles en el nombre."""
    if not re.search(r"\bgit\s+(?:add|commit)\b", command):
        return

    if re.search(r"(?<![\w.])\.env(?!\.(?:example|sample|template|dist))\b", command):
        block(
            "Bloqueado: parece que estás intentando añadir `.env` al commit. "
            "Los archivos `.env` no deben versionarse — agrega el archivo a "
            "`.gitignore`. Si es un `.env.example`, renómbralo."
        )

    suspicious = [
        r"\bid_rsa\b",
        r"\bid_ed25519\b",
        r"\bcredentials?\.(?:json|yaml|yml|env|txt)\b",
        r"\bsecrets?\.(?:json|yaml|yml|env|txt)\b",
        r"\bservice[-_]account\.json\b",
        r"\.pem\b",
        r"\.p12\b",
    ]
    for pattern in suspicious:
        if re.search(pattern, command, re.IGNORECASE):
            block(
                f"Bloqueado: el comando parece añadir un archivo sensible "
                f"(patrón `{pattern}`). Si es un falso positivo, renombra el "
                f"archivo o córrelo manualmente."
            )


def check_gitflow_not_initialized(command: str, _branch: Optional[str]) -> None:
    """
    Bloquea `git flow <feature|hotfix|release|support> <sub>` si el repo no
    tiene git-flow inicializado. Evita que Claude ejecute comandos que darían
    error confuso tipo "Not a gitflow-enabled repo yet".

    No bloquea `git flow init` (evidentemente, eso es lo que arregla esto).
    No bloquea `git flow version`, `git flow help`, etc.
    """
    m = re.search(r"\bgit\s+flow\s+(\w+)", command)
    if not m:
        return
    subcommand = m.group(1)

    # Subcomandos que requieren repo inicializado
    requires_init = {"feature", "hotfix", "release", "support", "bugfix"}
    if subcommand not in requires_init:
        return

    if gitflow_initialized():
        return

    block(
        "Bloqueado: este repo no tiene git-flow inicializado todavía, y el "
        "subcomando `git flow " + subcommand + "` lo requiere.",
        hint=(
            "Para arreglarlo, propónle al usuario inicializar git-flow con "
            "los defaults del equipo:\n\n"
            "  git flow init -d\n\n"
            "Eso configura `main` como rama de producción, `develop` como "
            "rama de integración, y los prefijos estándar (feature/, "
            "hotfix/, release/, support/).\n\n"
            "Si el repo aún no tiene rama `develop`, git-flow la creará "
            "automáticamente. Una vez inicializado, reintenta el comando "
            "original."
        ),
    )


BASH_CHECKS = [
    check_force_push,
    check_commit_on_main,
    check_no_verify,
    check_explicit_author,
    check_reset_hard,
    check_clean_force,
    check_sensitive_files,
    check_gitflow_not_initialized,
]


# --------------------------------------------------------------------------- #
# Edit checks (Write / Edit / MultiEdit / NotebookEdit)
# --------------------------------------------------------------------------- #


def extract_edit_target(tool_name: str, tool_input: dict) -> Optional[str]:
    """Extrae la ruta del archivo que se va a modificar, según la tool."""
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path") or tool_input.get("file_path")
    # Write, Edit, MultiEdit
    return tool_input.get("file_path")


def check_edit_on_main(file_path: str) -> None:
    """
    Bloquea ediciones cuando la rama del repo que contiene el archivo es
    main/master. Permite develop (el skill `git` maneja la excepción de
    commit directo en develop cuando el usuario lo pide explícitamente).
    Permite archivos fuera de cualquier repo git.

    Excepción: si el repo aún no tiene commits (primer setup), se permite
    editar para poder preparar el `chore: initial commit`.
    """
    branch = branch_of_file(file_path)
    if branch is None:
        return  # No es un repo git

    if branch not in PROTECTED_BRANCHES:
        return

    # Excepción: repo sin commits — permitir ediciones del primer setup
    # Resolvemos cwd relativo al archivo para consultar ese repo específico.
    try:
        resolved = Path(file_path).expanduser().resolve()
        cwd = str(resolved if resolved.is_dir() else resolved.parent)
    except (OSError, RuntimeError):
        cwd = None
    if not repo_has_commits(cwd=cwd):
        return

    block(
        f"Bloqueado: estás parado en `{branch}` y no se permite modificar "
        f"archivos directamente sobre la rama de producción. Antes de "
        f"editar `{file_path}`, crea una rama de trabajo.",
        hint=(
            "Flujo sugerido:\n"
            "  1. Preguntarle al usuario qué tipo de cambio es "
            "(feature, fix, hotfix, chore, refactor).\n"
            "  2. Proponer un nombre de rama en kebab-case y confirmarlo.\n"
            "  3. Crear la rama con `git flow <tipo> start <nombre>` "
            "(para feature/hotfix/release) o `git checkout -b "
            "<tipo>/<nombre>` desde develop (para fix/refactor/chore).\n"
            "  4. Una vez en la rama, reintentar la edición."
        ),
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        allow()

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}

    # Ruta 1: Bash
    if tool_name == "Bash":
        command = tool_input.get("command", "") or ""
        if not command.strip():
            allow()
        branch = current_branch()
        for check in BASH_CHECKS:
            try:
                check(command, branch)
            except SystemExit:
                raise
            except Exception:
                continue
        allow()

    # Ruta 2: modificación de archivos
    if tool_name in EDIT_TOOLS:
        file_path = extract_edit_target(tool_name, tool_input)
        if not file_path:
            allow()
        try:
            check_edit_on_main(file_path)
        except SystemExit:
            raise
        except Exception:
            pass
        allow()

    # Cualquier otra tool — no aplica
    allow()


if __name__ == "__main__":
    main()
