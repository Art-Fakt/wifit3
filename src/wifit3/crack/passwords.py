"""WPA passphrases: length validation, SSID variants, wordlist streaming."""
from collections.abc import Iterator
from pathlib import Path

_SSID_SUFFIXES = ("123", "1234", "12345678", "!")


def is_valid_psk(psk: str) -> bool:
    """8..63 bytes once UTF-8 encoded, matching how ``wpa_psk.pmk`` encodes the passphrase."""
    return 8 <= len(psk.encode()) <= 63


def ssid_variants(ssid: str) -> list[str]:
    bases = [ssid, ssid.lower(), ssid.upper()]
    variants = bases + [base + suffix for base in bases for suffix in _SSID_SUFFIXES]
    return [psk for psk in dict.fromkeys(variants) if is_valid_psk(psk)]


def read_passwords(path: Path) -> Iterator[str]:
    with path.open(encoding="utf-8", errors="ignore") as wordlist:
        for line in wordlist:
            psk = line.rstrip("\r\n")
            if is_valid_psk(psk):
                yield psk
