import http.server
import os
import signal
import socketserver
import sys
from .server import CopilotRequestHandler
from .auth import login
from .config import log


class LocalServer(http.server.ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(), a reverse DNS lookup that can stall for tens of seconds on macOS
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return 2
    if argv[0] == "login":
        return login()
    if argv[0] == "serve":
        if len(argv) < 3:
            return 2
        srv = LocalServer(("127.0.0.1", int(argv[1])), CopilotRequestHandler)
        def bye(n, _f):
            log("shim: pid %d exiting on signal %d" % (os.getpid(), n))
            os._exit(0)
        for n in (signal.SIGTERM, signal.SIGINT):
            signal.signal(n, bye)
        with open(argv[2] + ".tmp", "w") as f:
            f.write("%d %d" % (os.getpid(), srv.server_address[1]))
        os.replace(argv[2] + ".tmp", argv[2])
        try:
            srv.serve_forever()
        except BaseException as e:
            log("shim: pid %d died: %r" % (os.getpid(), e))
            raise
    return 2

