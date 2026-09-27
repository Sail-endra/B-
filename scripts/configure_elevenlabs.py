"""Set ELEVENLABS_API_KEY in the project .env without echoing the secret."""

from __future__ import annotations

import os
import re
import tempfile
from getpass import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
KEY_LINE = re.compile(r"^\s*(?:export\s+)?ELEVENLABS_API_KEY\s*=")


def set_key(value: str) -> None:
    if not value or "\n" in value or "\r" in value:
        raise ValueError("A non-empty, single-line key is required.")

    old = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    kept = [line for line in old if not KEY_LINE.match(line)]
    kept.append(f"ELEVENLABS_API_KEY={value}")
    content = "\n".join(kept) + "\n"

    fd, temporary = tempfile.mkstemp(prefix=".env.", dir=ROOT, text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, ENV_FILE)
        os.chmod(ENV_FILE, 0o600)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> None:
    try:
        key = getpass("ElevenLabs API key (input hidden): ").strip()
        set_key(key)
    except (OSError, ValueError) as error:
        print(f"Could not update .env: {error}")
        raise SystemExit(1) from error
    print("ElevenLabs API key saved to .env (value hidden).")


if __name__ == "__main__":
    main()
