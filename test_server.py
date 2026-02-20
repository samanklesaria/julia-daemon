import asyncio
import json
import os
import shutil
import socket
import tempfile
import uuid

import pytest
import pytest_asyncio

from julia_server import JuliaSession, SessionManager, TEMP_SESSION_KEY, SOCKET_PATH, handle_client


# -- Helpers --


def make_sentinel() -> str:
    return f"__JULIA_DAEMON_{uuid.uuid4().hex}__"


@pytest_asyncio.fixture
async def session():
    tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
    s = JuliaSession(tmpdir, make_sentinel(), is_temp=True)
    await s.start()
    yield s
    await s.kill()


@pytest_asyncio.fixture
async def manager():
    m = SessionManager()
    yield m
    await m.shutdown()


# -- JuliaSession tests --


class TestJuliaSession:
    async def test_basic_eval(self, session: JuliaSession):
        result = await session.execute("println(1 + 1)", timeout=30.0)
        assert result == "2"

    async def test_variable_persistence(self, session: JuliaSession):
        await session.execute("x = 42", timeout=30.0)
        result = await session.execute("println(x + 1)", timeout=30.0)
        assert result == "43"

    async def test_println(self, session: JuliaSession):
        result = await session.execute('println("hello world")', timeout=30.0)
        assert result == "hello world"

    async def test_multiline(self, session: JuliaSession):
        code = "function foo(x)\n    x * 2\nend\nprintln(foo(21))"
        result = await session.execute(code, timeout=30.0)
        assert "42" in result

    async def test_multi_expression(self, session: JuliaSession):
        result = await session.execute("a = 1\nb = 2\nprintln(a + b)", timeout=30.0)
        assert result.strip() == "3"

    async def test_no_auto_display(self, session: JuliaSession):
        result = await session.execute("1 + 2\nprint(7)\n5 + 6", timeout=30.0)
        assert result == "7"

    async def test_using_import(self, session: JuliaSession):
        result = await session.execute(
            "using Statistics\nprintln(mean([1, 2, 3]))", timeout=30.0
        )
        assert result == "2.0"

    async def test_macro_after_import(self, session: JuliaSession):
        code = "using Test\n@test 1 == 1\nprintln(\"ok\")"
        result = await session.execute(code, timeout=60.0)
        assert "ok" in result

    async def test_error_handling(self, session: JuliaSession):
        result = await session.execute('error("boom")', timeout=30.0)
        assert "boom" in result
        assert "ERROR" in result or "error" in result.lower()

    async def test_error_does_not_kill_session(self, session: JuliaSession):
        await session.execute('error("boom")', timeout=30.0)
        result = await session.execute("println(1 + 1)", timeout=30.0)
        assert result == "2"

    async def test_nothing_result(self, session: JuliaSession):
        result = await session.execute('println("hi")', timeout=30.0)
        assert "hi" in result

    async def test_large_output(self, session: JuliaSession):
        result = await session.execute("println(collect(1:100))", timeout=30.0)
        assert "1" in result
        assert "100" in result

    async def test_huge_single_line(self, session: JuliaSession):
        n = 1_000_000
        result = await session.execute(f'print("a"^{n})', timeout=30.0)
        assert len(result) == n
        assert result == "a" * n

    async def test_huge_single_line_then_normal(self, session: JuliaSession):
        n = 1_000_000
        result = await session.execute(f'print("a"^{n})', timeout=30.0)
        assert len(result) == n
        result = await session.execute("println(1 + 1)", timeout=30.0)
        assert result == "2"

    async def test_huge_single_line_then_restart(self, manager: SessionManager):
        s = await manager.get_or_create(None)
        n = 1_000_000
        result = await s.execute(f'print("a"^{n})', timeout=30.0)
        assert len(result) == n
        await manager.restart(None)
        s2 = await manager.get_or_create(None)
        assert s2 is not s
        result = await s2.execute("println(1 + 1)", timeout=30.0)
        assert result == "2"

    async def test_timeout_kills_session(self, session: JuliaSession):
        with pytest.raises(RuntimeError, match="timed out"):
            await session.execute("sleep(60)", timeout=2.0)
        assert not session.is_alive()

    async def test_is_alive(self, session: JuliaSession):
        assert session.is_alive()

    async def test_kill(self):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        s = JuliaSession(tmpdir, make_sentinel(), is_temp=True)
        await s.start()
        assert s.is_alive()
        await s.kill()
        assert not s.is_alive()
        assert not os.path.exists(tmpdir)

    async def test_temp_dir_cleanup(self):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        s = JuliaSession(tmpdir, make_sentinel(), is_temp=True)
        await s.start()
        assert os.path.isdir(tmpdir)
        await s.kill()
        assert not os.path.isdir(tmpdir)

    async def test_non_temp_dir_not_cleaned(self):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        s = JuliaSession(tmpdir, make_sentinel(), is_temp=False)
        await s.start()
        await s.kill()
        assert os.path.isdir(tmpdir)
        os.rmdir(tmpdir)

    async def test_execute_on_dead_session_raises(self, session: JuliaSession):
        session.process.kill()
        await session.process.wait()
        with pytest.raises(RuntimeError, match="died unexpectedly"):
            await session.execute("1 + 1", timeout=30.0)

    async def test_revise_picks_up_changes(self):
        # Create a minimal Julia package in a temp dir
        pkg_dir = tempfile.mkdtemp(prefix="julia-daemon-test-revise-")
        src_dir = os.path.join(pkg_dir, "src")
        os.makedirs(src_dir)

        with open(os.path.join(pkg_dir, "Project.toml"), "w") as f:
            f.write(
                'name = "TestRevPkg"\n'
                'uuid = "12345678-1234-1234-1234-123456789abc"\n'
                'version = "0.1.0"\n'
            )

        src_file = os.path.join(src_dir, "TestRevPkg.jl")
        with open(src_file, "w") as f:
            f.write("module TestRevPkg\ngreet() = \"hello\"\nend\n")

        # Start Julia directly in the package env
        s = JuliaSession(pkg_dir, make_sentinel(), is_temp=True)
        await s.start()
        try:
            await s.execute("using TestRevPkg", timeout=120.0)
            result = await s.execute(
                "println(TestRevPkg.greet())", timeout=60.0
            )
            assert result == "hello"

            # Modify the source file on disk
            with open(src_file, "w") as f:
                f.write("module TestRevPkg\ngreet() = \"goodbye\"\nend\n")

            # Call again — Revise should pick up the change
            result = await s.execute(
                "println(TestRevPkg.greet())", timeout=60.0
            )
            assert result == "goodbye"
        finally:
            await s.kill()


# -- SessionManager tests --


class TestSessionManager:
    async def test_lazy_creation(self, manager: SessionManager):
        assert manager.list_sessions() == []
        session = await manager.get_or_create(None)
        assert session.is_alive()
        assert len(manager.list_sessions()) == 1

    async def test_reuse_session(self, manager: SessionManager):
        s1 = await manager.get_or_create(None)
        s2 = await manager.get_or_create(None)
        assert s1 is s2

    async def test_separate_envs(self, manager: SessionManager):
        tmpdir1 = tempfile.mkdtemp(prefix="julia-daemon-test-")
        tmpdir2 = tempfile.mkdtemp(prefix="julia-daemon-test-")
        try:
            s1 = await manager.get_or_create(tmpdir1)
            s2 = await manager.get_or_create(tmpdir2)
            assert s1 is not s2
            assert len(manager.list_sessions()) == 2

            # Variables are isolated
            await s1.execute("x = 100", timeout=30.0)
            result = await s2.execute(
                "try; x; catch; println(\"undefined\"); end", timeout=30.0
            )
            assert "undefined" in result.lower() or "UndefVarError" in result
        finally:
            await manager.shutdown()
            os.rmdir(tmpdir1)
            os.rmdir(tmpdir2)

    async def test_restart(self, manager: SessionManager):
        s1 = await manager.get_or_create(None)
        await s1.execute("x = 42", timeout=30.0)
        await manager.restart(None)
        assert len(manager.list_sessions()) == 0

        s2 = await manager.get_or_create(None)
        assert s2 is not s1
        result = await s2.execute(
            "try; x; catch e; println(e); end", timeout=30.0
        )
        assert "UndefVarError" in result

    async def test_list_sessions(self, manager: SessionManager):
        await manager.get_or_create(None)
        sessions = manager.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["alive"] is True
        assert sessions[0]["temp"] is True

    async def test_list_sessions_contains_env_path(self, manager: SessionManager):
        tmpdir = os.path.realpath(tempfile.mkdtemp(prefix="julia-daemon-test-"))
        try:
            await manager.get_or_create(tmpdir)
            sessions = manager.list_sessions()
            assert len(sessions) == 1
            assert sessions[0]["env_path"] == tmpdir
            assert sessions[0]["temp"] is False
        finally:
            await manager.shutdown()
            os.rmdir(tmpdir)

    async def test_list_sessions_test_dir_shows_test_path(self, manager: SessionManager):
        tmpdir = os.path.realpath(tempfile.mkdtemp(prefix="julia-daemon-test-"))
        test_dir = os.path.join(tmpdir, "test")
        os.makedirs(test_dir)
        try:
            await manager.get_or_create(test_dir)
            sessions = manager.list_sessions()
            assert len(sessions) == 1
            # Should show the original test dir path, not the parent
            assert sessions[0]["env_path"] == test_dir
        finally:
            await manager.shutdown()
            shutil.rmtree(tmpdir)

    async def test_dead_session_auto_recreated(self, manager: SessionManager):
        s1 = await manager.get_or_create(None)
        s1.process.kill()
        await s1.process.wait()
        s2 = await manager.get_or_create(None)
        assert s2 is not s1
        assert s2.is_alive()

    async def test_test_dir_uses_parent_project(self, manager: SessionManager):
        tmpdir = os.path.realpath(tempfile.mkdtemp(prefix="julia-daemon-test-"))
        test_dir = os.path.join(tmpdir, "test")
        os.makedirs(test_dir)
        try:
            session = await manager.get_or_create(test_dir)
            # project_path should be the parent, not the test dir
            assert session.project_path == tmpdir
            assert session.init_code == "using TestEnv; TestEnv.activate()"
        finally:
            await manager.shutdown()
            shutil.rmtree(tmpdir)

    async def test_test_dir_separate_from_parent(self, manager: SessionManager):
        tmpdir = os.path.realpath(tempfile.mkdtemp(prefix="julia-daemon-test-"))
        test_dir = os.path.join(tmpdir, "test")
        os.makedirs(test_dir)
        try:
            s1 = await manager.get_or_create(tmpdir)
            s2 = await manager.get_or_create(test_dir)
            assert s1 is not s2
        finally:
            await manager.shutdown()
            shutil.rmtree(tmpdir)

    async def test_shutdown_cleans_all(self, manager: SessionManager):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        await manager.get_or_create(None)
        await manager.get_or_create(tmpdir)
        assert len(manager.list_sessions()) == 2
        await manager.shutdown()
        assert len(manager.list_sessions()) == 0
        # Non-temp dir still exists
        assert os.path.isdir(tmpdir)
        os.rmdir(tmpdir)

    async def test_default_julia_args_threads(self):
        m = SessionManager()
        try:
            session = await m.get_or_create(None)
            result = await session.execute("println(Threads.nthreads())", timeout=30.0)
            assert int(result) > 1
        finally:
            await m.shutdown()

    async def test_custom_julia_args_threads(self):
        m = SessionManager(julia_args=("--threads=1",))
        try:
            session = await m.get_or_create(None)
            result = await session.execute("println(Threads.nthreads())", timeout=30.0)
            assert result == "1"
        finally:
            await m.shutdown()


# -- Timeout auto-detection tests --


class TestTimeoutDetection:
    """Test that PKG_PATTERN correctly identifies Pkg/using/import code."""

    from julia_server import PKG_PATTERN

    @pytest.mark.parametrize(
        "code",
        [
            "Pkg.add(\"Example\")",
            "using Pkg; Pkg.status()",
        ],
    )
    def test_pkg_pattern_matches(self, code: str):
        assert self.PKG_PATTERN.search(code)

    @pytest.mark.parametrize(
        "code",
        [
            "1 + 1",
            "x = 42",
            "let x = 1; end",
            "f(x) = x^2",
            "using LinearAlgebra",
            "import Pkg",
        ],
    )
    def test_pkg_pattern_no_match(self, code: str):
        assert not self.PKG_PATTERN.search(code)


# -- End-to-end client-daemon tests --


@pytest_asyncio.fixture
async def daemon_manager():
    """Create a SessionManager for daemon testing."""
    m = SessionManager()
    yield m
    await m.shutdown()


class TestClientDaemonProtocol:
    async def test_eval_basic(self, daemon_manager: SessionManager):
        request = {"command": "eval", "code": "println(1 + 1)"}
        reader = asyncio.StreamReader()
        reader.feed_data(json.dumps(request).encode())
        reader.feed_eof()

        writer = MockStreamWriter()
        await handle_client(reader, writer, daemon_manager)

        response = json.loads(writer.data.decode())
        assert response["status"] == "ok"
        assert response["output"] == "2"

    async def test_eval_persistence(self, daemon_manager: SessionManager):
        request1 = {"command": "eval", "code": "x = 42"}
        reader1 = asyncio.StreamReader()
        reader1.feed_data(json.dumps(request1).encode())
        reader1.feed_eof()
        writer1 = MockStreamWriter()
        await handle_client(reader1, writer1, daemon_manager)

        request2 = {"command": "eval", "code": "println(x)"}
        reader2 = asyncio.StreamReader()
        reader2.feed_data(json.dumps(request2).encode())
        reader2.feed_eof()
        writer2 = MockStreamWriter()
        await handle_client(reader2, writer2, daemon_manager)

        response = json.loads(writer2.data.decode())
        assert response["status"] == "ok"
        assert response["output"] == "42"

    async def test_list_empty(self, daemon_manager: SessionManager):
        request = {"command": "list"}
        reader = asyncio.StreamReader()
        reader.feed_data(json.dumps(request).encode())
        reader.feed_eof()

        writer = MockStreamWriter()
        await handle_client(reader, writer, daemon_manager)

        response = json.loads(writer.data.decode())
        assert response["status"] == "ok"
        assert response["sessions"] == []

    async def test_list_after_eval(self, daemon_manager: SessionManager):
        request1 = {"command": "eval", "code": "x = 1"}
        reader1 = asyncio.StreamReader()
        reader1.feed_data(json.dumps(request1).encode())
        reader1.feed_eof()
        writer1 = MockStreamWriter()
        await handle_client(reader1, writer1, daemon_manager)

        request2 = {"command": "list"}
        reader2 = asyncio.StreamReader()
        reader2.feed_data(json.dumps(request2).encode())
        reader2.feed_eof()
        writer2 = MockStreamWriter()
        await handle_client(reader2, writer2, daemon_manager)

        response = json.loads(writer2.data.decode())
        assert response["status"] == "ok"
        assert len(response["sessions"]) == 1
        assert response["sessions"][0]["temp"] is True
        assert response["sessions"][0]["alive"] is True


class MockStreamWriter:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass
