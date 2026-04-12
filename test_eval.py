import json
import os
import socket
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
from julia_daemon.eval import send_request, main, SOCKET_PATH

@pytest.fixture
def mock_socket():
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b'{"status": "ok", "output": "42"}', b""]
    return mock_sock

def test_send_request_socket_not_exists():
    with patch.object(Path, 'exists', return_value=False):
        with pytest.raises(SystemExit) as exc:
            send_request({"command": "eval", "code": "1+1"})
        assert exc.value.code == 1

def test_send_request_success(mock_socket):
    with patch.object(Path, 'exists', return_value=True):
        with patch('socket.socket', return_value=mock_socket):
            response = send_request({"command": "eval", "code": "1+1"})
            assert response["status"] == "ok"
            assert response["output"] == "42"
            mock_socket.connect.assert_called_once()
            mock_socket.sendall.assert_called_once()
            mock_socket.shutdown.assert_called_once_with(socket.SHUT_WR)
            mock_socket.close.assert_called_once()

def test_send_request_encodes_json(mock_socket):
    with patch.object(Path, 'exists', return_value=True):
        with patch('socket.socket', return_value=mock_socket):
            request = {"command": "eval", "code": "2+2", "env_path": "/tmp/test"}
            send_request(request)
            expected = json.dumps(request).encode()
            mock_socket.sendall.assert_called_once_with(expected)

def test_main_eval_code_argument(mock_socket, capsys):
    with patch.object(Path, 'exists', return_value=True):
        with patch('socket.socket', return_value=mock_socket):
            with patch('sys.argv', ['eval.py', '1+1']):
                with pytest.raises(SystemExit) as exc:
                    main()
                assert exc.value.code == 0
                captured = capsys.readouterr()
                assert "42" in captured.out

def test_main_eval_with_env_path(mock_socket):
    with patch.object(Path, 'exists', return_value=True):
        with patch('socket.socket', return_value=mock_socket):
            with patch('sys.argv', ['eval.py', '--env-path', '/tmp/project', '1+1']):
                with pytest.raises(SystemExit) as exc:
                    main()
                assert exc.value.code == 0
                call_args = mock_socket.sendall.call_args[0][0]
                request = json.loads(call_args.decode())
                assert "/tmp/project" in request["env_path"]

def test_main_eval_with_timeout(mock_socket):
    with patch.object(Path, 'exists', return_value=True):
        with patch('socket.socket', return_value=mock_socket):
            with patch('sys.argv', ['eval.py', '--timeout', '10.5', '1+1']):
                with pytest.raises(SystemExit) as exc:
                    main()
                assert exc.value.code == 0
                call_args = mock_socket.sendall.call_args[0][0]
                request = json.loads(call_args.decode())
                assert request["timeout"] == 10.5

def test_main_eval_from_stdin(mock_socket):
    with patch.object(Path, 'exists', return_value=True):
        with patch('socket.socket', return_value=mock_socket):
            with patch('sys.stdin.read', return_value='x = 5\nprintln(x)'):
                with patch('sys.argv', ['eval.py']):
                    with pytest.raises(SystemExit) as exc:
                        main()
                    assert exc.value.code == 0
                    call_args = mock_socket.sendall.call_args[0][0]
                    request = json.loads(call_args.decode())
                    assert request["code"] == 'x = 5\nprintln(x)'

def test_main_shutdown(capsys):
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b'{"status": "ok", "output": "Shutdown complete"}', b""]
    with patch.object(Path, 'exists', return_value=True):
        with patch('socket.socket', return_value=mock_sock):
            with patch('sys.argv', ['eval.py', '--shutdown']):
                with pytest.raises(SystemExit) as exc:
                    main()
                assert exc.value.code == 0
                captured = capsys.readouterr()
                assert "Shutdown complete" in captured.out

def test_main_list_empty(capsys):
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b'{"status": "ok", "sessions": []}', b""]
    with patch.object(Path, 'exists', return_value=True):
        with patch('socket.socket', return_value=mock_sock):
            with patch('sys.argv', ['eval.py', '--list']):
                with pytest.raises(SystemExit) as exc:
                    main()
                assert exc.value.code == 0
                captured = capsys.readouterr()
                assert "No active Julia sessions" in captured.out

def test_main_list_sessions(capsys):
    mock_sock = MagicMock()
    response = {
        "status": "ok",
        "sessions": [
            {"env_path": "/tmp/proj1", "alive": True, "log_file": "/tmp/log1"},
            {"env_path": "/tmp/proj2", "alive": False}
        ]
    }
    mock_sock.recv.side_effect = [json.dumps(response).encode(), b""]
    with patch.object(Path, 'exists', return_value=True):
        with patch('socket.socket', return_value=mock_sock):
            with patch('sys.argv', ['eval.py', '--list']):
                with pytest.raises(SystemExit) as exc:
                    main()
                assert exc.value.code == 0
                captured = capsys.readouterr()
                assert "Active Julia sessions" in captured.out
                assert "/tmp/proj1: alive" in captured.out
                assert "/tmp/proj2: dead" in captured.out
                assert "log=/tmp/log1" in captured.out

def test_main_restart(capsys):
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b'{"status": "ok", "output": "Restarted"}', b""]
    with patch.object(Path, 'exists', return_value=True):
        with patch('socket.socket', return_value=mock_sock):
            with patch('sys.argv', ['eval.py', '--restart']):
                with pytest.raises(SystemExit) as exc:
                    main()
                assert exc.value.code == 0
                captured = capsys.readouterr()
                assert "Restarted" in captured.out

def test_main_restart_with_env_path(mock_socket):
    with patch.object(Path, 'exists', return_value=True):
        with patch('socket.socket', return_value=mock_socket):
            with patch('sys.argv', ['eval.py', '--restart', '--env-path', '/tmp/proj']):
                with pytest.raises(SystemExit) as exc:
                    main()
                assert exc.value.code == 0
                call_args = mock_socket.sendall.call_args[0][0]
                request = json.loads(call_args.decode())
                assert request["command"] == "restart"
                assert "/tmp/proj" in request["env_path"]

def test_main_interrupt(capsys):
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b'{"status": "ok", "output": "Interrupted"}', b""]
    with patch.object(Path, 'exists', return_value=True):
        with patch('socket.socket', return_value=mock_sock):
            with patch('sys.argv', ['eval.py', '--interrupt']):
                with pytest.raises(SystemExit) as exc:
                    main()
                assert exc.value.code == 0
                captured = capsys.readouterr()
                assert "Interrupted" in captured.out

def test_main_error_response(capsys):
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b'{"status": "error", "output": "Something failed"}', b""]
    with patch.object(Path, 'exists', return_value=True):
        with patch('socket.socket', return_value=mock_sock):
            with patch('sys.argv', ['eval.py', '1+1']):
                with pytest.raises(SystemExit) as exc:
                    main()
                assert exc.value.code == 1
                captured = capsys.readouterr()
                assert "Something failed" in captured.err

def test_main_eval_uses_cwd_as_default_env_path(mock_socket):
    with patch.object(Path, 'exists', return_value=True):
        with patch('socket.socket', return_value=mock_socket):
            with patch('os.getcwd', return_value='/home/user/project'):
                with patch('sys.argv', ['eval.py', '1+1']):
                    with pytest.raises(SystemExit):
                        main()
                    call_args = mock_socket.sendall.call_args[0][0]
                    request = json.loads(call_args.decode())
                    assert request["env_path"] == '/home/user/project'

def test_main_interrupt_uses_cwd_as_default_env_path(mock_socket):
    with patch.object(Path, 'exists', return_value=True):
        with patch('socket.socket', return_value=mock_socket):
            with patch('os.getcwd', return_value='/home/user/project'):
                with patch('sys.argv', ['eval.py', '--interrupt']):
                    with pytest.raises(SystemExit):
                        main()
                    call_args = mock_socket.sendall.call_args[0][0]
                    request = json.loads(call_args.decode())
                    assert request["env_path"] == '/home/user/project'
