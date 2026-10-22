"""Give an existing user the admin role.

Run with:  python -m scripts.make_admin --phone 0151 23456789
"""

import argparse
import asyncio
import sys

from app.db.session import SessionLocal, engine
from app.modules.users import repository as users_repo
from app.modules.users.enums import UserRole
from app.shared.exceptions import ValidationFailed
from app.shared.utils.phone import normalize_phone


async def main(raw_phone: str) -> int:
    engine.echo = False
    try:
        phone = normalize_phone(raw_phone)
    except ValidationFailed:
        print(f"Invalid phone number: {raw_phone!r}")
        return 2

    try:
        async with SessionLocal() as session:
            user = await users_repo.get_user_by_phone(session, phone)
            if user is None:
                print(f"No user with phone {phone}. Register first, or use scripts.create_admin.")
                return 1
            if user.role == UserRole.admin:
                print(f"User {user.id} ({user.name}, {phone}) is already an admin.")
                return 0
            user.role = UserRole.admin
            await session.commit()
            print(f"User {user.id} ({user.name}, {phone}) is now an admin.")
            return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Give an existing user the admin role.")
    parser.add_argument("--phone", required=True, help="Phone number, local or international format")
    sys.exit(asyncio.run(main(parser.parse_args().phone)))
