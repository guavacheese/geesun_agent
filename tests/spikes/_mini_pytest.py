"""极简 pytest 替身：生产镜像内没装 pytest，用于跑无 fixture 的 test_* 函数。

用法: python _mini_pytest.py tests/core/test_loop_detection.py [...]
支持 pytest.raises 的最小实现（通过 __import__ hook 注入）。
"""
from __future__ import annotations

import importlib.util
import inspect
import pathlib
import sys
import traceback
import types

sys.path.insert(0, "/app")

# --- 最小 pytest 替身：只实现 raises / approx / fixture(no-op) ---
_mp = types.ModuleType("pytest")


class _Raises:
    def __init__(self, exc, match=None):
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, t, v, tb):
        if t is None:
            raise AssertionError("DID NOT RAISE %r" % (self.exc,))
        return issubclass(t, self.exc)


_mp.raises = lambda exc, match=None: _Raises(exc, match)


class _Approx:
    def __init__(self, v, rel=1e-6, abs=1e-12):
        self.v, self.rel, self.abs = v, rel, abs

    def __eq__(self, other):
        return abs(other - self.v) <= max(self.abs, self.rel * max(abs(self.v), abs(other)))


_mp.approx = _Approx
_mp.fixture = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))
_mp.mark = types.SimpleNamespace(parametrize=lambda *a, **k: (lambda f: f))
sys.modules.setdefault("pytest", _mp)

passed = failed = skipped = 0
for path in sys.argv[1:]:
    # 用 importlib 正规加载：注册进 sys.modules，否则 dataclass 解析注解会炸
    # （cls.__module__ 查不到模块 -> AttributeError）
    modname = "mini_" + pathlib.Path(path).stem
    spec = importlib.util.spec_from_file_location(modname, path)
    ns = importlib.util.module_from_spec(spec)
    sys.modules[modname] = ns
    try:
        spec.loader.exec_module(ns)
    except Exception as e:  # noqa: BLE001
        print("  ✗ %s 导入失败 -> %s: %s" % (path, type(e).__name__, e))
        traceback.print_exc()
        failed += 1
        continue
    cases = [(n, o) for n, o in vars(ns).items()
             if n.startswith("test_") and callable(o) and not inspect.signature(o).parameters]
    if not cases:
        print("  ⊘ %s（无用例，可能是脚本型/需 fixture）" % path)
        skipped += 1
        continue
    for name, fn in cases:
        try:
            fn()
            print("  ✓ %s::%s" % (path, name))
            passed += 1
        except Exception as e:  # noqa: BLE001
            print("  ✗ %s::%s -> %s: %s" % (path, name, type(e).__name__, e))
            traceback.print_exc()
            failed += 1

print("\nmini-pytest: %d passed / %d failed / %d files skipped" % (passed, failed, skipped))
sys.exit(1 if failed else 0)
