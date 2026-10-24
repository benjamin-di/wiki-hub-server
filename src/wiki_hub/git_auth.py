"""Answer Git credential prompts only for the operator's configured Git origins."""

import json
import os
import re
import sys
from urllib.parse import urlsplit


def origin(url):
    if not isinstance(url, str):
        raise ValueError("Unsupported Git credential origin")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Unsupported Git credential origin")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def reply(prompt, username, token, allowed_urls):
    match = re.fullmatch(r"(Username|Password) for '([^'\r\n]+)':\s*", prompt)
    if not match:
        return None
    try:
        target = origin(match[2])
        allowed = {origin(url) for url in allowed_urls if url}
    except (ValueError, TypeError):
        return None
    if target not in allowed:
        return None
    return username if match[1] == "Username" else token


def main():
    try:
        value = reply(sys.argv[1], os.environ["WIKI_GIT_USERNAME"], os.environ["WIKI_GIT_TOKEN"],
                      json.loads(os.environ["WIKI_GIT_ALLOWED_URLS"]))
    except (KeyError, IndexError, ValueError, TypeError):
        return 1
    if value is None:
        return 1
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
