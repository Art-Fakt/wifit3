"""WPA/WPA2-PSK key derivation + the 4-way EAPOL MIC (no I/O).

PMK = PBKDF2-HMAC-SHA1(psk, ssid, 4096, 32). PTK = PRF-512 over the sorted MAC pair and
sorted nonce pair. The MIC (key descriptor version 2, plain PSK) is HMAC-SHA1-128 over the
EAPOL payload with its MIC field zeroed, keyed by the KCK (PTK[:16]). Version 3 (PSK-SHA256,
AES-CMAC) is not implemented: the FakeAP twin advertises plain PSK.
"""
import hashlib
import hmac

from wifit3.crack.hc22000_format import Hc22000Line

_KCK_LEN = 16
_KEY_INFO_OFFSET = 5
_SNONCE_OFFSET = 17
_NONCE_LEN = 32
_KEY_DESCRIPTOR_VERSION_HMAC_SHA1 = 2


def pmk(psk: str, ssid: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", psk.encode(), ssid, 4096, 32)


def _prf(key: bytes, label: bytes, data: bytes, nbytes: int) -> bytes:
    out = b""
    i = 0
    while len(out) < nbytes:
        out += hmac.new(key, label + b"\x00" + data + bytes([i]), hashlib.sha1).digest()
        i += 1
    return out[:nbytes]


def ptk(pmk_bytes: bytes, aa: bytes, spa: bytes, anonce: bytes, snonce: bytes,
        nbytes: int = 48) -> bytes:
    """AA = AP MAC, SPA = client MAC. Both the MAC pair and the nonce pair enter sorted, so
    the two peers derive the same PTK regardless of who is addressed first."""
    data = min(aa, spa) + max(aa, spa) + min(anonce, snonce) + max(anonce, snonce)
    return _prf(pmk_bytes, b"Pairwise key expansion", data, nbytes)


def kck(ptk_bytes: bytes) -> bytes:
    return ptk_bytes[:_KCK_LEN]


def eapol_mic(kck_bytes: bytes, eapol_payload_mic_zeroed: bytes) -> bytes:
    return hmac.new(kck_bytes, eapol_payload_mic_zeroed, hashlib.sha1).digest()[:16]


def mic_for(psk: str, ssid: bytes, aa: bytes, spa: bytes, anonce: bytes, snonce: bytes,
            eapol_payload_mic_zeroed: bytes) -> bytes:
    """The M2 MIC a client with this PSK would produce over these EAPOL bytes."""
    k = kck(ptk(pmk(psk, ssid), aa, spa, anonce, snonce))
    return eapol_mic(k, eapol_payload_mic_zeroed)


def pmkid(pmk_bytes: bytes, aa: bytes, spa: bytes) -> bytes:
    return hmac.new(pmk_bytes, b"PMK Name" + aa + spa, hashlib.sha1).digest()[:16]


def psk_matches(pmk_bytes: bytes, line: Hc22000Line) -> bool:
    """True if this PMK cracks the hashline. EAPOL lines: key descriptor version 2 only, no nonce
    error correction."""
    mac_ap, mac_sta = bytes.fromhex(line.mac_ap), bytes.fromhex(line.mac_sta)
    expected = bytes.fromhex(line.pmkid_or_mic)
    if line.kind == "01":
        return hmac.compare_digest(pmkid(pmk_bytes, mac_ap, mac_sta), expected)
    if line.kind != "02":
        return False
    eapol = bytes.fromhex(line.eapol)
    key_info = int.from_bytes(eapol[_KEY_INFO_OFFSET:_KEY_INFO_OFFSET + 2], "big")
    if key_info & 7 != _KEY_DESCRIPTOR_VERSION_HMAC_SHA1:
        return False
    snonce = eapol[_SNONCE_OFFSET:_SNONCE_OFFSET + _NONCE_LEN]
    kck_bytes = ptk(pmk_bytes, mac_ap, mac_sta, bytes.fromhex(line.anonce), snonce, nbytes=_KCK_LEN)
    return hmac.compare_digest(eapol_mic(kck_bytes, eapol), expected)
