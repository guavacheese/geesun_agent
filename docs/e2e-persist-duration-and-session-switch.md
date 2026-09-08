# E2E 验证步骤 — 持久化时长字段 + 切换 session 不丢内容

> 2026-09-08 修复后的人工 E2E 验证清单。部署到生产前必须跑一遍，每项都要 ✅ 才算修复闭环。
> 自动化覆盖：`tests/spikes/chat_persist_duration.py` + `scripts/spike-reasoning-block.cjs` + `scripts/spike-chatarea-cache.cjs`。

---

## 准备

1. 后端部署到生产机（172.16.66.13）— `geesun_agent/` 重启：
   ```bash
   cd /opt/geesun-agent  # 或实际部署路径
   git pull origin master
   ./start_stack.sh --with=web
   ```
2. 前端构建产物同步到生产（已通过 Caddy 反代到 geesun-agent:3000）。
3. 浏览器登录目标用户账号。

---

## 场景 1：思考秒数跨刷新稳定显示（持久化字段验证）

**目的**：验证后端 `reasoning_duration_ms` / `turn_duration_ms` 持久化字段生效。

**步骤**：
1. 选一个空 session（或新建一个），发送一条会触发 CoT 推理的问题（建议 Qwen3.6-35B-A3B 走 reasoning_content 字段，例如"分析一下这份协议的核心矛盾点"）。
2. 观察流式期间：ReasoningBlock 标题"思考中 (Ns)"实时跳秒。
3. 流结束后等 1 秒让 ReasoningBlock 自动收起 → 标题应变成"思考过程 (共 Ns)"。
4. **关键**：**刷新浏览器页面（F5 或 Ctrl+R）**。
5. 切回原 session，ReasoningBlock 标题应**仍**显示"思考过程 (共 Ns)"，秒数与刷新前一致（允许 ±1s 误差，因为后端持久化时刻与前端刷新时刻可能差几百毫秒）。

**预期**：
- ✅ 刷新前后秒数稳定
- ❌ 刷新后变成"思考过程"（无秒数） → 后端持久化字段未写入，spike 跑过但部署未生效

**后端日志验证**：
```bash
tail -f /var/log/geesun-agent/server.log | grep -E "SSE 结束|会话保存完成"
# 应看到 "[DIAG] 会话保存完成: user=..., session=..., msgs=N"
```

**PostgreSQL 验证**（可选，更精确）：
```sql
-- 查 AI 消息是否带 reasoning_duration_ms
SELECT role, reasoning IS NOT NULL AS has_reasoning,
       reasoning_duration_ms, turn_duration_ms, reasoning_started_at
FROM messages_items
WHERE session_id = 'your-session-id'
  AND role = 'ai'
ORDER BY created_at DESC
LIMIT 5;
-- 期望：reasoning_duration_ms > 0、turn_duration_ms > 0
```

---

## 场景 2：切换 session 后不丢内容（核心修复验证）

**目的**：验证 ChatArea useEffect "无重叠覆盖"修复生效。

### 子场景 2a：A 流式 → 切 B → 切回 A

1. session A 发送一条**长耗时**问题（如"分析 80 页技术协议"），A 流式进行中。
2. **不要等 A 完成**，立即切到 session B。
3. 在 B 上观察：显示 B 的历史消息，无 A 的中间内容污染（c186e49 修复已保证）。
4. 等待 A 在后台跑完成（监听"任务执行完成"角标消失）。
5. 切回 session A。
6. **关键**：A 应显示**完整最终版**（含后台跑的全部 token / tool_call / file_generated）。

**预期**：
- ✅ A 显示完整最终版本
- ❌ A 停留在切走瞬间的中间版本 → useEffect 覆盖逻辑未生效，msgs 可能因网络慢延迟到达

### 子场景 2b：A 流式 → 切 B → B 流式 → 切回 A

1. session A 发送长耗时问题，A 流式中。
2. 切到 session B，B 发送另一条消息，B 流式开始（A 仍在后台跑）。
3. B 完成前，切回 session A。
4. **关键**：A 应显示**A 后台跑的最新版本**（不等同于切走瞬间），且 A 后续 token 继续追加。

**预期**：
- ✅ A 显示 A 后台跑的当前版本
- ⚠️ 已知 limitation：A 仍在流期间切来切去时，A 后台最新拼接 token 可能被 server 旧版本短暂覆盖（边界 case）

### 子场景 2c：刷新页面（验证持久化）

1. 任意 session A 完成一次对话。
2. 刷新浏览器页面（F5）。
3. A 应显示完整历史消息（getSessionMessages 从 server 拉）。

**预期**：
- ✅ 刷新后看到完整历史
- ❌ 历史空白 → getSessionMessages 失败 / 后端 store 写入有问题

---

## 场景 3：边界条件压力测试

### 子场景 3a：极端切换（切 5 次不同 session）

1. session A 发长耗时问题。
2. 依次切到 B / C / D / E，每个都看一眼再切走（A 在后台跑）。
3. 等待 30 秒（A 估计已跑完）。
4. 切回 A。
5. A 应显示完整最终版。

### 子场景 3b：网络慢模拟（getSessionMessages 延迟）

1. 后端临时 sleep 5 秒模拟网络慢（`time.sleep(5)` 注入到 `chat.py:_persist_session`，**仅在本地开发**）。
2. 切 session A → B。
3. 切回 A，**观察前 100ms**（cache 骨架期间）：用户应看到 A 的"切走瞬间中间版本"。
4. 5 秒后 msgs 拉到，setMessages(msgs) 覆盖，A 显示**完整最终版**。

**预期**：
- ✅ 100ms 内显示中间版本（cache 骨架），5 秒后切换到完整版
- ❌ 100ms 后一直是中间版本（msgs 覆盖未生效） → 修复失败

### 子场景 3c：断连路径

1. session A 发长耗时问题，A 流式进行中。
2. **直接关闭浏览器 tab**（不发停止按钮）。
3. 重新打开浏览器，登录，看 session A 的消息列表。
4. A 应显示"断连强制保存"的中间版本（不全但比空白好）。

**预期**：
- ✅ A 至少有 user 消息 + 中间 AI 消息（断连强制保存生效）
- ❌ A 完全空白 → 断连强制保存失败，chat.py:1338-1414 finally 路径未触发

**后端日志验证**：
```bash
tail -f /var/log/geesun-agent/server.log | grep "断连强制保存完成"
# 应看到 "[DIAG] 断连强制保存完成: user=..., session=..."
```

---

## 自动化覆盖确认

部署前跑一遍 spike：

```bash
# 后端
cd geesun_agent
python tests/spikes/chat_persist_duration.py
# 期望：=== 总结: 0 失败 ===

# 前端
cd geesun_agent_web
./node_modules/.bin/tsc.exe --noEmit   # 类型检查零错误
node scripts/spike-reasoning-block.cjs
node scripts/spike-chatarea-cache.cjs
# 两个 spike 都期望：=== 总结: 0 失败 ===
```

---

## 失败排查 quickref

| 现象 | 可能根因 | 排查命令 |
|---|---|---|
| 思考秒数刷新即丢 | 后端 chat.py 改动未部署 / spike 通过但 `_persist_session` 调用点未传时间戳 | `grep -n 'reasoning_started_at_ms' src/api/endpoints/chat.py` |
| 切换 session 内容仍丢 | 前端 ChatArea.tsx 改动未部署 / TypeScript 类型错误 | `grep -n 'streamSig.current.isStreaming' app/chat/components/ChatArea.tsx` |
| 后端 spike 失败 | chat.py entry 构造逻辑改了但 spike 未同步 | 对照 spike 的 `build_entry` 与 chat.py:478-528 |
| 前端 spike 失败 | ReasoningBlock / ChatArea 逻辑改了但 spike 未同步 | 对照 spike 与 ReasoningBlock.tsx:54-69 / ChatArea.tsx:90-141 |
| 断连路径丢消息 | chat.py:1338-1414 finally 路径未触发 | `grep -n 'interrupted' src/api/endpoints/chat.py` |