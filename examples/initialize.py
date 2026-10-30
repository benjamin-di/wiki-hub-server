#!/usr/bin/env python3
"""Create an owner-only server .env, preserving existing settings and secrets."""

import argparse
import os
import secrets
import tempfile
from pathlib import Path

SECRET_FIELDS = ("FORGEJO_PASSWORD", "POCKET_ID_ENCRYPTION_KEY", "POCKET_ID_STATIC_API_KEY",
                 "WIKI_WEB_CLIENT_SECRET", "WIKI_WEB_SESSION_KEY")


def initialize(template, destination):
    if destination.is_symlink():
        raise ValueError("The configuration must not be a symlink")
    source = destination.read_text() if destination.exists() else template.read_text()
    lines, found, present = [], set(), set()
    for line in source.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            present.add(key)
        if key in SECRET_FIELDS and separator:
            if key in found:
                raise ValueError("Duplicate credential field in configuration")
            found.add(key)
            if value.strip() in ("", "''", '""'):
                line = key + "=" + secrets.token_urlsafe(32)
        elif separator and key in ("SETUP_UID", "SETUP_GID") and not value.strip():
            line = key + "=" + str(os.getuid() if key == "SETUP_UID" else os.getgid())
        lines.append(line)
    for key in SECRET_FIELDS:
        if key not in found:
            lines.append(key + "=" + secrets.token_urlsafe(32))
    for key, value in (("SETUP_UID", os.getuid()), ("SETUP_GID", os.getgid())):
        if key not in present:
            lines.append(key + "=" + str(value))
    fd, temporary = tempfile.mkstemp(prefix=".wiki-config-", dir=destination.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write("\n".join(lines) + "\n")
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    output = destination.parent / ".setup"
    if output.is_symlink():
        raise ValueError("The setup output directory must not be a symlink")
    output.mkdir(mode=0o700, exist_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".env"))
    args = parser.parse_args()
    initialize(Path(__file__).resolve().parents[1] / ".env.example", args.output)
    print("OK: private server configuration prepared; existing values preserved. Edit the hostnames, owner and model.")


if __name__ == "__main__":
    main()
