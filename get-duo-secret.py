#!/usr/bin/env python3
"""Extract the Duo base32 OTP secret from an activation QR/link URL.

Works with both QR formats:
  * Android:  https://api-XXXX.duosecurity.com/frame/qr?value=<CODE>-<base64host>
  * Email/SMS: https://api-XXXX.duosecurity.com/frame/qr?value=https%3A%2F%2Fm-XXXX...
  * Raw:       https://m-XXXX.duosecurity.com/activate/<CODE>

Usage:
  uv run --with requests --with pycryptodome python get-duo-secret.py '<qr-url>'

Note: the activation link is single-use. Run it once; it registers a virtual
Duo Mobile device and prints your base32 secret + otpauth:// URIs.
"""

import base64
import sys
from urllib.parse import parse_qs, unquote, urlparse

import requests
from Crypto.PublicKey import RSA


def _resolve(uri: str) -> str:
    if "frame/qr" in uri:
        q = parse_qs(urlparse(uri).query)
        uri = unquote(q.get("value", [""])[0])
    return uri.strip()


def get_secret(uri: str) -> str:
    uri = _resolve(uri)
    parsed = urlparse(uri)
    host_id = parsed.netloc.split(".")[0].split("-")[-1]
    slug = parsed.path.rstrip("/").split("/")[-1]

    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Host": f"api-{host_id}.duosecurity.com",
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
        "User-Agent": "okhttp/4.0.0",
    }
    data = {
        "touchid_status": "not_supported",
        "jailbroken": False,
        "architecture": "arch64",
        "region": "US",
        "app_id": "com.duosecurity.duomobile",
        "full_disk_encryption": True,
        "passcode_status": True,
        "platform": "Android",
        "pkpush": "rsa-sha512",
        "pubkey": RSA.generate(2048).publickey().export_key().decode(),
        "app_version": "4.115.0",
        "app_build_number": 411500,
        "version": "9.0.0",
        "manufacturer": "Samsung",
        "language": "en",
        "model": "Samsung Smart Fridge",
        "security_patch_level": "2019-07-05",
    }
    address = f"https://{parsed.netloc}/push/v2/activation/{slug}"
    r = requests.post(
        address,
        headers=headers,
        params={"customer_protocol": "1"},
        data=data,
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    hotp_secret = body["response"]["hotp_secret"]
    return base64.b32encode(hotp_secret.encode()).decode()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    secret = get_secret(sys.argv[1])
    print(f"\nBase32 secret: {secret}")
    print(f"\nTOTP URI:\notpauth://totp/Duo:sghose7?secret={secret}&issuer=Duo")
    print(f"\nHOTP URI:\notpauth://hotp/Duo:sghose7?secret={secret}&issuer=Duo&counter=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
