H = CopilotRequestHandler

if sys.argv[1] == "login": sys.exit(login())
if sys.argv[1] == "serve":
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), H)
    with open(sys.argv[3] + ".tmp", "w") as f: f.write("%d %d" % (os.getpid(), srv.server_address[1]))
    os.replace(sys.argv[3] + ".tmp", sys.argv[3])
    srv.serve_forever()
