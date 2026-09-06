"""Local-only validation and safe installation of Google credentials."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path


class CredentialValidationError(ValueError):
    """The credential cannot be used because its local file is invalid."""


def validate_service_account_file(path: str | Path) -> Path:
    """Validate the non-secret structure of a service-account JSON file.

    No parsed credential content is included in errors, which ensures the
    private key cannot accidentally reach an operator dialog or log.
    """
    candidate = Path(path).expanduser()
    if not candidate.exists():
        raise CredentialValidationError(f"Credentials file missing: {candidate}")
    if not candidate.is_file():
        raise CredentialValidationError(f"Credentials path is not a file: {candidate}")
    try:
        with candidate.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CredentialValidationError(
            f"Credentials file is unreadable or is not valid JSON: {candidate}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("type") != "service_account":
        raise CredentialValidationError(
            "The selected file is not a Google service-account credential."
        )
    missing = [
        name for name in ("client_email", "private_key", "token_uri")
        if not isinstance(payload.get(name), str) or not payload[name].strip()
    ]
    if missing:
        raise CredentialValidationError(
            "The Google service-account credential is missing required fields: "
            + ", ".join(missing)
        )
    return candidate


def install_service_account_file(source: str | Path, destination: str | Path) -> Path:
    """Validate and atomically copy *source* to the managed destination."""
    source_path = validate_service_account_file(source)
    destination_path = Path(destination).expanduser()
    try:
        if source_path.resolve() == destination_path.resolve():
            return destination_path
    except OSError:
        pass

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{destination_path.name}.", suffix=".tmp",
            dir=destination_path.parent, delete=False,
        ) as temporary:
            temporary_name = temporary.name
            with source_path.open("rb") as input_file:
                shutil.copyfileobj(input_file, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, destination_path)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass
    return destination_path
