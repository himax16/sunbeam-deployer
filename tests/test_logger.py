"""Tests for sunbeam_deployer.logger — secret redaction."""

from __future__ import annotations

from sunbeam_deployer.logger import _redact


class TestRedact:
    def test_redacts_token(self) -> None:
        text = "token: eyJhbGciOiJIUzI1NiJ9dGVzdA"
        result = _redact(text)
        assert "eyJhbG" not in result
        assert "<REDACTED>" in result

    def test_redacts_token_with_equals(self) -> None:
        text = "token='ABCDEFghijklmnop1234567890'"
        result = _redact(text)
        assert "ABCDEFghijklmnop" not in result
        assert "<REDACTED>" in result

    def test_redacts_apikey(self) -> None:
        text = "apikey: my_secret_api_key_12345"
        result = _redact(text)
        assert "my_secret_api_key" not in result
        assert "<REDACTED>" in result

    def test_redacts_password(self) -> None:
        text = "password=supersecretpassword"
        result = _redact(text)
        assert "supersecretpassword" not in result
        assert "<REDACTED>" in result

    def test_redacts_ssh_private_key(self) -> None:
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIBogIBAAJBAK+FAKE+KEY\n"
            "-----END RSA PRIVATE KEY-----"
        )
        result = _redact(text)
        assert "FAKE+KEY" not in result
        assert "<REDACTED>" in result

    def test_preserves_non_sensitive_text(self) -> None:
        text = "Deploying to host 10.0.0.1 on port 8080"
        result = _redact(text)
        assert result == text

    def test_multiple_secrets(self) -> None:
        text = "token: abc123456789012345 password=mysecret"
        result = _redact(text)
        assert "abc12345" not in result
        assert "mysecret" not in result
        assert result.count("<REDACTED>") >= 2
