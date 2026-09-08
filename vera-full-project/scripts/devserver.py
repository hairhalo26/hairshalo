"""Static server that mirrors the production Caddy routing.

Caddyfile has:  try_files {path} {path}.html /index.html
`python -m http.server` has no equivalent, so /signin and /reset-password 404
locally while working in production. This makes local testing behave the same.

    python devserver.py <frontend-dir> <port>
"""
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

ROOT = os.path.abspath(sys.argv[1])
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 5500


class TryFilesHandler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def translate_path(self, path):
        # Resolve through the parent first: it normalises away "..", which is
        # what keeps this from serving files outside the frontend directory.
        resolved = super().translate_path(path)
        if os.path.isdir(resolved):
            index = os.path.join(resolved, 'index.html')
            return index if os.path.exists(index) else resolved
        if os.path.exists(resolved):
            return resolved
        with_html = resolved + '.html'
        if os.path.exists(with_html):
            return with_html
        return os.path.join(ROOT, 'index.html')

    def log_message(self, fmt, *args):
        path = urlparse(unquote(self.path)).path
        sys.stderr.write('%s %s\n' % (self.command, path))


ThreadingHTTPServer.allow_reuse_address = True
print('serving %s on http://localhost:%d (try_files like Caddy)' % (ROOT, PORT), flush=True)
ThreadingHTTPServer(('127.0.0.1', PORT), TryFilesHandler).serve_forever()
