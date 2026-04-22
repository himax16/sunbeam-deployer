"""Tests for executor — CommandResult, RemoteTarget, run_local."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sunbeam_deployer.executor import (
    CommandResult,
    RemoteTarget,
    _shell_quote,
    get_remote_target,
    run_host,
    run_local,
    set_remote_target,
)

# ---------------------------------------------------------------------------
# CommandResult
# ---------------------------------------------------------------------------


class TestCommandResult:
    def test_ok_when_zero_exit(self) -> None:
        r = CommandResult(returncode=0, stdout="", stderr="", duration=1.0)
        assert r.ok is True

    def test_not_ok_when_nonzero_exit(self) -> None:
        r = CommandResult(returncode=1, stdout="", stderr="", duration=1.0)
        assert r.ok is False

    def test_not_ok_when_timed_out(self) -> None:
        r = CommandResult(
            returncode=0, stdout="", stderr="", duration=1.0, timed_out=True
        )
        assert r.ok is False

    def test_not_ok_when_nonzero_and_timed_out(self) -> None:
        r = CommandResult(
            returncode=1, stdout="", stderr="", duration=1.0, timed_out=True
        )
        assert r.ok is False

    def test_default_timed_out_is_false(self) -> None:
        r = CommandResult(returncode=0, stdout="", stderr="", duration=0.0)
        assert r.timed_out is False


# ---------------------------------------------------------------------------
# RemoteTarget
# ---------------------------------------------------------------------------


class TestRemoteTarget:
    def test_ssh_base_default(self) -> None:
        target = RemoteTarget(host="10.0.0.1")
        cmd = target.ssh_base
        assert "ssh" in cmd
        assert "ubuntu@10.0.0.1" in cmd
        assert "-o" in cmd

    def test_ssh_base_with_key(self) -> None:
        target = RemoteTarget(host="10.0.0.1", key_path="/path/to/key")
        cmd = target.ssh_base
        assert "-i" in cmd
        idx = cmd.index("-i")
        assert cmd[idx + 1] == "/path/to/key"

    def test_ssh_base_custom_user(self) -> None:
        target = RemoteTarget(host="10.0.0.1", user="admin")
        cmd = target.ssh_base
        assert "admin@10.0.0.1" in cmd

    def test_ssh_base_with_options(self) -> None:
        target = RemoteTarget(
            host="10.0.0.1",
            ssh_options=["-o", "ConnectTimeout=5"],
        )
        cmd = target.ssh_base
        assert "-o" in cmd
        assert "ConnectTimeout=5" in cmd

    def test_scp_base_default(self) -> None:
        target = RemoteTarget(host="10.0.0.1")
        cmd = target.scp_base
        assert "scp" in cmd
        assert "-o" in cmd

    def test_scp_base_with_key(self) -> None:
        target = RemoteTarget(host="10.0.0.1", key_path="/key")
        cmd = target.scp_base
        assert "-i" in cmd
        idx = cmd.index("-i")
        assert cmd[idx + 1] == "/key"


# ---------------------------------------------------------------------------
# set_remote_target / get_remote_target
# ---------------------------------------------------------------------------


class TestRemoteTargetState:
    def setup_method(self) -> None:
        """Clear remote target before each test."""
        set_remote_target(None)

    def teardown_method(self) -> None:
        set_remote_target(None)

    def test_default_is_none(self) -> None:
        assert get_remote_target() is None

    def test_set_and_get(self) -> None:
        target = RemoteTarget(host="1.2.3.4")
        set_remote_target(target)
        assert get_remote_target() is target

    def test_clear(self) -> None:
        set_remote_target(RemoteTarget(host="1.2.3.4"))
        set_remote_target(None)
        assert get_remote_target() is None


# ---------------------------------------------------------------------------
# _shell_quote
# ---------------------------------------------------------------------------


class TestShellQuote:
    def test_simple_string(self) -> None:
        assert _shell_quote("hello") == "'hello'"

    def test_string_with_spaces(self) -> None:
        assert _shell_quote("hello world") == "'hello world'"

    def test_string_with_single_quotes(self) -> None:
        result = _shell_quote("it's")
        assert result == "'it'\\''s'"

    def test_empty_string(self) -> None:
        assert _shell_quote("") == "''"


# ---------------------------------------------------------------------------
# run_local — real subprocess calls
# ---------------------------------------------------------------------------


class TestRunLocal:
    def test_echo_command(self) -> None:
        result = run_local("echo hello", stream=False)
        assert result.ok
        assert "hello" in result.stdout

    def test_false_command_fails(self) -> None:
        result = run_local("false", stream=False)
        assert not result.ok
        assert result.returncode != 0

    def test_list_command(self) -> None:
        result = run_local(["echo", "test"], stream=False)
        assert result.ok
        assert "test" in result.stdout

    def test_check_raises_on_failure(self) -> None:
        with pytest.raises(RuntimeError, match="Command failed"):
            run_local("false", stream=False, check=True)

    def test_duration_recorded(self) -> None:
        result = run_local("echo fast", stream=False)
        assert result.duration >= 0.0

    def test_stdout_captures_multiline(self) -> None:
        result = run_local("echo line1 && echo line2", stream=False)
        assert "line1" in result.stdout
        assert "line2" in result.stdout

    def test_input_text(self) -> None:
        result = run_local(
            ["python3", "-c", "import sys; print(sys.stdin.read())"],
            stream=False,
            input_text="hello from stdin",
        )
        assert "hello from stdin" in result.stdout


# ---------------------------------------------------------------------------
# run_host — SSH routing
# ---------------------------------------------------------------------------


class TestRunHostRouting:
    def setup_method(self) -> None:
        set_remote_target(None)

    def teardown_method(self) -> None:
        set_remote_target(None)

    def test_runs_locally_without_target(self) -> None:
        result = run_host("echo local", stream=False)
        assert result.ok
        assert "local" in result.stdout

    def test_routes_through_ssh_with_target(self) -> None:
        """Verify that run_host calls _run_via_ssh when a target is set.

        We mock _run_subprocess to avoid actually connecting via SSH.
        """
        target = RemoteTarget(host="10.0.0.1", user="ubuntu")
        set_remote_target(target)

        mock_result = CommandResult(
            returncode=0, stdout="remote ok", stderr="", duration=0.1
        )
        with patch(
            "sunbeam_deployer.executor._run_subprocess",
            return_value=mock_result,
        ) as mock_sub:
            result = run_host("echo hello", stream=False)

        assert result.ok
        # The command passed to _run_subprocess should include ssh args
        call_args = mock_sub.call_args
        cmd = call_args[0][0]  # positional arg 0
        assert cmd[0] == "ssh"
        assert "ubuntu@10.0.0.1" in cmd
        assert "echo hello" in cmd
