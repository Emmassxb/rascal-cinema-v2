#!/usr/bin/env python3
"""Crea un usuario con rol (p. ej. el primer admin).

    python scripts/create_user.py --username admin --role admin --email admin@ejemplo.com
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pydantic import ValidationError  # noqa: E402

from rascal.auth.mailer import build_mailer  # noqa: E402
from rascal.auth.service import AuthService, RegistrationError  # noqa: E402
from rascal.config import get_settings  # noqa: E402
from rascal.data.db import session_scope  # noqa: E402
from rascal.data.enums import Role  # noqa: E402
from rascal.domain.schemas import RegisterForm  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--username", required=True)
    parser.add_argument("--role", choices=[r.value for r in Role], default=Role.USER.value)
    parser.add_argument("--email")
    parser.add_argument("--password", help="si se omite se pide de forma interactiva")
    args = parser.parse_args()

    password = args.password
    if not password:
        password = getpass.getpass("Contraseña: ")
        if password != getpass.getpass("Repite la contraseña: "):
            print("Las contraseñas no coinciden", file=sys.stderr)
            return 1
    try:
        form = RegisterForm(username=args.username, email=args.email, password=password)  # type: ignore[arg-type]
    except ValidationError as exc:
        print(exc, file=sys.stderr)
        return 1
    settings = get_settings()
    try:
        with session_scope() as session:
            principal = AuthService(session, settings, build_mailer(settings)).register(form, role=Role(args.role))
    except RegistrationError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Usuario '{principal.username}' creado con rol {principal.role.value} (id {principal.user_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
