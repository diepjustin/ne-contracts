"""No top-level function or class is defined twice in a module of scripts/ or tests/.

Python lets the second definition quietly replace the first. In a test module that means the
first test never runs and nothing says so; in a script it means the code that runs is not the
code that was read. It is cheap to see and easy to cause: a splice of one block of a file over
another can leave a whole function in the file twice, and the suite still passes.
"""

import ast
import collections
import glob
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def top_level_names(source, filename):
    seen = collections.defaultdict(list)
    for node in ast.parse(source, filename).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            seen[node.name].append(node.lineno)
    return seen


def test_the_detector_sees_a_shadowed_definition():
    seen = top_level_names("def a():\n    pass\n\n\ndef b():\n    pass\n\n\ndef a():\n    pass\n", "<x>")
    assert seen["a"] == [1, 9] and seen["b"] == [5]


def test_no_module_defines_a_function_or_class_twice():
    files = sorted(glob.glob(os.path.join(ROOT, "scripts", "*.py")) + glob.glob(os.path.join(ROOT, "tests", "*.py")))
    assert len(files) > 20, "the glob found almost nothing; the test would pass for the wrong reason"
    offenders = []
    for path in files:
        with open(path, encoding="utf-8") as f:
            for name, lines in top_level_names(f.read(), path).items():
                if len(lines) > 1:
                    offenders.append("%s: %s at lines %s" % (os.path.relpath(path, ROOT), name, lines))
    assert not offenders, "defined more than once:\n" + "\n".join(offenders)
