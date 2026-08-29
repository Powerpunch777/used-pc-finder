"""Local, permission-restricted storage for SMTP credentials."""

from __future__ import annotations

import getpass
import os
from collections.abc import Callable, MutableMapping
from pathlib import Path

SMTP_PASSWORD_ENVIRONMENT_VARIABLE = "KARROT_SMTP_PASSWORD"
SECRETS_DIRECTORY = Path.home() / ".config" / "used_pc_finder"
SECRETS_PATH = SECRETS_DIRECTORY / "secrets.env"


def _ensure_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)


def load_smtp_password(
    path: Path = SECRETS_PATH,
    environ: MutableMapping[str, str] | None = None,
) -> bool:
    """Load the saved password into the process environment without logging it."""
    environment = os.environ if environ is None else environ
    _ensure_directory(path.parent)
    if not path.is_file():
        return bool(environment.get(SMTP_PASSWORD_ENVIRONMENT_VARIABLE))
    os.chmod(path, 0o600)
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if key == SMTP_PASSWORD_ENVIRONMENT_VARIABLE and separator and value:
            environment.setdefault(key, value)
            return True
    return bool(environment.get(SMTP_PASSWORD_ENVIRONMENT_VARIABLE))


def setup_smtp_password(
    path: Path = SECRETS_PATH,
    prompt: Callable[[str], str] = getpass.getpass,
) -> None:
    """Prompt privately once and create a mode-600 local SMTP secret file."""
    if path.exists():
        raise FileExistsError(f"SMTP secret file already exists: {path}")
    password = prompt("Gmail app password: ")
    if not password or "\n" in password or "\r" in password:
        raise ValueError("Gmail app password must be a non-empty single line")
    _ensure_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"{SMTP_PASSWORD_ENVIRONMENT_VARIABLE}={password}\n")
    os.chmod(path, 0o600)
