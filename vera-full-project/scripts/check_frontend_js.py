"""Syntax-check every inline <script> in the storefront and Admin Panel.

The frontend has no build step, so a stray quote or an unescaped newline in a
string is only discovered by a customer: the browser drops the whole <script>
block, and with it checkout. This runs each block through `node --check`.

    python scripts/check_frontend_js.py [frontend-dir]
"""
import glob
import os
import re
import subprocess
import sys
import tempfile

FRONTEND = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "frontend")

failures = 0
targets = sorted(glob.glob(os.path.join(FRONTEND, "*.html"))) + \
    sorted(glob.glob(os.path.join(FRONTEND, "assets", "*.js")))
for path in targets:
    text = open(path, encoding="utf-8").read()
    if path.endswith(".js"):
        blocks = [(1, text)]
    else:
        blocks = [(text[:m.start()].count("\n") + 1, m.group(1))
                  for m in re.finditer(r"<script>(.*?)</script>", text, re.S)]
    for line, code in blocks:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(code)
        result = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
        os.unlink(f.name)
        if result.returncode:
            failures += 1
            print(f"FAIL {os.path.relpath(path, FRONTEND)} (script starting at line {line})")
            print(result.stderr.strip().split("\n", 1)[-1][:600])

print(f"{len(targets)} files checked; " + ("all scripts parse" if not failures else f"{failures} failing"))
sys.exit(1 if failures else 0)
