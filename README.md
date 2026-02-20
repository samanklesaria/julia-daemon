# julia-daemon

This is a fork of [julia-mcp](https://github.com/aplavin/julia-mcp) that turns it into a daemon. See [this](https://mariozechner.at/posts/2025-11-02-what-if-you-dont-need-mcp/) link for why you might want to do that.

More generally, this package gives you a deamon for running Julia code in persistent REPL sessions. This avoids Julia's startup and compilation costs by keeping sessions alive across calls, and persists state (variables, functions, loaded packages) between them — so each iteration is fast.

- Sessions start on demand, persist state between calls, and recover from crashes — no manual management
- Each project directory gets its own isolated Julia process
- Simple client-server architecture using Unix sockets

## Components

- **julia-server** — daemon that manages Julia sessions
- **julia-eval** — client script to execute Julia code

## Requirements

- Python 3.10+
- Julia – any version, `julia` binary must be in `PATH`
  - Recommended packages – used automatically if available in the global environment:
  - [Revise.jl](https://github.com/timholy/Revise.jl) - to pick code changes up without restarting
  - [TestEnv.jl](https://github.com/JuliaTesting/TestEnv.jl) — to properly activate test environment when `env_path` points to `/test/`

## Quick Start

First, you'll need to start the daemon with the `julia-server` command. You could make a systemd service on linux or a launchd service on macOS. 

Once the server is running, you can execute Julia code using the `julia-eval` command.

```bash
# Direct code argument (auto-displays result like REPL)
julia-eval "1 + 1"

# From stdin
echo "1 + 1" | julia-eval

# With project environment
julia-eval --env-path /path/to/project "using MyPackage; foo()"

# With custom timeout
julia-eval --timeout 120 "expensive_computation()"

# Restart a session
julia-eval --restart --env-path /path/to/project

# Send Ctrl-D to Julia process (useful for exiting Infiltrator.jl debugging)
julia-eval --interrupt

# Shutdown daemon
julia-eval --shutdown
```

## Options

- `code` — Julia code to evaluate (or read from stdin if omitted)
- `--env-path PATH` — Julia project directory path (omit to use the current directory)
- `--timeout SECONDS` — Timeout in seconds (default: 60, 0 for no timeout, auto-disabled for Pkg operations)
- `--restart` — Restart the session
- `--interrupt` — Send Ctrl-D to the Julia process (useful for exiting Infiltrator.jl debugging sessions)
- `--list` — List active sessions
- `--shutdown` — Shutdown the daemon

## Details

- Each unique `env_path` gets its own isolated Julia session. 
- If `env_path` ends in `/test/`, the parent directory is used as the project and `TestEnv` is activated automatically. For this to work, `TestEnv` must be installed in the base environment.
- Julia is launched with `--threads=auto` and `--startup-file=no` by default. Pass custom Julia CLI flags after `server.py` to override these defaults entirely.
- The daemon communicates via Unix socket at `/tmp/julia-daemon.sock`
- Session logs are stored in a temporary directory printed at daemon startup

## Examples

Persistent state between calls:

```bash
julia-eval "x = 42"
julia-eval "x"  # displays 42
```

Multiple sessions (different environments are isolated):

```bash
julia-eval --env-path ~/project1 "x = 1"
julia-eval --env-path ~/project2 "x = 2"
julia-eval --env-path ~/project1 "x"  # displays 1
julia-eval --env-path ~/project2 "x"  # displays 2
```

Debugging with Infiltrator.jl:

```bash
# In one terminal, call a function with @infiltrate
julia-eval "function debug_me(x); y = x * 2; @infiltrate; return y + 1; end; debug_me(5)"
# This will pause at @infiltrate - the command will appear to hang

# In another terminal, send Ctrl-D to exit the Infiltrator context
julia-eval --interrupt
# The original command will now complete
```
