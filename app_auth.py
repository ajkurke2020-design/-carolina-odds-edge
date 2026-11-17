import hmac


def passphrase_matches(entered: str, expected: str) -> bool:
    """Constant-time comparison that accepts non-ASCII passphrases.

    hmac.compare_digest raises TypeError for str arguments containing
    non-ASCII characters, so both sides are compared as UTF-8 bytes.
    """
    return hmac.compare_digest(entered.encode("utf-8"), expected.encode("utf-8"))
