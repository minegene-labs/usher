"""The token the Release workflow pushes with: .github/scripts/app_token.py signs a JWT with the release app's key and
trades it for an installation token limited to this repository and to Contents: write. No GitHub is called: the API is a
stand-in, and the key is made up for each run."""

import base64
import importlib.util
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("app_token", ROOT / ".github" / "scripts" / "app_token.py")
app_token = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_token)

TOKEN = "ghs_madeUpInstallationToken0123456789"
NOW = 1_800_000_000


def b64url_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class FakeGitHub:
    """Answers the two calls the script makes and records them."""

    def __init__(self, installation=None, grant=None, refuse=None):
        self.installation = {"id": 4242} if installation is None else installation
        self.grant = {"token": TOKEN, "permissions": {"contents": "write"}} if grant is None else grant
        self.refuse = refuse
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append((request.get_method(), request.full_url, dict(request.header_items()), request.data))
        if self.refuse:
            code, message = self.refuse
            raise urllib.error.HTTPError(request.full_url, code, "refused", {}, io.BytesIO(json.dumps({"message": message}).encode()))
        body = self.installation if request.full_url.endswith("/installation") else self.grant
        return io.BytesIO(json.dumps(body).encode())


@unittest.skipUnless(shutil.which("openssl"), "needs openssl")
class TokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        key = Path(cls.folder.name) / "key.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
        cls.key = key.read_text(encoding="ascii")
        # GitHub hands out keys in the older, PKCS#1 form ("BEGIN RSA PRIVATE KEY"); OpenSSL 3 writes PKCS#8 by default.
        traditional = Path(cls.folder.name) / "traditional.pem"
        subprocess.run(["openssl", "rsa", "-in", str(key), "-traditional", "-out", str(traditional)], check=True, capture_output=True)
        cls.traditional_key = traditional.read_text(encoding="ascii")
        cls.public = Path(cls.folder.name) / "public.pem"
        subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(cls.public)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def test_the_jwt_is_signed_with_the_key_and_lasts_under_ten_minutes(self):
        self.check_jwt(self.key)

    def test_a_key_in_the_form_github_hands_out_signs_too(self):
        self.assertIn("BEGIN RSA PRIVATE KEY", self.traditional_key)
        self.check_jwt(self.traditional_key)

    def check_jwt(self, key):
        jwt = app_token.make_jwt("12345", key, NOW)
        header, payload, signature = jwt.split(".")
        self.assertEqual(json.loads(b64url_decode(header)), {"alg": "RS256", "typ": "JWT"})
        claims = json.loads(b64url_decode(payload))
        self.assertEqual(claims["iss"], "12345")
        self.assertLessEqual(claims["exp"] - claims["iat"], 600)
        self.assertLess(claims["iat"], NOW)  # dated back, so a clock a little ahead is no problem
        self.assertGreater(claims["exp"], NOW)
        signature_file = Path(self.folder.name) / "signature"
        signature_file.write_bytes(b64url_decode(signature))
        verified = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(self.public), "-signature", str(signature_file)],
            input=f"{header}.{payload}".encode(),
            capture_output=True,
        )
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_the_token_is_limited_to_this_repository_and_to_contents(self):
        fake = FakeGitHub()
        with mock.patch.object(app_token.urllib.request, "urlopen", fake):
            token = app_token.installation_token("12345", self.key, "example/usher", "https://api.example.invalid", NOW)
        self.assertEqual(token, TOKEN)
        (first, second) = fake.calls
        self.assertEqual(first[:2], ("GET", "https://api.example.invalid/repos/example/usher/installation"))
        self.assertEqual(second[:2], ("POST", "https://api.example.invalid/app/installations/4242/access_tokens"))
        self.assertEqual(json.loads(second[3]), {"repositories": ["usher"], "permissions": {"contents": "write"}})
        for call in fake.calls:
            self.assertTrue(call[2]["Authorization"].startswith("Bearer ey"))  # the JWT, never the key
            self.assertNotIn("PRIVATE KEY", json.dumps(call[2]))

    def test_the_token_goes_to_the_output_and_is_masked_first(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "output"
            env = {"APP_ID": "12345", "APP_KEY": self.key, "GH_REPO": "example/usher", "GITHUB_OUTPUT": str(output)}
            printed = io.StringIO()
            with mock.patch.object(app_token.urllib.request, "urlopen", FakeGitHub()), mock.patch("sys.stdout", printed):
                self.assertEqual(app_token.main(env), 0)
            self.assertEqual(output.read_text(encoding="utf-8"), f"token={TOKEN}\n")
            self.assertEqual(printed.getvalue(), f"::add-mask::{TOKEN}\n")  # masked, and the only thing printed
            self.assertNotIn("PRIVATE KEY", printed.getvalue())

    def run_main(self, fake, **env):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "output"
            base = {"APP_ID": "12345", "APP_KEY": self.key, "GH_REPO": "example/usher", "GITHUB_OUTPUT": str(output)} | env
            printed = io.StringIO()
            with mock.patch.object(app_token.urllib.request, "urlopen", fake), mock.patch("sys.stdout", printed):
                code = app_token.main(base)
            return code, printed.getvalue(), output.exists()

    def test_a_refusal_says_what_github_said_and_where_to_look_and_gives_no_token(self):
        code, printed, wrote = self.run_main(FakeGitHub(refuse=(401, "A JSON web token could not be decoded")))
        self.assertEqual(code, 1)
        self.assertIn("::error::Can't get a token for the release app: GitHub answered 401", printed)
        self.assertIn("A JSON web token could not be decoded", printed)
        self.assertIn("CONTRIBUTING.md, Releases", printed)
        self.assertFalse(wrote)

    def test_an_app_that_is_not_installed_here_fails_clearly(self):
        code, printed, wrote = self.run_main(FakeGitHub(refuse=(404, "Not Found")))
        self.assertEqual(code, 1)
        self.assertIn("GitHub answered 404", printed)
        self.assertFalse(wrote)
        code, printed, wrote = self.run_main(FakeGitHub(installation={"message": "odd answer"}))
        self.assertEqual(code, 1)
        self.assertIn("not installed on example/usher", printed)

    def test_an_answer_without_a_token_fails(self):
        code, printed, wrote = self.run_main(FakeGitHub(grant={"permissions": {}}))
        self.assertEqual((code, wrote), (1, False))
        self.assertIn("no installation token", printed)

    def test_the_key_never_reaches_an_error_message(self):
        broken = self.key.replace("MII", "XXX", 1)  # still PEM-shaped, but openssl can't read it
        code, printed, _ = self.run_main(FakeGitHub(), APP_KEY=broken)
        self.assertEqual(code, 1)
        self.assertIn("could not be used to sign", printed)
        for line in broken.splitlines()[1:-1]:
            self.assertNotIn(line, printed)

    def test_a_key_that_is_not_a_private_key_is_refused_before_anything_is_called(self):
        fake = FakeGitHub()
        code, printed, _ = self.run_main(fake, APP_KEY="not a key")
        self.assertEqual(code, 1)
        self.assertIn("not a private key in PEM form", printed)
        self.assertEqual(fake.calls, [])
        self.assertNotIn("not a key\n", printed.replace("not a private key", ""))

    def test_a_missing_setting_says_the_app_is_not_set_up(self):
        for missing in ("APP_ID", "APP_KEY"):
            fake = FakeGitHub()
            code, printed, _ = self.run_main(fake, **{missing: ""})
            self.assertEqual(code, 1, missing)
            self.assertIn("The release app is not set up", printed)
            self.assertEqual(fake.calls, [])

    def test_a_repository_name_is_checked_before_it_is_put_in_a_url(self):
        fake = FakeGitHub()
        code, printed, _ = self.run_main(fake, GH_REPO="example/usher/../../other")
        self.assertEqual(code, 1)
        self.assertIn("Not a repository name", printed)
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
