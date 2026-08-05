import sys
import os

target = os.path.join(os.environ.get("TEMP", ""), "pyside6_target")
if os.path.isdir(target) and target not in sys.path:
    sys.path.insert(0, target)

script = os.path.join(os.path.dirname(__file__), "sistem_monitor.py")
with open(script, encoding="utf-8") as f:
    code = f.read()
exec(compile(code, script, "exec"))

