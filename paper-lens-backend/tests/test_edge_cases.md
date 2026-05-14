# Paper-Lens Web UI 边界测试用例

## 状态: 2026-03-31 发现的问题

### TC-01: 连续发送两条消息（打断场景）
- **操作**: 选论文 → 发消息 "hi" → 不等回复立即发 "hello"
- **期望**: 第一条显示+思考中, 第二条排队或被拒（禁用输入）
- **当前**: 两条都发出, 第二条可能导致 500 或冲突
- **根因**: 无重入锁, State.thinking 检查依赖 render 周期
- **修复**: 加 `_sendingMessage` flag + 立即禁用输入

### TC-02: 学习模式启动后发消息
- **操作**: 点击「学习」按钮 → 不等回复直接在输入框发消息
- **期望**: 输入框和发送按钮禁用, 显示思考中
- **当前**: 模式按钮禁用了, 但输入框和发送按钮没禁用
- **根因**: renderCenter 的 running phase 只在 render 时 sync, dispatch('START_MODE') 后 render 未及时触发 UI 更新
- **修复**: dispatch 中 thinking=true → render → _syncInputEnabled

### TC-03: 切换论文后发消息
- **操作**: 在论文A对话中 → 点击论文B → 发消息
- **期望**: 为论文B启动新 session, 不用论文A的 sessionId
- **当前**: 已修复 (sessionId 在 SELECT_PAPER 时清空)
- **验证**: 确认 State.sessionId = null, State.thinking = false

### TC-04: 切换论文时 thinking 残留
- **操作**: 论文A对话中显示"思考中..." → 切换到论文B
- **期望**: thinking 指示器消失, 按钮恢复
- **当前**: State.thinking 没在 SELECT_PAPER 中重置
- **修复**: 加 `State.thinking = false;`

### TC-05: 聊天记录是否持久化
- **操作**: 发消息收到回复 → 刷新页面
- **期望**: 对话记录不保留（当前设计）
- **当前**: messages 在 State 内存中, 刷新即丢失 ← 这是正确行为
- **注意**: 用户可能看到旧消息是因为 SSE 重连拿到了旧事件, 不是 localStorage

### TC-06: SSE 断连后重连
- **操作**: 正常对话 → 网络断开 → 恢复
- **期望**: EventSource 自动重连, 继续收流
- **当前**: EventSource 自带重连, 但可能丢失中间事件
- **风险**: 中等 — 可接受

### TC-07: 多轮对话（A→B→A 来回）
- **操作**: 发消息 → 收回复 → 再发消息 → 收回复
- **期望**: 每轮都有思考中, 每轮回复追加到聊天区
- **当前**: POST /api/answer 调用 adapter.send_message → 需要 --resume
- **风险**: 高 — ClaudeCLIAdapter 的 send_message 杀旧进程+重启, 可能丢事件

### TC-08: 发送空消息
- **操作**: 点发送按钮, 输入框为空
- **期望**: 无操作
- **当前**: center.js 第一行检查 `if (!text)` ← 正确

### TC-09: 没有 PDF 的论文启动分析
- **操作**: 选一个没有 paper.pdf 的论文 → 点速览
- **期望**: 模式按钮禁用
- **当前**: mode bar 有 `no-pdf` class 禁用 ← 应该正确

### TC-11: 问题卡片待回答时发消息
- **操作**: 学习模式运行 → 出现术语选择问题卡 → 在输入框发消息
- **期望**: 输入框禁用, 只能通过问题卡片回答
- **当前**: 已修复 — `State.waitingAnswer=true` 时禁用输入
- **验证**: 提交问题卡片后输入框恢复

### TC-12: 提交问题卡片后恢复
- **操作**: 选择术语 → 点提交
- **期望**: thinking 指示器出现, 输入框保持禁用直到回复到达
- **当前**: 已修复 — 提交后 `waitingAnswer=false, thinking=true`

### TC-10: 发送后输入框禁用时机
- **操作**: 发消息
- **期望**: 发送按钮立即变灰, 输入框不可编辑
- **当前**: _sendUserMessage 不禁用按钮, 依赖 render 周期
- **修复**: 函数开头立即禁用, finally 中恢复
