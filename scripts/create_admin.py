"""Create a phone + password account with the admin role.

The password is asked interactively (never pass it as an argument: it would end up in the
shell history and process list).

Run with:  python -m scripts.create_admin --name "Store Admin" --phone 0933 123 456
"""

import argparse
import asyncio
import getpass
import sys

from app.core import clock, security
from app.db.session import SessionLocal, engine
from app.modules.auth.service import flush_user_changes
from app.modules.users import repository as users_repo
from app.modules.users.enums import AuthProvider, UserRole
from app.modules.users.models import User
from app.shared.exceptions import AppError
from app.shared.utils.phone import normalize_phone


def ask_password(phone: str) -> str:
    while True:
        password = getpass.getpass("Password: ")
        try:
            security.validate_password(password, phone)
        except AppError as exc:
            print(f"  {exc.message}")
            continue
        if getpass.getpass("Repeat password: ") != password:
            print("  Passwords do not match.")
            continue
        return password


async def main(name: str, raw_phone: str) -> int:
    engine.echo = False
    try:
        phone = normalize_phone(raw_phone)
    except AppError:
        print(f"Invalid phone number: {raw_phone!r}")
        return 2
    name = name.strip()
    if not name or len(name) > 100:
        print("Name must be 1 to 100 characters.")
        return 2

    try:
        async with SessionLocal() as session:
            if await users_repo.phone_taken(session, phone):
                print(f"Phone {phone} is already used. To promote that user: python -m scripts.make_admin --phone {phone}")
                return 1
            password = ask_password(phone)
            user = User(
                name=name,
                phone=phone,
                auth_provider=AuthProvider.phone,
                password_hash=await security.hash_password_async(password),
                role=UserRole.admin,
                created_at=clock.utcnow(),
            )
            session.add(user)
            try:
                await flush_user_changes(session)
            except AppError as exc:
                print(exc.message)
                return 1
            await session.commit()
            print(f"Admin created: id={user.id}, name={user.name}, phone={phone}")
            return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create an admin account (phone + password).")
    parser.add_argument("--name", required=True)
    parser.add_argument("--phone", required=True, help="Phone number, local or international format")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.name, args.phone)))
