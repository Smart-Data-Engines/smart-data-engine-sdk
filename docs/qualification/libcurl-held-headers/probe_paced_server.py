"""A server that sends a status line, then a header a second, then the rest: what the C++ test's
scripted server does. Prints when it sent each piece, relative to the request."""
import socket
import sys
import time

listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", 0))
listener.listen(1)
print(listener.getsockname()[1], flush=True)
connection, _ = listener.accept()
request = b""
while b"\r\n\r\n" not in request:
    request += connection.recv(65536)
start = time.monotonic()
pieces = [(0.0, b"HTTP/1.1 200 OK\r\n")]
pieces += [(1.0, b'X-ClickHouse-Progress: {"read_rows":"%d"}\r\n' % n) for n in range(4)]
pieces.append((0.5, b"Content-Length: 5\r\nConnection: close\r\n\r\nslow\n"))
for pause, piece in pieces:
    time.sleep(pause)
    connection.sendall(piece)
    print(f"sent at {time.monotonic() - start:6.3f}s: {piece[:40]!r}", file=sys.stderr, flush=True)
connection.close()
