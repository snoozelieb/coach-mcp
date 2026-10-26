"""No bare open() in coach/: every data-file read and write goes through
coach.storage.

A bare open() of a file that another server process writes made that
process's atomic os.replace fail with "access denied" on Windows (CPython
does not open with FILE_SHARE_DELETE), and the snapshot swallowed the failed
write as "ingestion failed" — a mechanism for activities vanishing from
fitness_history. storage.read_json/write_json/update_json take the file lock,
so a locked reader never overlaps a locked writer.
"""
import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PACKAGE = REPO_ROOT / 'coach'
ALLOWED = {'coach/storage.py'}  # the one module that touches files directly


def _open_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == 'open':
            yield node.lineno
        elif (isinstance(func, ast.Attribute) and func.attr in ('open', 'read_text', 'write_text')
              and not (isinstance(func.value, ast.Name) and func.value.id == 'json')):
            yield node.lineno


def test_no_bare_file_io_outside_storage():
    offenders = []
    for path in sorted(PACKAGE.rglob('*.py')):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        offenders.extend(f"{rel}:{line}" for line in _open_calls(tree))
    assert not offenders, (
        "bare file I/O — route it through coach.storage "
        "(read_json / write_json / update_json):\n  " + "\n  ".join(offenders))
