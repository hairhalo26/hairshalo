"""Static server that mirrors the production Caddy routing.

Caddyfile has:  try_files {path} {path}.html, a 404.html for anything else,
and two email-link paths that still land on the storefront.
`python -m http.server` has no equivalent, so /signin and /reset-password 404
locally while working in production. This makes local testing behave the same.

    python devserver.py <frontend-dir> <port>
"""
import os
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

ROOT = os.path.abspath(sys.argv[1])
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 5500

# Linked from emails but with no page of their own yet; they land on the
# storefront, as they do in production (see the Caddyfile).
STOREFRONT_PATHS = ('/unsubscribe', '/confirm-subscription')
NOT_FOUND = object()


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
        if urlparse(path).path.rstrip('/') in STOREFRONT_PATHS:
            return os.path.join(ROOT, 'index.html')
        return NOT_FOUND

    def send_head(self):
        if self.translate_path(self.path) is NOT_FOUND:
            # The branded page, with the status a crawler or a link checker
            # needs to see: this address is not a page.
            page = open(os.path.join(ROOT, '404.html'), 'rb')
            size = os.fstat(page.fileno()).st_size
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(size))
            self.end_headers()
            return page
        return super().send_head()

    def log_message(self, fmt, *args):
        path = urlparse(unquote(self.path)).path
        sys.stderr.write('%s %s\n' % (self.command, path))


ThreadingHTTPServer.allow_reuse_address = True
print('serving %s on http://localhost:%d (try_files like Caddy)' % (ROOT, PORT), flush=True)
ThreadingHTTPServer(('127.0.0.1', PORT), TryFilesHandler).serve_forever()
