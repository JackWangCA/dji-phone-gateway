import importlib.util
import os
import pathlib
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))
import cloudflare_access as access
import web_dashboard as dashboard

CONFIG = {"CLOUDFLARE_ACCESS_ISSUER": "https://example.cloudflareaccess.com",
          "CLOUDFLARE_ACCESS_AUD": "a" * 64, "CLOUDFLARE_ACCESS_HOSTNAME": "sms.example.com"}


class RoutingTests(unittest.TestCase):
    def test_incomplete_config_fails_closed(self):
        with mock.patch.dict(os.environ, {**CONFIG, "CLOUDFLARE_ACCESS_AUD": ""}):
            with self.assertRaises(ValueError):
                access.configuration()

    def test_public_host_requires_access_without_header(self):
        with mock.patch.dict(os.environ, CONFIG):
            self.assertTrue(access.required({"Host": "SMS.EXAMPLE.COM:443"}))
            self.assertFalse(access.required({"Host": "smspi.local:8080"}))
            self.assertTrue(access.required({"Host": "smspi.local", "Cf-Access-Jwt-Assertion": ""}))

    def test_public_host_cannot_fall_back_to_local_cookie(self):
        with mock.patch.dict(os.environ, CONFIG), mock.patch.object(dashboard, "verify_session_token") as local:
            handler = SimpleNamespace(headers={"Host": "sms.example.com", "Cookie": "dji_gateway_session=valid"})
            self.assertFalse(dashboard.Handler.authorized(handler))
            local.assert_not_called()
            handler.headers["Host"] = "smspi.local"
            local.return_value = True
            self.assertTrue(dashboard.Handler.authorized(handler))

    def test_public_login_does_not_accept_local_password(self):
        handler = mock.Mock()
        handler.path = "/login"
        handler.headers = {"Host": "sms.example.com"}
        handler.authenticate.return_value = False
        with mock.patch.dict(os.environ, CONFIG):
            dashboard.Handler.do_POST(handler)
        handler.credentials_match.assert_not_called()

    def test_cloudflare_logout_and_csrf(self):
        handler = mock.Mock()
        handler.path = "/logout"
        handler.headers = {"Host": "sms.example.com"}
        handler.form.return_value = {"csrf": dashboard.CSRF_TOKEN}
        with mock.patch.dict(os.environ, CONFIG):
            dashboard.Handler.do_POST(handler)
        self.assertEqual(handler.redirect.call_args.args[0], "/cdn-cgi/access/logout")
        handler.reset_mock()
        handler.form.return_value = {"csrf": "invalid"}
        with mock.patch.object(dashboard, "page", return_value="expired"):
            dashboard.Handler.do_POST(handler)
        handler.redirect.assert_not_called()


@unittest.skipUnless(importlib.util.find_spec("jwt") and importlib.util.find_spec("cryptography"),
                     "Requires PyJWT and cryptography")
class SignatureTests(unittest.TestCase):
    def setUp(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        self.jwt = jwt
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.config = mock.patch.dict(os.environ, CONFIG)
        self.config.start()
        self.addCleanup(self.config.stop)
        client = mock.Mock()
        client.get_signing_key_from_jwt.return_value.key = self.key.public_key()
        patcher = mock.patch.object(access, "key_client", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.claims = {"iss": CONFIG["CLOUDFLARE_ACCESS_ISSUER"], "aud": ["a" * 64],
                       "sub": "user-id", "email": "owner@example.com", "type": "app",
                       "iat": int(time.time()) - 5, "exp": int(time.time()) + 300}

    def headers(self, claims=None, key=None, algorithm="RS256"):
        return {"Host": "sms.example.com", "Cf-Access-Jwt-Assertion": self.jwt.encode(
            claims if claims is not None else self.claims, key or self.key, algorithm=algorithm, headers={"kid": "test"})}

    def test_valid_signature_signs_in_without_local_session(self):
        headers = self.headers()
        self.assertEqual(access.identity(headers), "owner@example.com")
        self.assertTrue(dashboard.Handler.authorized(SimpleNamespace(headers=headers)))

    def test_rejects_expired_wrong_issuer_audience_missing_claims_and_meta(self):
        for changes in ({"exp": 1}, {"aud": ["b" * 64]}, {"iss": "https://attacker.example"},
                        {"iat": int(time.time()) + 300}, {"type": "meta"}, {"email": ""}):
            with self.subTest(changes=changes):
                self.assertIsNone(access.identity(self.headers({**self.claims, **changes})))
        claims = dict(self.claims)
        del claims["exp"]
        self.assertIsNone(access.identity(self.headers(claims)))

    def test_rejects_forged_signature_algorithm_and_spoofed_email_header(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.assertIsNone(access.identity(self.headers(key=other_key)))
        self.assertIsNone(access.identity(self.headers(key="attacker-secret", algorithm="HS256")))
        self.assertIsNone(access.identity({"Cf-Access-Authenticated-User-Email": "owner@example.com"}))

    def test_key_server_failure_denies_access(self):
        with mock.patch.object(access, "key_client", side_effect=OSError):
            self.assertIsNone(access.identity(self.headers()))
