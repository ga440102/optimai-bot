# OptimAi-BOT (Python)

基于 `api.optimai.network` 的 OptimAI 节点自动保活脚本（Python 异步版）。自动完成节点注册、在线心跳（uptime）与每日签到，并负责 access token 续期、refresh token 轮换、限流退避与失效 token 自动跳过。

> 本仓库为 Python 实现（单文件 `bot.py`），与上游 Node/TypeScript 版本互不相干。

## ✨ 功能特性

- **节点注册**：向 `/devices/register-v2` 提交经混淆的注册 payload（移植自官方 `generate_payload.js`，忠实复刻 JS float64 斐波那契混淆，保证服务端校验通过）。
- **在线心跳**：每 10 分钟向 `/uptime/online` 上报一次在线状态，使用服务端返回的真实 `device_id`。
- **每日签到**：每 12 小时调用 `/daily-tasks/check-in` 完成每日任务。
- **Token 自动续期**：access token 在过期前 300 秒自动刷新；若服务端轮换了 refresh token，自动写回 `tokens.txt`，无需手动维护。
- **失效 Token 自动跳过**：refresh token 收到 `400/401/403` 时判定为失效，永久跳过该账号，避免死循环刷接口。
- **限流退避**：按接口维度指数退避（15s → 120s，带抖动），避免同 IP 多点齐刷触发 `429`。
- **并发节流**：同一时刻最多 8 个账号并发刷新 token，且每个账号启动随机错峰 0–60s，降低被限流概率。
- **实时仪表盘**：PERCEPTRON 风格网格看板，顶部统计条（模式 / 在线数 / 运行时长）+ 账号网格，原地刷新（清屏重绘，不滚日志），直观显示每个账号状态与 refresh token 剩余天数。

## 📦 环境要求

- Python 3.8+
- 能访问 `https://api.optimai.network` 的网络环境

## 🔧 安装

```bash
pip install -r requirements.txt
```

依赖：`aiohttp`、`aiohttp-socks`、`fake-useragent`、`colorama`、`pytz`

## ⚙️ 配置

### tokens.txt（必填）

每个 OptimAI 账号一行 refresh token。支持两种格式：

```
# 纯 token
eyJhbGciOi...

# 带名字（便于看板辨识）
gdgzsy1:eyJhbGciOi...
```

- token 必须是完整的 JWT（以 `eyJ` 开头）。
- 脚本会从 JWT 中解码 `sub` / `userId` 作为账号标识，无需手动填写邮箱。
- `#` 开头的行视为注释，会被忽略。
- 可直接复制仓库内的 `tokens.txt.example` 改造为 `tokens.txt`。

### proxy.txt（可选）

仅在「使用代理」模式下生效。每行一个代理，支持三种格式：

```
ip:port                      # 默认 http
protocol://ip:port           # 例如 http://1.2.3.4:8080
protocol://user:pass@ip:port # 带鉴权，例如 socks5://user:pass@1.2.3.4:1080
```

## 🚀 运行

```bash
python bot.py
```

启动后会交互式询问代理模式：

```
1. Run With Monosans Proxy    # 自动拉取公开代理列表
2. Run With Private Proxy     # 使用 proxy.txt 中的私有代理
3. Run Without Proxy          # 直连
```

输入 `1` / `2` / `3` 选择后回车即可。脚本随后进入长期运行（节点注册 + 心跳 + 签到循环），按 `Ctrl+C` 退出。

## 📁 文件结构

```
.
├── bot.py              # 主程序（异步，单文件）
├── requirements.txt    # Python 依赖
├── tokens.txt          # 你的账号 token（被 .gitignore 忽略，勿提交）
├── tokens.txt.example  # token 格式模板（脱敏）
├── proxy.txt           # 你的代理列表（被 .gitignore 忽略，勿提交）
└── proxy.txt.example   # 代理格式模板
```

## ⚠️ 说明

- `tokens.txt` / `proxy.txt` 含有你的私密凭证，已在 `.gitignore` 中忽略，**切勿提交到公开仓库**。
- 本脚本仅用于自动化你自己的 OptimAI 节点运维，请遵守 OptimAI 服务条款与当地法律法规。
