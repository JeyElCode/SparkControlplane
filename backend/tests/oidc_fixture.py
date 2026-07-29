"""A minimal, in-process OpenID Provider for tests.

Testing OIDC against a real identity provider is not testable — it needs
network, a tenant, and a human at a consent screen. So this fixture is the
provider: it holds an RSA keypair, serves a discovery document and a JWKS, and
mints ID tokens. That makes the *negative* cases reachable, which is the point —
a token signed by the wrong key, with the wrong audience, from the wrong issuer,
with a stale nonce, or with an algorithm the caller did not ask for.

Deliberately hand-rolled rather than a library: the tests must be able to emit
tokens no correct library would ever produce.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64u_json(obj) -> str:
    return b64u(json.dumps(obj, separators=(",", ":")).encode())


def _int_to_b64u(n: int) -> str:
    return b64u(n.to_bytes((n.bit_length() + 7) // 8, "big"))


class FakeIdP:
    """An OpenID Provider with a real RSA key, entirely in memory."""

    def __init__(self, issuer: str = "https://idp.example.test", kid: str = "test-key-1") -> None:
        self.issuer = issuer
        self.kid = kid
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        # A second key that is NOT published in the JWKS — for "signed by
        # something else" tests.
        self._rogue = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # --- what the portal fetches -----------------------------------------
    def discovery(self) -> dict:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/keys",
            "end_session_endpoint": f"{self.issuer}/logout",
            "id_token_signing_alg_values_supported": ["RS256"],
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
        }

    def jwks(self) -> dict:
        pub = self._key.public_key().public_numbers()
        return {"keys": [{
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": self.kid,
            "n": _int_to_b64u(pub.n),
            "e": _int_to_b64u(pub.e),
        }]}

    def public_pem(self) -> bytes:
        return self._key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    # --- token minting ----------------------------------------------------
    def id_token(
        self,
        *,
        aud,
        sub: str = "user-guid-1",
        nonce: str | None = None,
        claims: dict | None = None,
        issuer: str | None = None,
        kid: str | None = None,
        alg: str = "RS256",
        expires_in: int = 300,
        issued_at: float | None = None,
        rogue_key: bool = False,
    ) -> str:
        """Mint an ID token. Every knob exists so a test can produce a *bad* one."""
        now = time.time() if issued_at is None else issued_at
        payload = {
            "iss": issuer or self.issuer,
            "sub": sub,
            "aud": aud,
            "exp": int(now + expires_in),
            "iat": int(now),
            "nbf": int(now),
            "jti": str(uuid.uuid4()),
        }
        if nonce is not None:
            payload["nonce"] = nonce
        payload.update(claims or {})

        header = {"alg": alg, "typ": "JWT", "kid": kid or self.kid}
        signing_input = f"{b64u_json(header)}.{b64u_json(payload)}".encode()

        if alg == "none":
            return f"{signing_input.decode()}."
        if alg.startswith("HS"):
            # The alg-confusion attack: HMAC the signing input with the
            # *public* key, betting the verifier will accept HS256 and hand it
            # the public key as the shared secret.
            sig = hmac.new(self.public_pem(), signing_input, hashlib.sha256).digest()
            return f"{signing_input.decode()}.{b64u(sig)}"

        key = self._rogue if rogue_key else self._key
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input.decode()}.{b64u(sig)}"
