"""Tests for credential redaction before persistence."""

from __future__ import annotations

import unittest

from harness.core.redaction import REDACTED, contains_secret, redact, redact_mapping


class TestRedaction(unittest.TestCase):
    def test_common_token_prefixes(self) -> None:
        samples = [
            "sk-proj-abcdefghijklmnopqrstuvwxyz",
            "ghp_abcdefghijklmnopqrstuvwxyz123456",
            "gho_abcdefghijklmnopqrstuvwxyz123456",
            "github_pat_abcdefghijklmnopqrstuvwxyz",
            "glpat-abcdefghijklmnopqrst",
            "xoxb-1234567890-abcdefghij",
            "AKIAABCDEFGHIJKLMNOP",
            "AIza" + "a" * 35,
            "eyJabcdefghij.eyJabcdefghij.abcdefghijkl",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                cleaned = redact(f"credential={sample}")
                self.assertIn(REDACTED, cleaned)
                self.assertNotIn(sample, cleaned)
                self.assertTrue(contains_secret(f"credential={sample}"))

    def test_assignments_and_headers(self) -> None:
        text = (
            "API_KEY=super-secret-value\n"
            "client_secret: 'another-secret-value'\n"
            "Authorization: Bearer abcdefghijklmnop\n"
        )
        cleaned = redact(text)
        self.assertNotIn("super-secret-value", cleaned)
        self.assertNotIn("another-secret-value", cleaned)
        self.assertNotIn("abcdefghijklmnop", cleaned)
        self.assertEqual(cleaned.count(REDACTED), 3)

    def test_connection_strings(self) -> None:
        text = "postgresql://app:pa55word@db.internal:5432/app\nhttps://user:pass@example.test/path"
        cleaned = redact(text)
        self.assertNotIn("pa55word", cleaned)
        self.assertNotIn("user:pass", cleaned)
        self.assertIn(REDACTED, cleaned)

    def test_private_key_block(self) -> None:
        text = (
            "before\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA\n"
            "-----END RSA PRIVATE KEY-----\n"
            "after"
        )
        cleaned = redact(text)
        self.assertNotIn("MIIEowIBAAKCAQEA", cleaned)
        self.assertEqual(cleaned.count(REDACTED), 1)
        self.assertTrue(cleaned.startswith("before\n"))
        self.assertTrue(cleaned.endswith("\nafter"))

    def test_gitlab_tokens_and_bearer_headers(self) -> None:
        for sample in (
            "glpat-abcdefghijklmnopqrst",
            "X-Auth: Bearer abcdefghijklmnopqrstuvwxyz",
            "Authorization: Bearer abcdefghijklmnop",
        ):
            with self.subTest(sample=sample):
                cleaned = redact(sample)
                self.assertIn(REDACTED, cleaned)
                for secret_part in (
                    "abcdefghijklmnopqrst",
                    "abcdefghijklmnopqrstuvwxyz",
                ):
                    self.assertNotIn(secret_part, cleaned)

    def test_sensitive_assignments_leave_metadata_keys_alone(self) -> None:
        text = (
            "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz123456\n"
            "MY_SECRET=plaintext-secret\n"
            "TOKEN_BUDGET=20000\n"
            "PASSWORD_MIN_LENGTH=12\n"
        )
        cleaned = redact(text)
        self.assertNotIn("plaintext-secret", cleaned)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", cleaned)
        self.assertIn("TOKEN_BUDGET=20000", cleaned)
        self.assertIn("PASSWORD_MIN_LENGTH=12", cleaned)

    def test_redact_mapping_is_recursive_and_non_strings_survive(self) -> None:
        value = {
            "args": ["--token=abcdef123456", {"password": "hunter2hunter2"}],
            "count": 3,
            "ok": True,
        }
        cleaned = redact_mapping(value)
        self.assertEqual(cleaned["count"], 3)
        self.assertIs(cleaned["ok"], True)
        self.assertNotIn("abcdef123456", str(cleaned))
        self.assertNotIn("hunter2hunter2", str(cleaned))

    def test_command_flags_and_quoted_passphrases(self) -> None:
        samples = [
            "run --token=abcdef123456",
            "run --password hunter2",
            "run --password letmein",
            'run --api-key "quoted-secret"',
            'API_KEY="my pass phrase"',
            "password: 'correct horse battery staple'",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                cleaned = redact(sample)
                self.assertIn(REDACTED, cleaned)
                self.assertNotIn("abcdef123456", cleaned)
                self.assertNotIn("hunter2", cleaned)
                self.assertNotIn("letmein", cleaned)
                self.assertNotIn("quoted-secret", cleaned)
                self.assertNotIn("pass phrase", cleaned)
                self.assertNotIn("correct horse", cleaned)

    def test_prose_that_mentions_credentials_is_not_redacted(self) -> None:
        samples = [
            "The token budget is 20000 and the password policy is documented.",
            "Use a token: refresh regularly.",
            "Pass the --token flag to the command.",
            "Use the --password option here.",
            "token_budget=20000",
            "password_hint=use-a-passphrase",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(redact(sample), sample)
                self.assertFalse(contains_secret(sample))

    def test_dsn_password_may_contain_a_slash(self) -> None:
        cleaned = redact("postgres://user:pa55/w0rd@host/db")
        self.assertNotIn("pa55", cleaned)
        self.assertNotIn("w0rd", cleaned)
        self.assertEqual(cleaned, "postgres://" + REDACTED + "@host/db")

    def test_json_style_keys_and_metadata_keys(self) -> None:
        cleaned = redact('{"apiKey": "hunter2hunter2", "token_budget": "20000"}')
        self.assertNotIn("hunter2hunter2", cleaned)
        self.assertIn('"token_budget": "20000"', cleaned)

    def test_redact_mapping_handles_sensitive_keys_and_metadata(self) -> None:
        cleaned = redact_mapping(
            {
                "token_count": 20000,
                "authorization": "Basic dXNlcjpwYXNz",
                "nested": {"password": "hunter2hunter2"},
            }
        )
        self.assertEqual(cleaned["token_count"], 20000)
        self.assertEqual(cleaned["authorization"], REDACTED)
        self.assertEqual(cleaned["nested"]["password"], REDACTED)

    def test_plain_text_is_not_changed(self) -> None:
        text = "Run pytest before committing; token budgets are documented separately."
        self.assertEqual(redact(text), text)
        self.assertFalse(contains_secret(text))
        self.assertEqual(redact(""), "")
        self.assertFalse(contains_secret(""))


if __name__ == "__main__":
    unittest.main()
