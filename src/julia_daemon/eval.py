#!/usr/bin/env python3
import argparse
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

SOCKET_PATH = Path(tempfile.gettempdir()) / "julia-daemon.sock"

def send_request(request):
    if not SOCKET_PATH.exists():
        print("Error: Julia daemon not running. Start it with 'server.py'", file=sys.stderr)
        sys.exit(1)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(SOCKET_PATH))
        sock.sendall(json.dumps(request).encode())
        sock.shutdown(socket.SHUT_WR)

        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk

        return json.loads(data.decode())
    finally:
        sock.close()

def main():
    parser = argparse.ArgumentParser(description="Evaluate Julia code in persistent daemon session")
    parser.add_argument("code", nargs="?", help="Julia code to evaluate")
    parser.add_argument("--env-path", help="Julia project directory path")
    parser.add_argument("--timeout", type=float, help="Timeout in seconds (default: 60, 0 for no timeout)")
    parser.add_argument("--restart", action="store_true", help="Restart the session")
    parser.add_argument("--list", action="store_true", help="List active sessions")
    parser.add_argument("--shutdown", action="store_true", help="Shutdown the daemon")

    args = parser.parse_args()

    if args.shutdown:
        response = send_request({"command": "shutdown"})
        print(response.get("output", ""))
        sys.exit(0 if response["status"] == "ok" else 1)

    if args.list:
        response = send_request({"command": "list"})
        if response["status"] == "ok":
            sessions = response.get("sessions", [])
            if not sessions:
                print("No active Julia sessions.")
            else:
                print("Active Julia sessions:")
                for s in sessions:
                    status = "alive" if s["alive"] else "dead"
                    label = s["env_path"]
                    log = f" log={s['log_file']}" if "log_file" in s else ""
                    print(f"  {label}: {status}{log}")
        else:
            print(f"Error: {response.get('output')}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if args.restart:
        request = {"command": "restart"}
        if args.env_path:
            request["env_path"] = args.env_path
        response = send_request(request)
        print(response.get("output", ""))
        sys.exit(0 if response["status"] == "ok" else 1)

    if not args.code:
        code = sys.stdin.read()
    else:
        code = args.code

    request = {"command": "eval", "code": code}
    if args.env_path:
        request["env_path"] = args.env_path
    else:
        request["env_path"] = os.getcwd()
    if args.timeout is not None:
        request["timeout"] = args.timeout

    response = send_request(request)

    if response["status"] == "ok":
        output = response.get("output", "")
        if output:
            print(output)
        sys.exit(0)
    else:
        print(f"Error: {response.get('output')}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
