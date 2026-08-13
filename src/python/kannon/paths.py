from __future__ import annotations

import os
import shlex
from pathlib import Path


PROJECT_ROOT = Path(os.environ.get("KANNON_PROJECT_ROOT") or Path(__file__).resolve().parents[3])


def project_root() -> Path:
    return PROJECT_ROOT


def install_command() -> str:
    installer = project_root() / "install.sh"
    if installer.exists():
        return f"sh {shlex.quote(str(installer))}"
    return "sh ./install.sh"


def install_command_parts() -> list[str]:
    installer = project_root() / "install.sh"
    if installer.exists():
        return ["sh", str(installer)]
    return ["sh", "./install.sh"]


def venv_python_command(args: str = "") -> str:
    python = shlex.quote(str(project_root() / ".venv" / "bin" / "python"))
    base = f"{python} -m pip"
    return f"{base} {args}".strip()


def startup_install_commands(package_name: str) -> list[str]:
    venv = shlex.quote(str(project_root() / ".venv"))
    root = shlex.quote(str(project_root()))
    root_pdf = shlex.quote(f"{project_root()}[pdf]")
    return [
        install_command(),
        f"python3 -m venv {venv} && {venv_python_command('install -e ' + root_pdf)}",
        venv_python_command(f"install {package_name}"),
    ]
