from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from dataclasses import dataclass

from .paths import install_command, install_command_parts, project_root


@dataclass(frozen=True)
class ImportStatus:
    import_name: str
    package_name: str
    purpose: str
    missing_error: ModuleNotFoundError | None = None

    @property
    def ok(self) -> bool:
        return self.missing_error is None


@dataclass(frozen=True)
class DoctorFix:
    label: str
    command: list[str]
    system: bool = False

    def command_text(self) -> str:
        import shlex

        return " ".join(shlex.quote(part) for part in self.command)


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str
    purpose: str
    required: bool = False
    fix: DoctorFix | None = None


def import_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def run_doctor(
    yaml_status: ImportStatus,
    pillow_status: ImportStatus,
    *,
    prompt: bool = True,
    fix_system: bool = False,
) -> int:
    checks = doctor_checks(yaml_status, pillow_status, fix_system=fix_system)
    print("Kannon doctor")
    print(f"Project root: {project_root()}")
    print(f"Python: {sys.executable}")
    print()
    for check in checks:
        status = "ok" if check.ok else "missing"
        print(f"{status:7} {check.name:10} {check.purpose} ({check.detail})")
    print()

    missing_required = any(check.required and not check.ok for check in checks)
    fixable = [check for check in checks if not check.ok and check.fix is not None]
    if fixable:
        ran_repairs = run_doctor_repairs(fixable, prompt=prompt)
        if ran_repairs:
            print("Repair commands finished. Rerun kannon --doctor so the wrapper can use any new .venv.")
            print()
    else:
        print("No automatic repair commands are available.")

    if any(check.name == "PyMuPDF" and not check.ok for check in checks):
        print("PyMuPDF is missing. PDFs can still render when pdftoppm is installed, but metadata may vary.")
        print(f"Preferred fix: {install_command()}")
    if any(check.name == "chafa" and not check.ok for check in checks):
        print("Chafa is missing. Kannon will use built-in terminal renderers.")
        print("Install on Debian/Kali/Ubuntu: sudo apt install chafa")
    if any(check.name == "xdg-open" and not check.ok for check in checks):
        print("xdg-open is missing, so the x shortcut cannot open files externally.")
        print("Install on Debian/Kali/Ubuntu: sudo apt install xdg-utils")
    print("Doctor finished.")
    return 2 if missing_required else 0


def doctor_checks(
    yaml_status: ImportStatus,
    pillow_status: ImportStatus,
    *,
    fix_system: bool = False,
) -> list[DoctorCheck]:
    python_dependencies_ok = yaml_status.ok and pillow_status.ok and import_available("fitz")
    python_fix = (
        None
        if python_dependencies_ok
        else DoctorFix("Install Kannon Python dependencies into .venv", install_command_parts())
    )
    return [
        DoctorCheck("Python", True, sys.executable, "required runtime", required=True),
        DoctorCheck(
            "PyYAML",
            yaml_status.ok,
            f"import {yaml_status.import_name}",
            yaml_status.purpose,
            required=True,
            fix=python_fix,
        ),
        DoctorCheck(
            "Pillow",
            pillow_status.ok,
            f"import {pillow_status.import_name}",
            pillow_status.purpose,
            required=True,
            fix=python_fix,
        ),
        DoctorCheck(
            "PyMuPDF",
            import_available("fitz"),
            "import fitz",
            "recommended primary PDF renderer",
            fix=python_fix,
        ),
        DoctorCheck(
            "pdftoppm",
            shutil.which("pdftoppm") is not None,
            shutil.which("pdftoppm") or "not found",
            "optional Poppler PDF fallback",
            fix=system_package_fix("poppler-utils", "poppler", fix_system=fix_system),
        ),
        DoctorCheck(
            "pdfinfo",
            shutil.which("pdfinfo") is not None,
            shutil.which("pdfinfo") or "not found",
            "optional Poppler PDF metadata",
            fix=system_package_fix("poppler-utils", "poppler", fix_system=fix_system),
        ),
        DoctorCheck(
            "chafa",
            shutil.which("chafa") is not None,
            shutil.which("chafa") or "not found",
            "recommended terminal graphics renderer",
            fix=system_package_fix("chafa", "chafa", fix_system=fix_system),
        ),
        DoctorCheck(
            "xdg-open",
            shutil.which("xdg-open") is not None,
            shutil.which("xdg-open") or "not found",
            "optional x shortcut opener",
            fix=system_package_fix("xdg-utils", None, fix_system=fix_system),
        ),
    ]


def system_package_fix(debian_package: str, brew_package: str | None, *, fix_system: bool) -> DoctorFix | None:
    if not fix_system:
        return None
    if shutil.which("apt"):
        return DoctorFix(f"Install {debian_package} with apt", ["sudo", "apt", "install", debian_package], system=True)
    if shutil.which("dnf"):
        return DoctorFix(f"Install {debian_package} with dnf", ["sudo", "dnf", "install", debian_package], system=True)
    if brew_package and shutil.which("brew"):
        return DoctorFix(f"Install {brew_package} with Homebrew", ["brew", "install", brew_package], system=True)
    return None


def run_doctor_repairs(checks: list[DoctorCheck], *, prompt: bool) -> bool:
    repairs: list[DoctorFix] = []
    seen: set[tuple[str, ...]] = set()
    for check in checks:
        if check.fix is None:
            continue
        key = tuple(check.fix.command)
        if key not in seen:
            repairs.append(check.fix)
            seen.add(key)
    if not repairs:
        return False
    print("Available repair command(s):")
    for fix in repairs:
        print(f"  - {fix.label}: {fix.command_text()}")
    print()
    if not prompt:
        print("Repair prompting is disabled for this run.")
        return False
    if not sys.stdin.isatty():
        print("Not prompting because stdin is not interactive. Run kannon --doctor in a terminal to apply repairs.")
        return False
    ran_any = False
    for fix in repairs:
        if confirm(f"Run {fix.label}? [{fix.command_text()}]"):
            run_repair_command(fix)
            ran_any = True
        else:
            print(f"Skipped: {fix.label}")
    print()
    return ran_any


def confirm(prompt: str) -> bool:
    while True:
        try:
            answer = input(f"{prompt} [y/N] ").strip().lower()
        except EOFError:
            return False
        if answer in {"y", "yes"}:
            return True
        if answer in {"", "n", "no"}:
            return False
        print("Please answer y or n.")


def run_repair_command(fix: DoctorFix) -> None:
    print(f"Running: {fix.command_text()}")
    try:
        subprocess.run(fix.command, check=True)
    except FileNotFoundError as exc:
        print(f"error: command not found: {exc.filename}", file=sys.stderr)
    except subprocess.CalledProcessError as exc:
        print(f"error: repair command failed with exit status {exc.returncode}: {fix.command_text()}", file=sys.stderr)
    else:
        print(f"Completed: {fix.label}")
