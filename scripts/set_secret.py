"""Write one secret into .env without echoing it or putting it in shell history.

python -m scripts.set_secret ANTHROPIC_API_KEY
python -m scripts.set_secret TELEGRAM_BOT_TOKEN
"""

import argparse
import getpass
import os
import re
import shutil

from app.config import PROJECT_ROOT

ENV_PATH = PROJECT_ROOT / ".env"
EXAMPLE_PATH = PROJECT_ROOT / ".env.example"


def set_value(text: str, name: str, value: str) -> str:
    line = f"{name}={value}"
    pattern = re.compile(rf"^{re.escape(name)}=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda _: line, text, count=1)
    return text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="variable name, e.g. ANTHROPIC_API_KEY")
    args = parser.parse_args()

    value = getpass.getpass(f"{args.name} (input hidden): ").strip()
    if not value:
        raise SystemExit("Nothing entered; .env unchanged.")
    if any(c.isspace() for c in value):
        raise SystemExit("Value contains whitespace; check the paste. .env unchanged.")

    if not ENV_PATH.exists():
        shutil.copy(EXAMPLE_PATH, ENV_PATH)
    ENV_PATH.write_text(set_value(ENV_PATH.read_text(), args.name, value))
    os.chmod(ENV_PATH, 0o600)
    print(f"Saved {args.name} to .env ({len(value)} characters).")


if __name__ == "__main__":
    main()
