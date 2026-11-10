"""Account setup: finding servers (autoconfig), browser sign-in (oauth2) and credentials.

    python3 -m unittest tests.test_accounts
"""
import base64
import hashlib
import http.server
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request

os.environ["POSTROOM_PROFILE"] = tempfile.mkdtemp(prefix="postroom-test-")
os.environ["POSTROOM_NO_KEYRING"] = "1"

from postroom import autoconfig, credentials, oauth2, passwords  # noqa: E402
from postroom.config import Account  # noqa: E402

GMAIL_XML = b"""<?xml version="1.0"?>
<clientConfig version="1.1">
  <emailProvider id="googlemail.com">
    <domain>gmail.com</domain>
    <displayName>Google Mail</displayName>
    <incomingServer type="pop3">
      <hostname>pop.gmail.com</hostname><port>995</port><socketType>SSL</socketType>
      <username>%EMAILADDRESS%</username><authentication>OAuth2</authentication>
    </incomingServer>
    <incomingServer type="imap">
      <hostname>imap.example.net</hostname><port>143</port><socketType>plain</socketType>
      <username>%EMAILLOCALPART%</username><authentication>password-cleartext</authentication>
    </incomingServer>
    <incomingServer type="imap">
      <hostname>imap.gmail.com</hostname><port>993</port><socketType>SSL</socketType>
      <username>%EMAILADDRESS%</username>
      <authentication>OAuth2</authentication>
      <authentication>password-cleartext</authentication>
    </incomingServer>
    <outgoingServer type="smtp">
      <hostname>smtp.gmail.com</hostname><port>587</port><socketType>STARTTLS</socketType>
      <username>%EMAILADDRESS%</username><authentication>password-cleartext</authentication>
    </outgoingServer>
    <outgoingServer type="smtp">
      <hostname>smtp.gmail.com</hostname><port>465</port><socketType>SSL</socketType>
      <username>%EMAILADDRESS%</username><authentication>OAuth2</authentication>
    </outgoingServer>
  </emailProvider>
</clientConfig>"""


class AutoconfigTest(unittest.TestCase):
    def test_reads_encrypted_imap_and_smtp(self):
        cfg = autoconfig.parse_config_xml(GMAIL_XML, "db")
        self.assertEqual(cfg.display_name, "Google Mail")
        self.assertEqual((cfg.incoming.hostname, cfg.incoming.port, cfg.incoming.security),
                         ("imap.gmail.com", 993, autoconfig.SSL))  # plain IMAP and POP3 skipped
        self.assertEqual(cfg.incoming.auth, ["oauth2", "password"])
        self.assertTrue(cfg.oauth2)
        self.assertEqual((cfg.outgoing.port, cfg.outgoing.security), (465, autoconfig.SSL))

    def test_rejects_config_with_only_plain_servers(self):
        xml = GMAIL_XML.replace(b"<socketType>SSL</socketType>", b"<socketType>plain</socketType>") \
                       .replace(b"<socketType>STARTTLS</socketType>", b"<socketType>plain</socketType>")
        with self.assertRaises(ValueError):
            autoconfig.parse_config_xml(xml, "db")

    def test_rejects_bad_hostnames(self):
        # The only encrypted IMAP server has a bad name, so the whole file is refused.
        xml = GMAIL_XML.replace(b"imap.gmail.com", b"imap.gmail.com/../evil")
        with self.assertRaises(ValueError):
            autoconfig.parse_config_xml(xml, "db")
        for bad in ("", "a b.com", "-x.com", "localhost/evil", "x" * 70 + ".com"):
            with self.assertRaises(ValueError, msg=bad):
                autoconfig.clean_hostname(bad)

    def test_placeholders(self):
        self.assertEqual(autoconfig.expand("%EMAILLOCALPART%@%EMAILDOMAIN%|%EMAILADDRESS%",
                                           "ann@example.org"),
                         "ann@example.org|ann@example.org")
        self.assertEqual(autoconfig.expand("%EMAILLOCALPART%", "ann@example.org"), "ann")

    def test_domain_of(self):
        self.assertEqual(autoconfig.domain_of("Ann@Example.ORG"), "example.org")
        with self.assertRaises(ValueError):
            autoconfig.domain_of("no-at-sign")

    def test_base_domain(self):
        self.assertEqual(autoconfig.base_domain("aspmx.l.google.com"), "google.com")
        self.assertEqual(autoconfig.base_domain("mx.mail.example.co.uk"), "example.co.uk")

    def test_dns_name_compression(self):
        msg = b"\0" * 12 + b"\x07example\x03com\x00" + b"\x04mail\xc0\x0c"
        self.assertEqual(autoconfig._read_name(msg, 12), ("example.com", 25))
        self.assertEqual(autoconfig._read_name(msg, 25), ("mail.example.com", 32))


class FakeProvider(http.server.BaseHTTPRequestHandler):
    """A token endpoint that checks the PKCE verifier and hands out tokens."""
    challenge = ""
    refreshes = 0

    def do_POST(self):  # noqa: N802
        form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
        if form["grant_type"] == "authorization_code":
            digest = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest())
            ok = form["code"] == "the-code" and digest.rstrip(b"=").decode() == FakeProvider.challenge
            body = ({"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600}
                    if ok else {"error": "invalid_grant"})
        else:
            FakeProvider.refreshes += 1
            ok = form["refresh_token"] == "refresh-1"
            body = ({"access_token": f"access-r{FakeProvider.refreshes}", "expires_in": 3600}
                    if ok else {"error": "invalid_grant"})
        data = json.dumps(body).encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class OAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), FakeProvider)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        port = cls.server.server_address[1]
        cls.issuer = oauth2.Issuer("test.provider", "https://auth.example.test/authorize",
                                   f"http://127.0.0.1:{port}/token", "mail")
        cls.client = oauth2.Client("postroom-test")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _browser(self, url, **override):
        """Do what the provider's page does after sign-in: redirect to redirect_uri."""
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        FakeProvider.challenge = q["code_challenge"]
        reply = {"code": "the-code", "state": q["state"], **override}
        with urllib.request.urlopen(q["redirect_uri"] + "?" + urllib.parse.urlencode(reply),
                                    timeout=5) as r:
            return q, r.read().decode()

    def test_sign_in_round_trip(self):
        s = oauth2.SignIn(self.issuer, self.client, login_hint="ann@example.org")
        url = s.start()
        q, page = self._browser(url)
        self.assertEqual(q["client_id"], "postroom-test")
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(q["login_hint"], "ann@example.org")
        self.assertTrue(q["redirect_uri"].startswith("http://127.0.0.1:"))
        self.assertIn("Signed in to Postroom", page)
        t = s.wait(5)
        self.assertEqual((t.refresh_token, t.access_token), ("refresh-1", "access-1"))
        self.assertTrue(t.valid())

    def test_wrong_state_is_refused(self):
        s = oauth2.SignIn(self.issuer, self.client)
        url = s.start()
        _, page = self._browser(url, state="forged")
        self.assertIn("could not sign in", page)
        with self.assertRaises(oauth2.OAuthError):
            s.wait(5)

    def test_provider_error_is_reported(self):
        s = oauth2.SignIn(self.issuer, self.client)
        url = s.start()
        self._browser(url, code="", error="access_denied")
        with self.assertRaisesRegex(oauth2.OAuthError, "access_denied"):
            s.wait(5)

    def test_cancel(self):
        s = oauth2.SignIn(self.issuer, self.client)
        s.start()
        s.cancel()
        with self.assertRaises(oauth2.OAuthError):
            s.wait(1)

    def test_refresh_and_bad_grant(self):
        t = oauth2.refresh(self.issuer, self.client, oauth2.Tokens("refresh-1"))
        self.assertTrue(t.access_token.startswith("access-r") and t.valid())
        with self.assertRaisesRegex(oauth2.OAuthError, "Sign in again"):
            oauth2.refresh(self.issuer, self.client, oauth2.Tokens("revoked"))

    def test_granted_scope_is_kept(self):
        t = oauth2.Tokens.loads(oauth2.Tokens("r", scope="a b").dumps())
        self.assertTrue(t.grants("b") and not t.grants("c"))
        self.assertFalse(oauth2.Tokens.loads('{"refresh_token": "r"}').grants("a"))  # older saves
        self.assertIn(oauth2.GOOGLE_CONTACTS_SCOPE, oauth2.ISSUERS["accounts.google.com"].scope)

    def test_xoauth2(self):
        self.assertEqual(oauth2.xoauth2("ann@example.org", "tok"),
                         "user=ann@example.org\x01auth=Bearer tok\x01\x01")

    def test_issuers(self):
        self.assertEqual(oauth2.issuer_for("imap.gmail.com").name, "accounts.google.com")
        self.assertEqual(oauth2.issuer_for("outlook.office365.com").name,
                         "login.microsoftonline.com")
        self.assertEqual(oauth2.issuer_for("eu.imap.mail.yahoo.com").name, "login.yahoo.com")
        self.assertIsNone(oauth2.issuer_for("mail.example.org"))

    def test_user_registration_overrides_builtin(self):
        google = oauth2.ISSUERS["accounts.google.com"]
        self.assertIs(oauth2.client_for(google), oauth2.THUNDERBIRD_CLIENTS[google.name])
        oauth2.clients_path().parent.mkdir(parents=True, exist_ok=True)
        oauth2.clients_path().write_text('{"accounts.google.com": {"client_id": "mine"}}')
        try:
            self.assertEqual(oauth2.client_for(google).client_id, "mine")
        finally:
            oauth2.clients_path().unlink()


class CredentialsTest(unittest.TestCase):
    def test_password_account(self):
        a = Account(name="P", email="p@example.org")
        passwords.store(a.id, "x", "app-pw")
        self.assertEqual(credentials.secret(a), "app-pw")

    def test_oauth_account_refreshes_and_caches(self):
        a = Account(name="G", email="g@gmail.com", auth="oauth2")
        calls = []

        def fake_refresh(issuer, client, tokens):
            calls.append(tokens.refresh_token)
            tokens.access_token, tokens.expires = f"tok{len(calls)}", time.time() + 3600
            return tokens
        orig_refresh, orig_client = oauth2.refresh, oauth2.client_for
        oauth2.refresh, oauth2.client_for = fake_refresh, lambda issuer: oauth2.Client("id")
        try:
            credentials.save_oauth(a, oauth2.Tokens("r1"))
            credentials._tokens.clear()  # as after a restart: only the stored refresh token
            self.assertEqual(credentials.secret(a), "tok1")
            self.assertEqual(credentials.secret(a), "tok1")  # cached, no second refresh
            credentials.invalidate(a)
            self.assertEqual(credentials.secret(a), "tok2")
            self.assertEqual(calls, ["r1", "r1"])
        finally:
            oauth2.refresh, oauth2.client_for = orig_refresh, orig_client

    def test_oauth_without_registration_explains(self):
        a = Account(name="G", email="g@gmail.com", auth="oauth2")
        credentials.save_oauth(a, oauth2.Tokens("r1"))
        credentials._tokens.clear()
        saved = dict(oauth2.THUNDERBIRD_CLIENTS)
        oauth2.THUNDERBIRD_CLIENTS.clear()  # no built-in fallback, and no network use
        try:
            with self.assertRaisesRegex(credentials.CredentialError, "registration"):
                credentials.secret(a)
        finally:
            oauth2.THUNDERBIRD_CLIENTS.update(saved)

    def test_forget(self):
        a = Account(name="G", email="g@gmail.com", auth="oauth2")
        passwords.store(a.id, "x", "pw")
        credentials.save_oauth(a, oauth2.Tokens("r1"))
        credentials.forget(a)
        self.assertIsNone(passwords.lookup(a.id))
        self.assertIsNone(passwords.lookup(a.id + ":oauth2"))


if __name__ == "__main__":
    unittest.main()
