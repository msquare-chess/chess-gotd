#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GOTD v6 security core (16 Nov 2026, KT section 331, Claude conversation "PlayWithEngine_2026-09-28 deep analysis /
Arasan integration"; owner: "make sure it is 100 percent secure and not leaking anything").

Used by the PC sender, SETUP and the GitHub Action (the Admin app has the same rules in Kotlin, data/Secure.kt).

Who may do what
  * Every device (phone / PC) has its OWN signing key (ECDSA P-256). Phones: Android Keystore, fingerprint per use.
    PCs: the key is protected by Windows (DPAPI, this Windows user only) / the macOS Keychain.
  * Every request is SIGNED by the device key and then SEALED with the inbox public key (RSA-OAEP-256 + AES-256-GCM,
    AAD = request id), so the public repo never shows what is asked and nobody can change it on the way.
  * The GitHub Action accepts a request only when the signature belongs to a device in devices.json (gotd-outbox
    branch, written only by the Action itself or by the owner's own GitHub sign-in) - the GitHub access token alone
    (the "doorbell") can start the Action but cannot make it DO anything.
  * Adding a device never puts a secret into a QR the network sees: either the new device shows its PUBLIC keys and
    a device you already have approves it (the Action then hands the new device its access sealed to the new device's
    one-time transfer key), or a device you have shows a one-time ticket QR on its own screen (10 minutes, used once).

Formats (version 2)
  request  = {"v":2, "id", "op", "created" (UTC ISO), ...op fields}
  envelope = {"v":2, "dev": device id, "sig": b64 DER ECDSA-SHA256 over SIGN_PREFIX + id + "\\n" + req JSON,
              "req": req JSON (string), ["pub": b64 SubjectPublicKeyInfo - only for op pair_claim]}
  box      = base64(JSON {"v":1, "alg":"RSA-OAEP-256+A256GCM", "ek", "iv", "ct"})  - same box as v5, envelope inside
  bundle   = sealed for a device's one-time transfer key: ECDH P-256 (ephemeral) -> HKDF-SHA256 (salt = pairing id,
             info "GOTD2-pair") -> AES-256-GCM (AAD = pairing id): {"v":2, "epk", "iv", "ct"}
  team box = AES-256-GCM with the team key (shared by your devices + the Action), AAD = what it is ("gotd-plan")
"""
import base64
import ctypes
import hashlib
import json
import os
import secrets
import subprocess
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SIGN_PREFIX = b"GOTD2\n"
PAIR_INFO = b"GOTD2-pair"
ID_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def b64(b):
    return base64.b64encode(b).decode()


def unb64(s):
    return base64.b64decode(s.encode() if isinstance(s, str) else s, validate=True)


def safe_id(s, max_len=64):
    return isinstance(s, str) and 0 < len(s) <= max_len and all(c in ID_OK for c in s)


# ──────────────────────────────────────────── keys

def new_ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def pub_der_b64(key):
    pub = key.public_key() if hasattr(key, "public_key") else key
    return b64(pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))


def load_pub(b64der):
    pub = serialization.load_der_public_key(unb64(b64der))
    if not isinstance(pub, ec.EllipticCurvePublicKey) or pub.curve.name != "secp256r1":
        raise ValueError("not a P-256 key")
    return pub


def device_id(pub_b64):
    """Short stable id of a device = first 16 hex of SHA-256 of its public key."""
    return hashlib.sha256(unb64(pub_b64)).hexdigest()[:16]


def key_pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def key_from_pem(pem):
    return serialization.load_pem_private_key(pem.encode() if isinstance(pem, str) else pem, password=None)


# ──────────────────────────────────────────── sign / verify

def signed_bytes(req_id, req_json):
    return SIGN_PREFIX + req_id.encode("utf-8") + b"\n" + req_json.encode("utf-8")


def sign_request(key, req):
    """-> (req_id, envelope dict). [req] gets "v":2 if missing."""
    req = dict(req)
    req.setdefault("v", 2)
    req_json = json.dumps(req, ensure_ascii=False, separators=(",", ":"))
    sig = key.sign(signed_bytes(req["id"], req_json), ec.ECDSA(hashes.SHA256()))
    return req["id"], {"v": 2, "dev": device_id(pub_der_b64(key)), "sig": b64(sig), "req": req_json}


def verify_envelope(env, req_id, devices):
    """-> (request dict, device entry or None for a pair claim). Raises ValueError with a plain reason."""
    if not isinstance(env, dict) or env.get("v") != 2:
        raise ValueError("This request comes from an older app or sender. Update it (v6) and send again.")
    req_json = env.get("req")
    if not isinstance(req_json, str) or len(req_json) > 400_000:
        raise ValueError("bad request")
    req = json.loads(req_json)
    if req.get("id") != req_id:
        raise ValueError("The request id does not match.")
    sig = unb64(env.get("sig", ""))
    if req.get("op") == "pair_claim":
        pub_b64 = env.get("pub", "")
        pub = load_pub(pub_b64)
        if env.get("dev") != device_id(pub_b64):
            raise ValueError("bad device id")
        try:
            pub.verify(sig, signed_bytes(req_id, req_json), ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            raise ValueError("The new device's signature is wrong.")
        req["_pub"] = pub_b64
        return req, None
    dev = next((d for d in devices if d.get("id") == env.get("dev")), None)
    if not dev:
        raise ValueError("This device is not (or no longer) one of your devices. Add it again from a device you use.")
    try:
        load_pub(dev["pub"]).verify(sig, signed_bytes(req_id, req_json), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise ValueError("The request's signature is wrong - it was not made by your device.")
    return req, dev


# ──────────────────────────────────────────── the inbox box (same as v5)

def seal_box(payload_obj, req_id, inbox_pub_b64):
    pub = serialization.load_der_public_key(unb64(inbox_pub_b64))
    aes = AESGCM.generate_key(bit_length=256)
    iv = secrets.token_bytes(12)
    ct = AESGCM(aes).encrypt(iv, json.dumps(payload_obj, ensure_ascii=False).encode("utf-8"), req_id.encode("utf-8"))
    ek = pub.encrypt(aes, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    env = {"v": 1, "alg": "RSA-OAEP-256+A256GCM", "ek": b64(ek), "iv": b64(iv), "ct": b64(ct)}
    return b64(json.dumps(env).encode())


def open_box(box_b64, req_id, inbox_pem):
    env = json.loads(unb64(box_b64))
    if env.get("v") != 1 or env.get("alg") != "RSA-OAEP-256+A256GCM":
        raise ValueError("unknown box format")
    key = key_from_pem(inbox_pem)
    aes = key.decrypt(unb64(env["ek"]), padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    if len(aes) != 32:
        raise ValueError("bad key length")
    plain = AESGCM(aes).decrypt(unb64(env["iv"]), unb64(env["ct"]), req_id.encode("utf-8"))
    return json.loads(plain.decode("utf-8"))


# ──────────────────────────────────────────── bundle for a new device (ECDH)

def _pair_key(shared, pid):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=pid.encode("utf-8"), info=PAIR_INFO).derive(shared)


def seal_bundle(obj, pid, xfer_pub_b64):
    peer = load_pub(xfer_pub_b64)
    eph = new_ec_key()
    k = _pair_key(eph.exchange(ec.ECDH(), peer), pid)
    iv = secrets.token_bytes(12)
    ct = AESGCM(k).encrypt(iv, json.dumps(obj).encode("utf-8"), pid.encode("utf-8"))
    return {"v": 2, "epk": pub_der_b64(eph), "iv": b64(iv), "ct": b64(ct)}


def open_bundle(bundle, pid, xfer_key):
    if bundle.get("v") != 2:
        raise ValueError("unknown bundle")
    k = _pair_key(xfer_key.exchange(ec.ECDH(), load_pub(bundle["epk"])), pid)
    return json.loads(AESGCM(k).decrypt(unb64(bundle["iv"]), unb64(bundle["ct"]), pid.encode("utf-8")))


# ──────────────────────────────────────────── team key (plan etc.)

def team_seal(obj, team_key_b64, what):
    iv = secrets.token_bytes(12)
    ct = AESGCM(unb64(team_key_b64)).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode("utf-8"), what.encode())
    return json.dumps({"v": 1, "iv": b64(iv), "ct": b64(ct)})


def team_open(text, team_key_b64, what):
    env = json.loads(text)
    return json.loads(AESGCM(unb64(team_key_b64)).decrypt(unb64(env["iv"]), unb64(env["ct"]), what.encode()).decode("utf-8"))


# KT 400 (18 Nov 2026): the device access token for your devices, sealed with the team key, on the public gotd-outbox
# branch (access.json). A phone whose saved token was replaced on GitHub reads it WITHOUT a token, opens it with the
# team key only your devices have, and goes on - no "add the phone again" after a new token on the PC.
ACCESS_PATH = "access.json"
ACCESS_WHAT = "gotd-access"


def access_doc(token, team_key_b64):
    return team_seal({"t": token}, team_key_b64, ACCESS_WHAT)


def access_token_of(text, team_key_b64):
    """The token in an access.json text ('' when it cannot be opened)."""
    try:
        return str(team_open(text, team_key_b64, ACCESS_WHAT).get("t", ""))
    except Exception:
        return ""


def seal_label(label, team_key_b64):
    """KT 332: a device's name (phone model / PC name) is sealed in the public devices.json - only your devices read it."""
    return team_seal({"l": str(label)[:30]}, team_key_b64, "gotd-label")


def device_label(d, team_key_b64):
    """The name of a devices.json entry (sealed "lbl", or an older plain "label")."""
    if d.get("lbl") and team_key_b64:
        try:
            return team_open(d["lbl"], team_key_b64, "gotd-label").get("l", "") or "device"
        except Exception:
            return "device"
    return d.get("label", "") or "device"


def new_team_key():
    return b64(secrets.token_bytes(32))


def ticket_hash(ticket):
    return hashlib.sha256(("GOTD2-ticket\n" + ticket).encode()).hexdigest()


# ──────────────────────────────────────────── this PC's private store (Windows DPAPI / macOS Keychain)

class StoreError(Exception):
    pass


def _dpapi(data, protect):
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    entropy_raw = b"GOTD2 sender"
    ebuf = ctypes.create_string_buffer(entropy_raw, len(entropy_raw))
    ent = BLOB(len(entropy_raw), ctypes.cast(ebuf, ctypes.POINTER(ctypes.c_char)))
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    flags = 0x1                                  # CRYPTPROTECT_UI_FORBIDDEN
    ok = fn(ctypes.byref(inp), None, ctypes.byref(ent), None, None, flags, ctypes.byref(out))
    if not ok:
        raise StoreError("Windows could not " + ("protect" if protect else "open") + " the sender's keys "
                         "(they belong to another Windows user). Pair this PC again.")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


KEYCHAIN_SERVICE = "GOTD sender v6"


class PrivateStore:
    """One small secret JSON (device key, access token, team key) for this PC user only."""

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "device_v6.protected")
        self.test = os.environ.get("GOTD_TEST_PLAIN_STORE") == "1"     # tests on Linux only

    def exists(self):
        if sys.platform == "darwin" and not self.test:
            return subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", "device"],
                                  capture_output=True).returncode == 0
        return os.path.exists(self.path)

    def save(self, obj):
        raw = json.dumps(obj).encode()
        if self.test:
            blob = raw
        elif sys.platform == "win32":
            blob = _dpapi(raw, True)
        elif sys.platform == "darwin":
            # KT 332: the secret goes in through stdin ("security -i"), never on the command line (visible to other
            # programs in the process list).
            r = subprocess.run(["security", "-i"], input=f'add-generic-password -U -s "{KEYCHAIN_SERVICE}" -a device -w {b64(raw)}\n',
                               capture_output=True, text=True)
            if r.returncode == 0 and "error" in (r.stdout + r.stderr).lower():
                r = subprocess.CompletedProcess(r.args, 1, r.stdout, r.stderr)
            if r.returncode != 0:
                raise StoreError("The macOS Keychain refused to keep the sender's keys: " + (r.stderr or "").strip()[:160])
            return
        else:
            raise StoreError("This PC has no protected key store (Windows or macOS needed).")
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, self.path)

    def load(self):
        if sys.platform == "darwin" and not self.test:
            r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", "device", "-w"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                return None
            return json.loads(unb64(r.stdout.strip()))
        if not os.path.exists(self.path):
            return None
        with open(self.path, "rb") as f:
            blob = f.read()
        raw = blob if self.test else _dpapi(blob, False)
        return json.loads(raw)

    def clear(self):
        if sys.platform == "darwin" and not self.test:
            subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", "device"], capture_output=True)
            return
        try:
            os.remove(self.path)
        except OSError:
            pass
