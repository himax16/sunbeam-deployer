"""Tests for sunbeam_deployer.phases.cluster — token extraction."""

from __future__ import annotations

from sunbeam_deployer.phases.cluster import _extract_token


class TestExtractToken:
    def test_yaml_format(self) -> None:
        output = "token: eyJhbGciOiJIUzI1NiJ9.dGVzdA.abc123\n"
        assert _extract_token(output) == "eyJhbGciOiJIUzI1NiJ9.dGVzdA.abc123"

    def test_yaml_format_with_quotes(self) -> None:
        output = "token: 'mytoken12345678901234'\n"
        assert _extract_token(output) == "mytoken12345678901234"

    def test_yaml_format_double_quotes(self) -> None:
        output = 'token: "mytoken12345678901234"\n'
        assert _extract_token(output) == "mytoken12345678901234"

    def test_yaml_format_with_prefix(self) -> None:
        output = (
            "Some preamble\n"
            "token: ABCDEFGHIJKLMNOPQRSTuvwxyz1234567890\n"
            "More text\n"
        )
        assert _extract_token(output) == "ABCDEFGHIJKLMNOPQRSTuvwxyz1234567890"

    def test_bare_base64_fallback(self) -> None:
        output = "Generating join token...\nABCDEFghijklmnop1234567890+/=\n"
        assert _extract_token(output) == "ABCDEFghijklmnop1234567890+/="

    def test_bare_base64_picks_last_line(self) -> None:
        output = (
            "short\n"
            "notavalidtokenxxxxxxxxxxxxxx\n"
            "ABCDEFGhijklmnopqrstuVWXYZ0123456789_.-\n"
        )
        result = _extract_token(output)
        assert result == "ABCDEFGhijklmnopqrstuVWXYZ0123456789_.-"

    def test_returns_none_for_empty(self) -> None:
        assert _extract_token("") is None

    def test_returns_none_for_no_token(self) -> None:
        output = "Some random output\nwithout any token\n"
        assert _extract_token(output) is None

    def test_returns_none_for_short_base64(self) -> None:
        # Strings < 20 chars should not be considered tokens
        output = "abc123\n"
        assert _extract_token(output) is None

    def test_yaml_empty_value(self) -> None:
        output = "token:\n"
        assert _extract_token(output) is None

    def test_real_world_multiline(self) -> None:
        token_val = (
            "eyJuYW1lIjoiYm0xLnJlcyIsInNlY3JldCI6ImFiY2RlZjEyMzQ1Njc4OTAi"
        )
        output = (
            f"Adding node bm1.res to cluster...\ntoken: {token_val}\nDone.\n"
        )
        token = _extract_token(output)
        assert token == token_val
