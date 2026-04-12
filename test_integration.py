"""Integration tests that start real server and client processes.

Unlike the unit tests (test_eval.py and test_server.py) which mock the client
and server respectively, these tests actually spawn the julia-daemon server
process and run the julia-eval client commands against it to verify end-to-end
functionality.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import pytest

SOCKET_PATH = Path(tempfile.gettempdir()) / "julia-daemon.sock"

@pytest.fixture
async def server_process():
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()

    proc = subprocess.Popen(
        [sys.executable, "-m", "julia_daemon.server"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    for _ in range(50):
        if SOCKET_PATH.exists():
            break
        await asyncio.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("Server did not start within timeout")

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()

def run_eval(*args):
    cmd = [sys.executable, "-m", "julia_daemon.eval"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result

@pytest.mark.asyncio
async def test_basic_eval(server_process):
    result = run_eval("1 + 1")
    assert result.returncode == 0
    assert "2" in result.stdout

@pytest.mark.asyncio
async def test_variable_persistence(server_process):
    result1 = run_eval("x = 42")
    assert result1.returncode == 0

    result2 = run_eval("x")
    assert result2.returncode == 0
    assert "42" in result2.stdout

@pytest.mark.asyncio
async def test_error_handling(server_process):
    result = run_eval("error(\"test error\")")
    assert result.returncode == 0
    assert "test error" in result.stdout

@pytest.mark.asyncio
async def test_list_sessions(server_process):
    run_eval("1 + 1")

    result = run_eval("--list")
    assert result.returncode == 0
    assert "Active Julia sessions" in result.stdout

@pytest.mark.asyncio
async def test_restart(server_process):
    with tempfile.TemporaryDirectory() as tmpdir:
        run_eval("--env-path", tmpdir, "y = 100")
        result1 = run_eval("--env-path", tmpdir, "y")
        assert "100" in result1.stdout

        restart_result = run_eval("--env-path", tmpdir, "--restart")
        assert restart_result.returncode == 0

        result2 = run_eval("--env-path", tmpdir, "y")
        assert result2.returncode == 0
        assert "UndefVarError" in result2.stdout or "not defined" in result2.stdout

@pytest.mark.asyncio
async def test_shutdown(server_process):
    result = run_eval("--shutdown")
    assert result.returncode == 0

    time.sleep(0.5)
    assert server_process.poll() is not None

@pytest.mark.asyncio
async def test_stdin_eval(server_process):
    code = "z = 5\nprintln(z)"
    result = subprocess.run(
        [sys.executable, "-m", "julia_daemon.eval"],
        input=code,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "5" in result.stdout

@pytest.mark.asyncio
async def test_multiple_sessions(server_process):
    with tempfile.TemporaryDirectory() as env1, tempfile.TemporaryDirectory() as env2:
        result1 = run_eval("--env-path", env1, "a = 1")
        assert result1.returncode == 0

        result2 = run_eval("--env-path", env2, "a = 2")
        assert result2.returncode == 0

        check1 = run_eval("--env-path", env1, "a")
        assert "1" in check1.stdout

        check2 = run_eval("--env-path", env2, "a")
        assert "2" in check2.stdout

@pytest.mark.asyncio
async def test_timeout(server_process):
    result = run_eval("--timeout", "0.1", "sleep(10)")
    assert result.returncode == 1
    assert "timed out" in result.stderr.lower()

@pytest.mark.asyncio
async def test_revise_picks_up_changes(server_process):
    with tempfile.TemporaryDirectory() as tmpdir:
        src_dir = os.path.join(tmpdir, "src")
        os.makedirs(src_dir)
        module_file = os.path.join(src_dir, "MyMod.jl")
        with open(module_file, "w") as f:
            f.write("module MyMod\nfoo() = 1\nend\n")

        with open(os.path.join(tmpdir, "Project.toml"), "w") as f:
            f.write('name = "MyMod"\nuuid = "12345678-1234-1234-1234-123456789abc"\n')

        result = run_eval("--env-path", tmpdir, "using MyMod")
        assert result.returncode == 0

        result = run_eval("--env-path", tmpdir, "MyMod.foo()")
        assert result.returncode == 0
        assert "1" in result.stdout

        with open(module_file, "w") as f:
            f.write("module MyMod\nfoo() = 2\nend\n")

        time.sleep(0.5)

        result = run_eval("--env-path", tmpdir, "MyMod.foo()")
        assert result.returncode == 0
        assert "2" in result.stdout
