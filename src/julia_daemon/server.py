#!/usr/bin/env python3

import asyncio
import atexit
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from io import TextIOWrapper
from pathlib import Path

DEFAULT_TIMEOUT = 60.0
DEFAULT_JULIA_ARGS = ("--startup-file=no", "--threads=auto")
PKG_PATTERN = re.compile(r"\bPkg\.")
TEMP_SESSION_KEY = "__temp__"
SOCKET_PATH = Path(tempfile.gettempdir()) / "julia-daemon.sock"


class JuliaSession:
    def __init__(
        self,
        env_dir: str,
        sentinel: str,
        *,
        is_temp: bool = False,
        is_test: bool = False,
        julia_args: tuple[str, ...] = DEFAULT_JULIA_ARGS,
        log_file: TextIOWrapper | None = None,
    ):
        self.env_dir = env_dir
        self.sentinel = sentinel
        self.is_temp = is_temp
        self.is_test = is_test
        self.julia_args = julia_args
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self._log_file = log_file

    @property
    def project_path(self) -> str:
        if self.is_test:
            return str(Path(self.env_dir).parent)
        return self.env_dir

    @property
    def init_code(self) -> str | None:
        if self.is_test:
            return "using TestEnv; TestEnv.activate()"
        return None

    async def start(self) -> None:
        julia = shutil.which("julia")
        if julia is None:
            raise RuntimeError(
                "Julia not found in PATH. Install from https://julialang.org/downloads/"
            )

        cmd = [
            julia,
            "-i",
            *self.julia_args,
            f"--project={self.project_path}",
        ]

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.env_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            limit=64 * 1024 * 1024,
        )

        await self._execute_raw("", timeout=120.0)
        await self._execute_raw("try; using Revise; catch; end", timeout=120.0)

        if self.init_code:
            await self._execute_raw(self.init_code, timeout=None)

    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def execute(self, code: str, timeout: float | None) -> str:
        async with self.lock:
            if not self.is_alive():
                raise RuntimeError("Julia session has died unexpectedly")
            hex_encoded = code.encode().hex()
            wrapped = (
                f'try; Revise.revise(); catch; end;'
                f'include_string(Main, String(hex2bytes("{hex_encoded}")));'
                f'nothing'
            )
            if self._log_file:
                ts = time.strftime("%H:%M:%S")
                self._log_file.write(f"[{ts}] julia> {code}\n")
                self._log_file.flush()
            output = await self._execute_raw(wrapped, timeout)
            if self._log_file and output:
                self._log_file.write(f"{output}\n\n")
                self._log_file.flush()
            return output

    async def _execute_raw(self, code: str, timeout: float | None) -> str:
        assert self.process is not None
        assert self.process.stdin is not None

        sentinel_cmd = (
            f'flush(stderr); write(stdout, "\\n"); println(stdout, "{self.sentinel}"); flush(stdout)'
        )
        payload = code + "\n" + sentinel_cmd + "\n"
        self.process.stdin.write(payload.encode())
        await self.process.stdin.drain()

        lines: list[str] = []

        async def read_until_sentinel() -> str:
            while True:
                raw = await self.process.stdout.readline()
                if not raw:
                    collected = "\n".join(lines)
                    raise RuntimeError(
                        f"Julia process died during execution.\n"
                        f"Output before death:\n{collected}"
                    )
                line = raw.decode().rstrip("\n").rstrip("\r")
                if line == self.sentinel:
                    break
                lines.append(line)
            if lines and lines[-1] == "":
                lines.pop()
            return "\n".join(lines)

        if timeout is not None:
            try:
                return await asyncio.wait_for(read_until_sentinel(), timeout=timeout)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
                partial = "\n".join(lines)
                msg = f"Execution timed out after {timeout}s. Session killed; it will restart on next call."
                if partial:
                    msg += f"\n\nOutput before timeout:\n{partial}"
                raise RuntimeError(msg)
        else:
            return await read_until_sentinel()

    async def kill(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        if self.is_temp and os.path.isdir(self.env_dir):
            shutil.rmtree(self.env_dir, ignore_errors=True)


class SessionManager:
    def __init__(self, julia_args: tuple[str, ...] = DEFAULT_JULIA_ARGS):
        self.julia_args = julia_args
        self._sessions: dict[str, JuliaSession] = {}
        self._create_locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._log_dir = tempfile.mkdtemp(prefix="julia-daemon-logs-")
        self._log_files: dict[str, TextIOWrapper] = {}
        atexit.register(self._cleanup_logs)

    def _get_log_file(self, key: str) -> TextIOWrapper:
        if key not in self._log_files:
            safe_name = key.replace("/", "_").replace("\\", "_").strip("_") or "temp"
            path = os.path.join(self._log_dir, f"{safe_name}.log")
            self._log_files[key] = open(path, "a")
        return self._log_files[key]

    def _cleanup_logs(self) -> None:
        for f in self._log_files.values():
            try:
                f.close()
            except Exception:
                pass
        shutil.rmtree(self._log_dir, ignore_errors=True)

    def _key(self, env_path: str | None) -> str:
        if env_path is None:
            return TEMP_SESSION_KEY
        return str(Path(env_path).resolve())

    async def get_or_create(self, env_path: str | None) -> JuliaSession:
        key = self._key(env_path)

        if key in self._sessions and self._sessions[key].is_alive():
            return self._sessions[key]

        async with self._global_lock:
            if key not in self._create_locks:
                self._create_locks[key] = asyncio.Lock()
            create_lock = self._create_locks[key]

        async with create_lock:
            if key in self._sessions and self._sessions[key].is_alive():
                return self._sessions[key]

            if key in self._sessions:
                await self._sessions[key].kill()
                del self._sessions[key]

            sentinel = f"__JULIA_DAEMON_{uuid.uuid4().hex}__"
            is_temp = env_path is None
            if is_temp:
                env_dir = tempfile.mkdtemp(prefix="julia-daemon-")
                is_test = False
            else:
                resolved = Path(env_path).resolve()
                env_dir = str(resolved)
                is_test = resolved.name == "test"

            session = JuliaSession(
                env_dir, sentinel, is_temp=is_temp, is_test=is_test,
                julia_args=self.julia_args,
                log_file=self._get_log_file(key),
            )
            await session.start()
            self._sessions[key] = session
            return session

    async def restart(self, env_path: str | None) -> None:
        key = self._key(env_path)
        if key in self._sessions:
            await self._sessions[key].kill()
            del self._sessions[key]

    def list_sessions(self) -> list[dict]:
        result = []
        for key, session in self._sessions.items():
            info = {
                "env_path": session.env_dir,
                "alive": session.is_alive(),
                "temp": session.is_temp,
            }
            if key in self._log_files:
                info["log_file"] = self._log_files[key].name
            result.append(info)
        return result

    async def shutdown(self) -> None:
        for session in self._sessions.values():
            await session.kill()
        self._sessions.clear()
        self._cleanup_logs()


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, manager: SessionManager, shutdown_event: asyncio.Event = None):
    try:
        data = await reader.read(100 * 1024 * 1024)
        request = json.loads(data.decode())

        command = request.get("command")
        response = {}

        if command == "eval":
            code = request["code"]
            env_path = request.get("env_path")
            timeout = request.get("timeout")

            if timeout is None:
                effective_timeout = None if PKG_PATTERN.search(code) else DEFAULT_TIMEOUT
            else:
                effective_timeout = timeout if timeout > 0 else None

            try:
                session = await manager.get_or_create(env_path)
                output = await session.execute(code, timeout=effective_timeout)
                response = {"status": "ok", "output": output if output else "(no output)"}
            except RuntimeError as e:
                key = manager._key(env_path)
                if key in manager._sessions and not manager._sessions[key].is_alive():
                    del manager._sessions[key]
                response = {"status": "error", "output": str(e)}

        elif command == "restart":
            env_path = request.get("env_path")
            await manager.restart(env_path)
            response = {"status": "ok", "output": "Session restarted. A fresh session will start on next eval."}

        elif command == "list":
            sessions = manager.list_sessions()
            response = {"status": "ok", "sessions": sessions}

        elif command == "shutdown":
            response = {"status": "ok", "output": "Daemon shutting down"}
            writer.write(json.dumps(response).encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            if shutdown_event:
                shutdown_event.set()
            return

        else:
            response = {"status": "error", "output": f"Unknown command: {command}"}

        writer.write(json.dumps(response).encode())
        await writer.drain()
    except Exception as e:
        error_response = {"status": "error", "output": str(e)}
        writer.write(json.dumps(error_response).encode())
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def async_main():
    julia_args = tuple(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT_JULIA_ARGS
    manager = SessionManager(julia_args=julia_args)
    shutdown_event = asyncio.Event()

    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()

    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, manager, shutdown_event),
        str(SOCKET_PATH)
    )

    print(f"Julia daemon started", file=sys.stderr)
    print(f"Socket: {SOCKET_PATH}", file=sys.stderr)
    print(f"Logs: {manager._log_dir}", file=sys.stderr)

    def handle_signal():
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    async with server:
        serve_task = asyncio.create_task(server.serve_forever())
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        done, pending = await asyncio.wait(
            [serve_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED
        )

        for task in pending:
            task.cancel()

        server.close()
        await server.wait_closed()
        await manager.shutdown()

def main():
    asyncio.run(async_main())

if __name__ == "__main__":
    main()
