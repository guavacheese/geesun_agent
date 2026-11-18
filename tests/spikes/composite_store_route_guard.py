"""Spike: 目录列举收敛 —— 根 ls 给清单、记忆存储不参与全路由枚举（2026-11-18）。

背景（生产会话 GY35362:c6a0eebd，08:36）：
  ① `ls("/")` 返回 `path_not_found` —— CompositeBackend.als 先问 default（sandbox，
     没有虚拟根），一报错就 return，路由目录永远列不出来，模型为此白跑两步；
  ② `glob(pattern, path=None)` 会遍历**全部路由**，包括 /workspace/memories、
     /workspace/agent-memory 两个 StoreBackend → 触发
     `'ReconnectingAsyncPostgresStore' object has no attribute 'search'`；
  ③ ls 在路由上抛异常时无人兜底 → 异常直接穿过工具边界打断 SSE。

本 spike 用"被访问就报错"的假 store 验证我们的两条收敛动作：
  · 无 path 的 glob/aglob **不会碰到** StoreBackend 路由；
  · 根目录 ls/als 返回路由目录清单（不依赖 default 的成败）；
  · 路由上的 ls 异常被收敛成 LsResult(error=...)（工具可见失败，不炸流）。

运行: python tests/spikes/composite_store_route_guard.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve()
# 兼容两种落点：仓内 tests/spikes/（parents[2] = 仓根）与容器里的 /tmp/xxx.py
for _cand in (_ROOT.parents[2] if len(_ROOT.parents) > 2 else _ROOT.parent, Path("/app"), Path.cwd()):
    if (_cand / "src").is_dir():
        sys.path.insert(0, str(_cand))
        break

for _k, _v in (
    ("BASE_URL", "http://fake:8003/v1"),
    ("OPENAI_API_KEY", "spike-key"),
    ("MODEL_NAME", "Qwen3.6-35B-A3B"),
    ("AGENT_WORKSPACE", "./.spike-workspace"),
):
    os.environ.setdefault(_k, _v)

from deepagents.backends.protocol import GlobResult, GrepResult, LsResult
from deepagents.backends.store import StoreBackend

from src.services.agent import ValidatedCompositeBackend

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name} {detail}")


class _AngryStore:
    """同步访问即抛错：用来证明 StoreBackend 路由**没有被枚举到**。

    与生产一致的诱因是"有 search 属性但异步"→ AttributeError；这里换成显式
    RuntimeError，好处是即便将来 deepagents 补了同步垫片，本判据依然能证明
    "我们根本没让它被问到"，而不是靠缺属性这种偶然形态。
    """

    def __init__(self) -> None:
        self.hits = 0

    def search(self, *a, **kw):
        self.hits += 1
        raise RuntimeError("store route enumerated（记忆存储不该参与全路由枚举）")

    def get(self, *a, **kw):
        self.hits += 1
        raise RuntimeError("store route enumerated")

    def put(self, *a, **kw):
        self.hits += 1
        raise RuntimeError("store route enumerated")


class _FakeFs:
    """最小 FilesystemBackend 替身：只实现本 spike 用到的路径。"""

    def __init__(self, root_error: bool = False) -> None:
        self.root_error = root_error
        self.touched: list[tuple] = []

    def ls(self, path: str) -> LsResult:
        self.touched.append(("ls", path))
        if path == "/" and self.root_error:
            return LsResult(error="path_not_found")
        return LsResult(entries=[{"path": "/f.txt", "is_dir": False, "size": 1}])

    async def als(self, path: str) -> LsResult:
        self.touched.append(("als", path))
        if path == "/" and self.root_error:
            return LsResult(error="path_not_found")
        return LsResult(entries=[{"path": "/f.txt", "is_dir": False, "size": 1}])

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return GlobResult(matches=[{"path": "/f.txt"}])

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        return GlobResult(matches=[{"path": "/f.txt"}])

    def grep(self, pattern: str, path=None, glob=None) -> GrepResult:
        return GrepResult(matches=[])

    async def agrep(self, pattern: str, path=None, glob=None) -> GrepResult:
        return GrepResult(matches=[])


def _build(angry: _AngryStore | None = None) -> ValidatedCompositeBackend:
    angry = angry or _AngryStore()
    # default 用"根目录 path_not_found"的假 sandbox，复刻生产形态
    return ValidatedCompositeBackend(
        default=_FakeFs(root_error=True),
        routes={
            "/uploads/u1/s1/": _FakeFs(),
            "/workspace/memories/": StoreBackend(namespace=lambda _rt: ("memories", "u1"), store=angry),
            "/workspace/agent-memory/": StoreBackend(namespace=lambda _rt: ("__agent__",), store=angry),
        },
        user_id="u1",
        session_id="s1",
    )


async def main() -> None:
    print("\n[1] 路由归类")
    cb = _build()
    check(
        "两个 StoreBackend 路由被识别为记忆存储",
        set(cb._store_route_prefixes) == {"/workspace/memories/", "/workspace/agent-memory/"},
        f"got {cb._store_route_prefixes}",
    )

    print("\n[2] 无 path 的 glob 不遍历记忆存储")
    angry = _AngryStore()
    cb = _build(angry)
    res = await cb.aglob("/uploads/u1/s1/*")
    check("aglob 成功（不再因 store.search 崩）", res.error is None, f"error={res.error}")
    check("aglob 结果非空", bool(res.matches), f"matches={res.matches}")
    check("假 store 一次都没被问（枚举已跳过）", angry.hits == 0, f"hits={angry.hits}")
    res_sync = cb.glob("/uploads/u1/s1/*")
    check("同步 glob 同样跳过记忆存储", res_sync.error is None and angry.hits == 0,
          f"error={res_sync.error} hits={angry.hits}")

    print("\n[3] 根目录列举：不再被 default 的 path_not_found 短路")
    root = await cb.als("/")
    paths = [e.get("path") for e in (root.entries or [])]
    check("als('/') 成功", root.error is None, f"error={root.error}")
    check("含 /uploads/ 与 /workspace/", "/uploads/" in paths and "/workspace/" in paths, f"paths={paths}")
    check("目录项去重（/workspace 只出现一次）", paths.count("/workspace/") == 1, f"paths={paths}")
    root_sync = cb.ls("/")
    check("同步 ls('/') 同样给清单", root_sync.error is None and bool(root_sync.entries))

    print("\n[4] 记忆路由上的失败被收敛成 error 结果（不炸流）")
    ls_mem = await cb.als("/workspace/memories")
    check("als 记忆目录 → LsResult(error=...) 而不是抛异常", ls_mem.error is not None, f"error={ls_mem.error}")
    check("错误信息带上原因（store route enumerated）", "store route enumerated" in (ls_mem.error or ""),
          f"error={ls_mem.error}")
    check("确实访问过假 store（证明第 4 步不是被跳过）", angry.hits > 0, f"hits={angry.hits}")

    print("\n[5] 有 path 的 glob 照常路由到单 backend")
    cb2 = _build()
    res2 = await cb2.aglob("*.txt", path="/uploads/u1/s1/")
    check("限定 path 的 aglob 正常", res2.error is None and bool(res2.matches), f"error={res2.error}")


asyncio.run(main())

print(f"\n{'=' * 60}")
print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    sys.exit(1)
print("ALL OK — 目录列举收敛验证通过")
