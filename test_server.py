import asyncio
import json
import os
import shutil
import socket
import tempfile
import uuid

import pytest
import pytest_asyncio

from julia_daemon.server import (
    start_julia_session,
    execute_code,
    kill_session,
    get_or_create_session,
    restart_session,
    list_sessions,
    shutdown_all,
    sessions,
    session_locks,
    SOCKET_PATH,
    handle_client,
    PKG_PATTERN,
)


def make_sentinel():
    return f"__JULIA_DAEMON_{uuid.uuid4().hex}__"


@pytest_asyncio.fixture
async def session():
    tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
    s = await start_julia_session(tmpdir, ("--startup-file=no", "--threads=auto"))
    yield s
    await kill_session(s)


@pytest_asyncio.fixture
async def manager():
    yield
    await shutdown_all()


class TestJuliaSession:
    async def test_basic_eval(self, session):
        result = await execute_code(session, "1 + 1", timeout=30.0)
        assert result == "2"

    async def test_variable_persistence(self, session):
        await execute_code(session, "x = 42", timeout=30.0)
        result = await execute_code(session, "x + 1", timeout=30.0)
        assert result == "43"

    async def test_string_result(self, session):
        result = await execute_code(session, '"hello world"', timeout=30.0)
        assert "hello world" in result

    async def test_multiline(self, session):
        code = "function foo(x)\n    x * 2\nend\nfoo(21)"
        result = await execute_code(session, code, timeout=30.0)
        assert "42" in result

    async def test_multi_expression(self, session):
        result = await execute_code(session, "a = 1\nb = 2\na + b", timeout=30.0)
        assert result.strip() == "3"

    async def test_println_still_works(self, session):
        result = await execute_code(session, "print(7)", timeout=30.0)
        assert result == "7"

    async def test_using_import(self, session):
        result = await execute_code(session, "using Statistics\nmean([1, 2, 3])", timeout=30.0)
        assert result == "2.0"

    async def test_macro_after_import(self, session):
        code = "using Test\n@test 1 == 1\n\"ok\""
        result = await execute_code(session, code, timeout=60.0)
        assert "ok" in result

    async def test_error_handling(self, session):
        result = await execute_code(session, 'error("boom")', timeout=30.0)
        assert "boom" in result
        assert "ERROR" in result or "error" in result.lower()

    async def test_error_does_not_kill_session(self, session):
        await execute_code(session, 'error("boom")', timeout=30.0)
        result = await execute_code(session, "1 + 1", timeout=30.0)
        assert result == "2"

    async def test_nothing_result(self, session):
        result = await execute_code(session, 'nothing', timeout=30.0)
        assert result == ""

    async def test_large_output(self, session):
        result = await execute_code(session, "collect(1:100)", timeout=30.0)
        assert "1" in result
        assert "100" in result

    async def test_huge_single_line(self, session):
        n = 1_000_000
        result = await execute_code(session, f'"a"^{n}', timeout=30.0)
        assert len(result) >= n

    async def test_huge_single_line_then_normal(self, session):
        n = 1_000_000
        await execute_code(session, f'"a"^{n}', timeout=30.0)
        result = await execute_code(session, "1 + 1", timeout=30.0)
        assert result == "2"

    async def test_huge_single_line_then_restart(self, manager):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        try:
            s = await start_julia_session(tmpdir, ("--startup-file=no", "--threads=auto"))
            n = 1_000_000
            await execute_code(s, f'"a"^{n}', timeout=30.0)
            await kill_session(s)
            s = await start_julia_session(tmpdir, ("--startup-file=no", "--threads=auto"))
            result = await execute_code(s, "1 + 1", timeout=30.0)
            assert result == "2"
            await kill_session(s)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_timeout_kills_session(self, session):
        with pytest.raises(RuntimeError, match="timed out"):
            await execute_code(session, "sleep(10)", timeout=0.5)
        assert session["process"].returncode is not None

    async def test_is_alive(self, session):
        assert session["process"].returncode is None

    async def test_kill(self, manager):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        try:
            s = await start_julia_session(tmpdir, ("--startup-file=no", "--threads=auto"))
            assert s["process"].returncode is None
            await kill_session(s)
            assert s["process"].returncode is not None
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_non_temp_dir_not_cleaned(self, manager):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        try:
            s = await start_julia_session(tmpdir, ("--startup-file=no", "--threads=auto"))
            await kill_session(s)
            assert os.path.isdir(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_execute_on_dead_session_raises(self, manager):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        try:
            s = await start_julia_session(tmpdir, ("--startup-file=no", "--threads=auto"))
            s["process"].kill()
            await s["process"].wait()
            with pytest.raises(RuntimeError, match="died"):
                await execute_code(s, "1", timeout=30.0)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_revise_picks_up_changes(self, manager):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        try:
            src_dir = os.path.join(tmpdir, "src")
            os.makedirs(src_dir)
            module_file = os.path.join(src_dir, "MyMod.jl")
            with open(module_file, "w") as f:
                f.write("module MyMod\nfoo() = 1\nend\n")

            project_toml = os.path.join(tmpdir, "Project.toml")
            with open(project_toml, "w") as f:
                f.write('name = "MyMod"\nuuid = "12345678-1234-1234-1234-123456789abc"\n')

            s = await start_julia_session(tmpdir, ("--startup-file=no", "--threads=auto"))
            await execute_code(s, "using MyMod", timeout=30.0)
            result = await execute_code(s, "MyMod.foo()", timeout=30.0)
            assert result == "1"

            with open(module_file, "w") as f:
                f.write("module MyMod\nfoo() = 2\nend\n")

            await asyncio.sleep(0.5)
            result = await execute_code(s, "Revise.revise(); MyMod.foo()", timeout=30.0)
            assert result == "2"

            await kill_session(s)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestSessionManager:
    async def test_lazy_creation(self, manager):
        assert len(sessions) == 0
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        s = await get_or_create_session(tmpdir, ("--startup-file=no", "--threads=auto"))
        assert len(sessions) == 1
        shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_reuse_session(self, manager):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        s1 = await get_or_create_session(tmpdir, ("--startup-file=no", "--threads=auto"))
        s2 = await get_or_create_session(tmpdir, ("--startup-file=no", "--threads=auto"))
        assert s1 is s2
        shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_separate_envs(self, manager):
        tmpdir1 = tempfile.mkdtemp(prefix="julia-daemon-test1-")
        tmpdir2 = tempfile.mkdtemp(prefix="julia-daemon-test2-")
        try:
            s1 = await get_or_create_session(tmpdir1, ("--startup-file=no", "--threads=auto"))
            s2 = await get_or_create_session(tmpdir2, ("--startup-file=no", "--threads=auto"))
            assert s1 is not s2

            await execute_code(s1, "x = 1", timeout=30.0)
            await execute_code(s2, "x = 2", timeout=30.0)

            r1 = await execute_code(s1, "x", timeout=30.0)
            r2 = await execute_code(s2, "x", timeout=30.0)

            assert r1 == "1"
            assert r2 == "2"
        finally:
            shutil.rmtree(tmpdir1, ignore_errors=True)
            shutil.rmtree(tmpdir2, ignore_errors=True)

    async def test_restart(self, manager):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        try:
            s1 = await get_or_create_session(tmpdir, ("--startup-file=no", "--threads=auto"))
            await execute_code(s1, "x = 42", timeout=30.0)
            await restart_session(tmpdir)
            s2 = await get_or_create_session(tmpdir, ("--startup-file=no", "--threads=auto"))
            assert s1 is not s2
            result = await execute_code(s2, "try; x; catch e; string(e); end", timeout=30.0)
            assert "UndefVarError" in result
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_list_sessions(self, manager):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        await get_or_create_session(tmpdir, ("--startup-file=no", "--threads=auto"))
        session_list = list_sessions()
        assert len(session_list) == 1
        shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_list_sessions_contains_env_path(self, manager):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        try:
            await get_or_create_session(tmpdir, ("--startup-file=no", "--threads=auto"))
            session_list = list_sessions()
            assert len(session_list) == 1
            found = any(tmpdir in s["env_path"] for s in session_list)
            assert found
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_list_sessions_test_dir_shows_test_path(self, manager):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        try:
            test_dir = os.path.join(tmpdir, "test")
            os.makedirs(test_dir)
            await get_or_create_session(test_dir, ("--startup-file=no", "--threads=auto"))
            session_list = list_sessions()
            assert len(session_list) == 1
            found = any(test_dir in s["env_path"] for s in session_list)
            assert found
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_dead_session_auto_recreated(self, manager):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        try:
            s1 = await get_or_create_session(tmpdir, ("--startup-file=no", "--threads=auto"))
            s1["process"].kill()
            await s1["process"].wait()
            s2 = await get_or_create_session(tmpdir, ("--startup-file=no", "--threads=auto"))
            assert s1 is not s2
            assert s2["process"].returncode is None
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_test_dir_uses_parent_project(self, manager):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        try:
            test_dir = os.path.join(tmpdir, "test")
            os.makedirs(test_dir)
            with open(os.path.join(tmpdir, "Project.toml"), "w") as f:
                f.write('name = "TestProject"\n')
            s = await get_or_create_session(test_dir, ("--startup-file=no", "--threads=auto"))
            result = await execute_code(s, 'Base.active_project()', timeout=30.0)
            assert tmpdir in result
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_test_dir_separate_from_parent(self, manager):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        try:
            test_dir = os.path.join(tmpdir, "test")
            os.makedirs(test_dir)
            s1 = await get_or_create_session(tmpdir, ("--startup-file=no", "--threads=auto"))
            s2 = await get_or_create_session(test_dir, ("--startup-file=no", "--threads=auto"))
            assert s1 is not s2
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_shutdown_cleans_all(self, manager):
        tmpdir1 = tempfile.mkdtemp(prefix="julia-daemon-test1-")
        tmpdir2 = tempfile.mkdtemp(prefix="julia-daemon-test2-")
        try:
            await get_or_create_session(tmpdir1, ("--startup-file=no", "--threads=auto"))
            await get_or_create_session(tmpdir2, ("--startup-file=no", "--threads=auto"))
            assert len(sessions) == 2
            await shutdown_all()
            assert len(sessions) == 0
        finally:
            shutil.rmtree(tmpdir1, ignore_errors=True)
            shutil.rmtree(tmpdir2, ignore_errors=True)

    async def test_default_julia_args_threads(self, manager):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        try:
            s = await get_or_create_session(tmpdir, ("--startup-file=no", "--threads=auto"))
            result = await execute_code(s, "Threads.nthreads()", timeout=30.0)
            assert int(result) >= 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    async def test_custom_julia_args_threads(self, manager):
        tmpdir = tempfile.mkdtemp(prefix="julia-daemon-test-")
        try:
            s = await get_or_create_session(tmpdir, ("--startup-file=no", "--threads=1"))
            result = await execute_code(s, "Threads.nthreads()", timeout=30.0)
            assert result == "1"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestTimeoutDetection:
    def test_pkg_pattern_matches(self):
        assert PKG_PATTERN.search("Pkg.add(\"Foo\")")
        assert PKG_PATTERN.search("using Pkg; Pkg.add(\"Foo\")")
        assert PKG_PATTERN.search("Pkg.activate()")
        assert PKG_PATTERN.search('code = "Pkg.test()"')
        assert PKG_PATTERN.search("a = 1\nPkg.status()")

    def test_pkg_pattern_no_match(self):
        assert not PKG_PATTERN.search("1 + 1")
        assert not PKG_PATTERN.search("using Statistics")
        assert not PKG_PATTERN.search("pkg = 1")
        assert not PKG_PATTERN.search("mypkg = load()")
        assert not PKG_PATTERN.search("import Pkg")


@pytest_asyncio.fixture
async def daemon_manager():
    sessions.clear()
    session_locks.clear()
    yield
    await shutdown_all()


class TestClientDaemonProtocol:
    async def test_eval_basic(self, daemon_manager):
        reader = asyncio.StreamReader()
        writer = MockStreamWriter()

        request = {"command": "eval", "code": "1 + 1", "env_path": tempfile.mkdtemp()}
        reader.feed_data(json.dumps(request).encode())
        reader.feed_eof()

        shutdown_event = asyncio.Event()
        await handle_client(reader, writer, ("--startup-file=no", "--threads=auto"), shutdown_event)

        response = json.loads(writer.data.decode())
        assert response["status"] == "ok"
        assert "2" in response["output"]

    async def test_eval_persistence(self, daemon_manager):
        tmpdir = tempfile.mkdtemp()

        reader1 = asyncio.StreamReader()
        writer1 = MockStreamWriter()
        request1 = {"command": "eval", "code": "x = 42", "env_path": tmpdir}
        reader1.feed_data(json.dumps(request1).encode())
        reader1.feed_eof()
        shutdown_event = asyncio.Event()
        await handle_client(reader1, writer1, ("--startup-file=no", "--threads=auto"), shutdown_event)

        reader2 = asyncio.StreamReader()
        writer2 = MockStreamWriter()
        request2 = {"command": "eval", "code": "x", "env_path": tmpdir}
        reader2.feed_data(json.dumps(request2).encode())
        reader2.feed_eof()
        await handle_client(reader2, writer2, ("--startup-file=no", "--threads=auto"), shutdown_event)

        response = json.loads(writer2.data.decode())
        assert response["status"] == "ok"
        assert "42" in response["output"]

    async def test_list_empty(self, daemon_manager):
        reader = asyncio.StreamReader()
        writer = MockStreamWriter()

        request = {"command": "list"}
        reader.feed_data(json.dumps(request).encode())
        reader.feed_eof()

        shutdown_event = asyncio.Event()
        await handle_client(reader, writer, ("--startup-file=no", "--threads=auto"), shutdown_event)

        response = json.loads(writer.data.decode())
        assert response["status"] == "ok"
        assert response["sessions"] == []

    async def test_list_after_eval(self, daemon_manager):
        tmpdir = tempfile.mkdtemp()

        reader1 = asyncio.StreamReader()
        writer1 = MockStreamWriter()
        request1 = {"command": "eval", "code": "x = 1", "env_path": tmpdir}
        reader1.feed_data(json.dumps(request1).encode())
        reader1.feed_eof()
        shutdown_event = asyncio.Event()
        await handle_client(reader1, writer1, ("--startup-file=no", "--threads=auto"), shutdown_event)

        reader2 = asyncio.StreamReader()
        writer2 = MockStreamWriter()
        request2 = {"command": "list"}
        reader2.feed_data(json.dumps(request2).encode())
        reader2.feed_eof()
        await handle_client(reader2, writer2, ("--startup-file=no", "--threads=auto"), shutdown_event)

        response = json.loads(writer2.data.decode())
        assert response["status"] == "ok"
        assert len(response["sessions"]) == 1

    async def test_infiltrator_interrupt(self, daemon_manager):
        """Test Infiltrator.jl interaction: @infiltrate, inspect variables, then Ctrl-D to exit.

        This test simulates a typical debugging workflow:
        1. Define a function with @infiltrate
        2. Call the function (which pauses at @infiltrate)
        3. Inspect local variables in the paused frame
        4. Send Ctrl-D to exit the Infiltrator context
        5. Verify the session continues to work normally
        """
        tmpdir = tempfile.mkdtemp()
        shutdown_event = asyncio.Event()

        # Define a function with @infiltrate
        reader1 = asyncio.StreamReader()
        writer1 = MockStreamWriter()
        code1 = """
        function test_func(x)
            y = x * 2
            @infiltrate
            return y + 1
        end
        """
        request1 = {"command": "eval", "code": code1, "env_path": tmpdir}
        reader1.feed_data(json.dumps(request1).encode())
        reader1.feed_eof()
        await handle_client(reader1, writer1, ("--startup-file=no", "--threads=auto"), shutdown_event)
        response1 = json.loads(writer1.data.decode())
        assert response1["status"] == "ok"

        # Start calling the function (will block at @infiltrate) - run concurrently
        reader2 = asyncio.StreamReader()
        writer2 = MockStreamWriter()
        request2 = {"command": "eval", "code": "test_func(5)", "env_path": tmpdir, "timeout": 10}
        reader2.feed_data(json.dumps(request2).encode())
        reader2.feed_eof()
        eval_task = asyncio.create_task(
            handle_client(reader2, writer2, ("--startup-file=no", "--threads=auto"), shutdown_event)
        )

        # Wait a bit for @infiltrate to be hit
        await asyncio.sleep(1.0)

        # Get the session and inspect variables by writing directly to stdin
        # This simulates what a user would do interactively in Infiltrator mode
        session = sessions[tmpdir]

        # Inspect variable 'y' (should be 10 since x=5, y=x*2)
        session["process"].stdin.write(b"y\n")
        await session["process"].stdin.drain()
        await asyncio.sleep(0.3)

        # Inspect variable 'x' (should be 5)
        session["process"].stdin.write(b"x\n")
        await session["process"].stdin.drain()
        await asyncio.sleep(0.3)

        # Send interrupt (Ctrl-D) to exit Infiltrator
        reader3 = asyncio.StreamReader()
        writer3 = MockStreamWriter()
        request3 = {"command": "interrupt", "env_path": tmpdir}
        reader3.feed_data(json.dumps(request3).encode())
        reader3.feed_eof()
        await handle_client(reader3, writer3, ("--startup-file=no", "--threads=auto"), shutdown_event)
        response3 = json.loads(writer3.data.decode())
        assert response3["status"] == "ok"
        assert "Ctrl-D" in response3["output"]

        # The eval should complete (returning nothing since @infiltrate exits the function)
        await eval_task
        response2 = json.loads(writer2.data.decode())
        # The function exits when we Ctrl-D from @infiltrate, so we get no output or error
        assert response2["status"] in ["ok", "error"]

        # Verify session still works after interrupt
        reader4 = asyncio.StreamReader()
        writer4 = MockStreamWriter()
        request4 = {"command": "eval", "code": "2 + 2", "env_path": tmpdir}
        reader4.feed_data(json.dumps(request4).encode())
        reader4.feed_eof()
        await handle_client(reader4, writer4, ("--startup-file=no", "--threads=auto"), shutdown_event)
        response4 = json.loads(writer4.data.decode())
        assert response4["status"] == "ok"
        assert "4" in response4["output"]


class MockStreamWriter:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass
