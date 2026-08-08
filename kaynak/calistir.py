import sys
import os

script = os.path.join(os.path.dirname(__file__), "sistem_monitor.py")
with open(script, encoding="utf-8") as f:
    code = f.read()
exec(compile(code, script, "exec"))
