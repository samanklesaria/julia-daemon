#!/usr/bin/env python3

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

DEFAULT_TIMEOUT = 60.0
DEFAULT_JULIA_ARGS = ("--startup-file=no", "--threads=auto")
PKG_PATTERN = re.compile(r"\bPkg\.")
SOCKET_PATH = Path(tempfile.gettempdir()) / "julia-daemon.sock"

sessions = {}
session_locks = {}

def get_session_key(env_path):
    if env_path is None:
        return "__temp__"
    return str(Path(env_path).resolve())

def is_test_env(env_path):
    if env_path is None:
        return False
    return Path(env_path).resolve().name == "test"

def get_project_path(env_path, is_test):
    if is_test:
        return str(Path(env_path).parent)
    return env_path

def get_init_code(is_test):
    if is_test:
        return "using TestEnv; TestEnv.activate()"
    return None

async def start_julia_session(env_path, julia_args):
    julia = shutil.which("julia")
    if julia is None:
        raise RuntimeError("Julia not found in PATH. Install from https://julialang.org/downloads/")

    is_test = is_test_env(env_path)
    is_temp = env_path is None

    if is_temp:
        env_dir = tempfile.mkdtemp(prefix="julia-daemon-")
        project_path = env_dir
    else:
        env_dir = str(Path(env_path).resolve())
        project_path = get_project_path(env_dir, is_test)

    sentinel = f"__JULIA_DAEMON_{uuid.uuid4().hex}__"

    cmd = [julia, "-i", *julia_args, f"--project={project_path}"]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=env_dir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        limit=64 * 1024 * 1024,
    )

    await execute_raw(process, sentinel, "", 120.0)
    await execute_raw(process, sentinel, "try; using Revise; catch; end", 120.0)

    init = get_init_code(is_test)
    if init:
        await execute_raw(process, sentinel, init, None)

    return {
        "process": process,
        "sentinel": sentinel,
        "env_dir": env_dir,
        "is_temp": is_temp,
        "lock": asyncio.Lock(),
    }

async def execute_raw(process, sentinel, code, timeout):
    sentinel_cmd = f'flush(stderr); write(stdout, "\\n"); println(stdout, "{sentinel}"); flush(stdout)'
    payload = code + "\n" + sentinel_cmd + "\n"
    process.stdin.write(payload.encode())
    await process.stdin.drain()

    lines = []

    async def read_until_sentinel():
        while True:
            raw = await process.stdout.readline()
            if not raw:
                collected = "\n".join(lines)
                raise RuntimeError(f"Julia process died during execution.\nOutput before death:\n{collected}")
            line = raw.decode().rstrip("\n").rstrip("\r")
            if line == sentinel:
                break
            lines.append(line)
        if lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines)

    if timeout is not None:
        try:
            return await asyncio.wait_for(read_until_sentinel(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            partial = "\n".join(lines)
            msg = f"Execution timed out after {timeout}s. Session killed; it will restart on next call."
            if partial:
                msg += f"\n\nOutput before timeout:\n{partial}"
            raise RuntimeError(msg)
    else:
        return await read_until_sentinel()

async def execute_code(session, code, timeout):
    async with session["lock"]:
        if session["process"].returncode is not None:
            raise RuntimeError("Julia session has died unexpectedly")

        hex_encoded = code.encode().hex()
        wrapped = (
            f'try; Revise.revise(); catch; end;'
            f'include_string(Main, String(hex2bytes("{hex_encoded}")));'
            f'nothing'
        )
        return await execute_raw(session["process"], session["sentinel"], wrapped, timeout)

async def kill_session(session):
    if session["process"].returncode is None:
        session["process"].kill()
        await session["process"].wait()
    if session["is_temp"] and os.path.isdir(session["env_dir"]):
        shutil.rmtree(session["env_dir"], ignore_errors=True)

async def get_or_create_session(env_path, julia_args):
    key = get_session_key(env_path)

    if key in sessions and sessions[key]["process"].returncode is None:
        return sessions[key]

    if key not in session_locks:
        session_locks[key] = asyncio.Lock()

    async with session_locks[key]:
        if key in sessions and sessions[key]["process"].returncode is None:
            return sessions[key]

        if key in sessions:
            await kill_session(sessions[key])
            del sessions[key]

        session = await start_julia_session(env_path, julia_args)
        sessions[key] = session
        return session

async def restart_session(env_path):
    key = get_session_key(env_path)
    if key in sessions:
        await kill_session(sessions[key])
        del sessions[key]

def list_sessions():
    result = []
    for key, session in sessions.items():
        result.append({
            "env_path": session["env_dir"],
            "alive": session["process"].returncode is None,
            "temp": session["is_temp"],
        })
    return result

async def shutdown_all():
    for session in sessions.values():
        await kill_session(session)
    sessions.clear()

async def handle_client(reader, writer, julia_args, shutdown_event):
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
                session = await get_or_create_session(env_path, julia_args)
                output = await execute_code(session, code, effective_timeout)
                response = {"status": "ok", "output": output if output else "(no output)"}
            except RuntimeError as e:
                key = get_session_key(env_path)
                if key in sessions and sessions[key]["process"].returncode is not None:
                    del sessions[key]
                response = {"status": "error", "output": str(e)}

        elif command == "restart":
            env_path = request.get("env_path")
            await restart_session(env_path)
            response = {"status": "ok", "output": "Session restarted. A fresh session will start on next eval."}

        elif command == "list":
            session_list = list_sessions()
            response = {"status": "ok", "sessions": session_list}

        elif command == "shutdown":
            response = {"status": "ok", "output": "Daemon shutting down"}
            writer.write(json.dumps(response).encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()
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
    shutdown_event = asyncio.Event()

    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()

    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, julia_args, shutdown_event),
        str(SOCKET_PATH)
    )

    print(f"Julia daemon started", file=sys.stderr)
    print(f"Socket: {SOCKET_PATH}", file=sys.stderr)

    def handle_signal():
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    async with server:
        serve_task = asyncio.create_task(server.serve_forever())
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        await asyncio.wait([serve_task, shutdown_task], return_when=asyncio.FIRST_COMPLETED)

        for task in [serve_task, shutdown_task]:
            task.cancel()

        server.close()
        await server.wait_closed()
        await shutdown_all()

def main():
    asyncio.run(async_main())

if __name__ == "__main__":
    main()
