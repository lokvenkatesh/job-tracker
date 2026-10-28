"""Prompt for a dashboard password and print its bcrypt hash for DASHBOARD_PASSWORD_HASH."""

import getpass

import bcrypt


def main() -> None:
    password = getpass.getpass("Dashboard password: ")
    if password != getpass.getpass("Repeat: "):
        raise SystemExit("Passwords do not match.")
    print(bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode())


if __name__ == "__main__":
    main()
