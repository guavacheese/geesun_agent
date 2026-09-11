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


def _fixture(*a, **k):
    """最小 fixture 标记（支持 @pytest.fixture 与 @pytest.fixture() 两种写法）。"""

    def deco(fn):
        fn._is_mini_fixture = True
        return fn

    if a and callable(a[0]):
        return deco(a[0])
    return deco


_mp.approx = _Approx
_mp.fixture = _fixture
_mp.mark = types.SimpleNamespace(parametrize=lambda *a, **k: (lambda f: f))
sys.modules.setdefault("pytest", _mp)

passed = failed = skipped = 0
skipped_names: list[str] = []
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
    fixtures = {
        n: o for n, o in vars(ns).items() if getattr(o, "_is_mini_fixture", False)
    }
    cases = [
        (n, o)
        for n, o in vars(ns).items()
        if n.startswith("test_") and callable(o) and not getattr(o, "_is_mini_fixture", False)
    ]
    if not cases:
        print("  ⊘ %s（无用例）" % path)
        skipped += 1
        continue
    for name, fn in cases:
        # 参数全部按名解析为 fixture（仅支持零参 fixture，够本仓用）
        params = list(inspect.signature(fn).parameters)
        unknown = [p for p in params if p not in fixtures]
        if unknown:
            print("  ⊘ %s::%s（缺 fixture: %s）" % (path, name, ", ".join(unknown)))
            skipped += 1
            skipped_names.append(name)
            continue
        try:
            fn(**{p: fixtures[p]() for p in params})
            print("  ✓ %s::%s" % (path, name))
            passed += 1
        except Exception as e:  # noqa: BLE001
            print("  ✗ %s::%s -> %s: %s" % (path, name, type(e).__name__, e))
            traceback.print_exc()
            failed += 1

print(
    "\nmini-pytest: %d passed / %d failed / %d skipped%s"
    % (passed, failed, skipped, (" (%s)" % ", ".join(skipped_names)) if skipped_names else "")
)
sys.exit(1 if failed else 0)
