"""Phone-number normalization and authenticated encryption for messaging/calls."""
import os
import re


# Normalize and validate a Pakistani phone number.
def normalize_phone_pk(phone):
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        raise ValueError("A phone number is required")
    if digits.startswith("0092"):
        digits = digits[2:]
    if digits.startswith("0"):
        digits = "92" + digits[1:]
    elif digits.startswith("3") and len(digits) == 10:
        digits = "92" + digits
    if not digits.startswith("92"):
        raise ValueError("Phone number must be a Pakistani number")
    return "+" + digits


# Load the configured Fernet cipher for phone encryption.
def _fernet():
    key = os.getenv("PHONE_ENC_KEY")
    if not key:
        raise RuntimeError("PHONE_ENC_KEY is required to encrypt customer phone numbers")
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError("Install project requirements to use phone encryption") from exc
    return Fernet(key.encode("ascii"))


# Encrypt a normalized phone number for later dialing.
def encrypt_phone(phone):
    return _fernet().encrypt(normalize_phone_pk(phone).encode("utf-8"))


# Decrypt a stored phone number for a call provider.
def decrypt_phone(blob):
    return _fernet().decrypt(bytes(blob)).decode("utf-8")
