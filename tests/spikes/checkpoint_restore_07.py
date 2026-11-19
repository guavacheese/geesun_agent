"""Spike: 生产 checkpoint（0.6.12 树写入）在 0.7.24 依赖树真实恢复（2026-11-19）。

背景（动作三两大迁移风险之一）：
生产 f508e75c（889 条消息）是真实资产。升级后 checkpoint 能否恢复必须实测。

⚠ 重要事实（本次实测澄清）：langgraph-checkpoint-postgres 3.x 用 **delta 存储**
——checkpoint 行存增量引用，完整 seed blob 只留少数（全库 GAP 是设计内行为），
恢复时 stage1 沿链回溯 seed 并重放 delta。因此**不能用手工拼 blob 的方式测**，
必须用真实 AsyncPostgresSaver（走完整 stage1/回溯/重放路径）。

方法：
1. 生产 agent-postgres 里 COPY OUT f508e75c 三表（checkpoints/checkpoint_blobs/
   checkpoint_writes，text 格式 gzip）→ 推到 67 的测试 postgres 容器
   （cp-restore-test:15432，用完即删）。
2. 本地 0.7.24 venv：AsyncPostgresSaver.setup() 建表 → COPY IN 原始数据。
3. aget_tuple 验证 delta 重放出完整消息列表 → 完整 middleware 栈的 agent
   ainvoke(None) → 断言模型收到全部恢复历史 + 新 checkpoint 写入成功。

运行: python tests/spikes/checkpoint_restore_07.py <dir-with-cp_*.gz>
"""

import asyncio
import gzip
import os
import sys
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("BASE_URL", "http://fake:8003/v1")
os.environ.setdefault("OPENAI_API_KEY", "spike-key")
os.environ.setdefault("MODEL_NAME", "Qwen3.6-35B-A3B")
os.environ.setdefault("AGENT_WORKSPACE", "./.spike-workspace")

DATA_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"D:\workspace")
DB_DSN = os.environ.get(
    "CP_TEST_DSN",
    "postgresql://geesun:test123@10.10.10.67:15432/agent_mem_restore",
)
THREAD = "louzexi:f508e75c"

import src.services.agent as agent_mod
from src.infra.database import ReconnectingAsyncPostgresSaver
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

RESULTS = []


def check(tag, cond, detail=""):
    ok = bool(cond)
    RESULTS.append((tag, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {tag}" + (f" — {detail}" if detail else ""))


async def main():
    import psycopg

    # ─── [1] 建表 + 导入 ───
    print("== [1] 0.7 树 setup 建表 + COPY IN 生产数据 ==")
    conn = await psycopg.AsyncConnection.connect(DB_DSN, autocommit=True)
    saver = AsyncPostgresSaver(conn)
    await saver.setup()

    import json

    await conn.execute("DELETE FROM checkpoints WHERE thread_id=%s", (THREAD,))
    await conn.execute("DELETE FROM checkpoint_blobs WHERE thread_id=%s", (THREAD,))
    await conn.execute("DELETE FROM checkpoint_writes WHERE thread_id=%s", (THREAD,))

    specs = [
        ("checkpoints", "cp_checkpoints.gz",
         "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata)"),
        ("checkpoint_blobs", "cp_blobs.gz",
         "(thread_id, checkpoint_ns, channel, version, type, blob)"),
        ("checkpoint_writes", "cp_writes.gz",
         "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob)"),
    ]
    for table, fname, cols in specs:
        path = DATA_DIR / fname
        raw = gzip.open(path, "rb").read().decode("utf-8")
        async with conn.cursor() as cur:
            async with cur.copy(f"COPY {table} {cols} FROM STDIN") as copy:
                await copy.write(raw)
        n = (await (await conn.execute(f"SELECT count(*) FROM {table} WHERE thread_id=%s", (THREAD,))).fetchone())[0]
        check(1, n > 0, f"{table}: {n} rows imported")

    # ─── [2] aget_tuple：delta 回溯重放（核心兼容性判据）───
    print("== [2] aget_tuple（0.7 树 stage1 回溯 + delta 重放）==")
    cfg = {"configurable": {"thread_id": THREAD, "checkpoint_ns": ""}}
    tpl = await saver.aget_tuple(cfg)
    check(2, tpl is not None, f"checkpoint_id={tpl.checkpoint['id'] if tpl else 'N/A'}")
    if tpl is None:
        raise SystemExit("no checkpoint")
    # 3.1.2 delta 存储：aget_tuple 的 channel_values.messages 保留 delta 引用形态
    # （完整列表由 langgraph 在执行时重放）——断言版本引用存在即可，重放判据在 [3]
    ver = tpl.checkpoint.get("channel_versions", {}).get("messages")
    check(2, str(ver) == "3013", f"messages channel version 引用 = {ver}（生产最新值）")

    # ─── [3] 完整 middleware 栈 agent 恢复 invoke ───
    print("== [3] ainvoke(None) 真实恢复 ==")

    class _FakeWithCalls(GenericFakeChatModel):
        # 0.7 树 model 节点调用形态与 0.6 不同（trace 证实 model 节点执行但
        # _astream 探针 0 命中）→ 四个入口全部记录，与调用形态解耦。
        seen: "ClassVar[list]" = []

        def bind_tools(self, tools, **kw):
            return self

        def _record(self, messages):
            self.seen.append(list(messages))

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            self._record(messages)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kw)

        def _stream(self, messages, stop=None, run_manager=None, **kw):
            self._record(messages)
            return super()._stream(messages, stop=stop, run_manager=run_manager, **kw)

        async def _agenerate(self, messages, stop=None, run_manager=None, **kw):
            self._record(messages)
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kw)

        async def _astream(self, messages, stop=None, run_manager=None, **kw):
            self._record(messages)
            async for chunk in super()._astream(messages, stop=stop, run_manager=run_manager, **kw):
                yield chunk

    def _gen():
        while True:
            yield AIMessage(content="ok")

    fake_model = _FakeWithCalls(messages=_gen(), profile={"max_input_tokens": 262144})
    budget = agent_mod.ToolOutputBudgetMiddleware(
        backend=None, enabled=False, externalize_min_chars=1, fallback_max_chars=1)
    sum_mw = agent_mod._SummarizationAccurate(
        model=fake_model, backend=None,
        trigger=("fraction", 0.6), keep=("fraction", 0.10),
        token_counter=lambda ms, tools=None: 10,
        partial_token_counter=lambda ms, tools=None: 10)
    composite = agent_mod._SweepThenSummarize(budget, sum_mw)
    backend = agent_mod.StateBackend()
    skills_dir = Path(os.environ["AGENT_WORKSPACE"]) / "spike-skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    # ⚠ checkpointer 必须用生产同款 ReconnectingAsyncPostgresSaver：
    # 该类继承 BaseCheckpointSaver 且不覆写 get_next_version → 运行时版本号走
    # base 的 int+1（生产库 398 万个 channel_versions 全 int 即此来源）。
    # 若直接挂裸 AsyncPostgresSaver，其 get_next_version 返回
    # f"{v:032}.{rand}"（str），与库内 int 版本号比较即 TypeError
    # ——2026-11-19 实证：这是测试环境与生产不同构，非 0.7 不兼容。
    prod_saver = ReconnectingAsyncPostgresSaver(DB_DSN)
    await prod_saver.setup()

    agent = agent_mod._build_deep_agent(
        model=fake_model, tools=[], backend=backend, skills=None,
        middleware=[
            agent_mod._FreshSkillsMiddleware(backend=backend, sources=[str(skills_dir)]),
            budget,
            composite,
        ],
        checkpointer=prod_saver,
    )

    try:
        # invoke 前 checkpoint 总数（写回判据的基线）
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM checkpoints WHERE thread_id=%s", (THREAD,))
            cps_before = (await cur.fetchone())[0]

        # 真实场景 = 用户带新消息继续会话（input=None + 图已完成 → 直接返回 state 不执行）
        from langchain_core.messages import HumanMessage as _Human

        # 抓执行轨迹：本轮每个 super-step 更新了哪些节点（解释 model 是否被调度）
        trace = []
        async for ev in agent.astream(
            {"messages": [_Human(content="继续刚才的任务")]},
            config=cfg, stream_mode="updates",
        ):
            if isinstance(ev, dict):
                trace.extend(ev.keys())
            elif isinstance(ev, tuple) and len(ev) == 2:  # subgraph 事件 (ns, payload)
                ns, payload = ev
                trace.append(f"{ns[0] if ns else '?'}/{list(payload)[0] if isinstance(payload, dict) else payload}")
        print("  [trace] 执行节点序列:", trace[:40])
        result = await agent.aget_state(cfg)
        got = (result.values or {}).get("messages", []) if hasattr(result, "values") else []
        check(3, len(got) >= 500, f"恢复后 state messages={len(got)} 条（889 历史量级）")
        check(3, "沙箱 ID" in str(getattr(got[0], "content", "")) if got else False,
              "state 首条 = 生产会话最早历史（恢复自 checkpoint 而非凭空新会话）")
        # 准确判据 = model 收到的消息与 checkpoint 恢复历史有实质交集。
        # ⚠ 不能断言"收到 ≥500 条"：主链 _SummarizationAccurate 在模型调用前先
        # 摘要压缩（keep=fraction 0.10 → 889 条压到 ~117 条），这是生产「先瘦身
        # 再喂模型」的预期形态；摘要链自身那次调用 + 主链调用合计 ≥2 次。
        hist_ids = {getattr(m, "id", None) for m in got} - {None}
        seen_ids = {getattr(m, "id", None) for s in fake_model.seen for m in s} - {None}
        overlap = hist_ids & seen_ids
        check(3, len(fake_model.seen) >= 1 and len(overlap) >= 50,
              f"model 被调用 {len(fake_model.seen)} 次，收到消息中 {len(overlap)} 条"
              "来自 checkpoint 恢复历史（摘要压缩后的尾部=「先瘦身再喂模型」预期形态）")
    except Exception as e:
        import traceback
        traceback.print_exc()
        check(3, False, f"恢复失败: {type(e).__name__}: {e}")
        await conn.close()
        return

    # ─── [4] 0.7 树写路径（新 checkpoint 落库）───
    print("== [4] 0.7 树 checkpoint 写回 ==")
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM checkpoints WHERE thread_id=%s", (THREAD,))
        cps_after = (await cur.fetchone())[0]
    check(4, cps_after > cps_before,
          f"invoke 前 {cps_before} → 后 {cps_after}（0.7 树写路径正常）")

    await conn.close()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n===== CHECKPOINT RESTORE RESULT: {passed}/{total} passed =====")
    if passed != total:
        for tag, ok, detail in RESULTS:
            if not ok:
                print(f"  FAIL {tag}: {detail}")
        sys.exit(1)
    print("ALL_OK")


# Windows ProactorEventLoop 不支持 psycopg async（SelectorEventLoop 兼容）
if sys.platform == "win32":
    import selectors

    asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
else:
    asyncio.run(main())
