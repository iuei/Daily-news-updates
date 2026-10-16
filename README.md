# 每日资讯推送（飞书）

每个工作日早上 **09:05** 自动生成一份 **20 条**的资讯日报，推送到飞书机器人。手机上 3–5 分钟扫完。

- **全中文可读**：英文内容自动转成中文摘要，英文原题降为灰色小字附注
- **零第三方依赖**：只用 Python 标准库，不需要 pip install 任何东西
- **零付费数据源**：23 个免费源，全部实测可用，无需任何数据 API Key
- **失败可降级**：摘要服务挂了、某个源挂了、卡片发不出去，都不会丢日报

---

## 一、换电脑后 5 分钟部署

### 前置条件

| 依赖 | 要求 | 检查命令 |
|---|---|---|
| Windows | 10 / 11 | — |
| Python | **3.9 或更高**（实测 3.14 可用） | `python --version` |
| 飞书群机器人 | 有 Webhook 地址 | 见下方第 2 步 |
| DeepSeek API Key | 可选，但**强烈建议**（否则英文内容不会变成中文） | 见下方第 3 步 |

> 不需要 pip、不需要虚拟环境、不需要 Docker、不需要 Git（克隆时除外）。

### 步骤

```powershell
# 1. 克隆仓库
git clone https://github.com/iuei/Daily-news-updates.git
cd Daily-news-updates

# 2. 生成配置
Copy-Item config.example.json config.json

# 3. 编辑 config.json（用记事本即可，务必另存为「UTF-8 无 BOM」）
#    填两个地方：
#      "feishu":   { "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/你的token" }
#      "deepseek": { "api_key": "sk-你的key" }        ← 可留空，留空则英文不翻译

# 4. 自检（离线，不联网）
python daily_digest.py --selftest

# 5. 验证飞书通不通（手机会立刻收到一张测试卡片）
python daily_digest.py --test-push

# 6. 试跑一次（抓取+组版+中文摘要，但不推送、不写台账）
python daily_digest.py --dry-run

# 7. 注册计划任务（工作日 09:05，勾选"错过则尽快启动"）
.\register_task.ps1
```

> **若第 7 步报「未对文件进行数字签名」**，用这条命令绕过（不需要改系统策略）：
> ```powershell
> powershell -ExecutionPolicy Bypass -File .\register_task.ps1
> ```

### 怎么拿到飞书 Webhook

1. 在飞书群里点 **设置 → 群机器人 → 添加机器人 → 自定义机器人**
2. 起个名字，复制它给的 **Webhook 地址**（形如 `https://open.feishu.cn/open-apis/bot/v2/hook/xxxx`）
3. 安全设置三选一或叠加：
   - **签名校验**（推荐）：勾选后会给你一个**密钥**，填到 config.json 的 `feishu.secret`
   - **自定义关键词**：让消息必须包含某个词，注意卡片正文得真的含这个词
   - **IP 白名单**：家庭宽带 IP 会变，一般不要用

### 怎么拿到 DeepSeek Key

1. 打开 <https://platform.deepseek.com> → 注册/登录
2. 左侧 **API keys** → 创建 → 复制（形如 `sk-xxxx`，**只显示一次**）
3. 填到 `config.json` 的 `deepseek.api_key`

**成本**：20 条批量一次调用，输入约 400–900 token、输出约 550 token，**约 0.001 元/天**（月成本 2–3 分钱）。

---

## 二、常用命令

| 命令 | 作用 |
|---|---|
| `python daily_digest.py` | 正常运行（计划任务调用的就是它） |
| `python daily_digest.py --test-push` | 只发一条测试卡片，验证 webhook/加签（不写台账、不抓数据） |
| `python daily_digest.py --test-push --dry-run` | 只打印将要发送的报文，**不联网**（连 webhook 都不用填） |
| `python daily_digest.py --dry-run` | 抓取 + 组版 + 摘要 + 打印，**不推送、不写台账** |
| `python daily_digest.py --dry-run --preview` | 额外输出 `logs/preview.md` 预览文件 |
| `python daily_digest.py --selftest` | 离线单元测试（**155 项**，不联网，几秒跑完） |
| `python daily_digest.py --force` | **测试模式**：忽略去重并实际推送，但**不写台账**（正式场景勿用） |
| `python daily_digest.py -v` | 输出调试日志 |
| `.\register_task.ps1 -Status` | 查看计划任务状态与上次运行结果 |
| `.\register_task.ps1 -RunNow` | 立即触发一次计划任务 |
| `.\register_task.ps1 -Time 09:30` | 改时间并重新注册 |
| `.\register_task.ps1 -WithLogonTrigger` | 额外加「登录时补推」（脚本内会自行跳过周末） |
| `.\register_task.ps1 -Unregister` | 卸载计划任务 |

**退出码**：`0` 正常（含「无新内容不推送」「当天已推送过」）；`1` 失败。

---

## 三、日报长什么样

**以中文为阅读主体**，英文只作附注，每条带编号方便核对有没有读漏：

```
📰 每日资讯日报 · 2026年10月16日

🔥 GitHub 热门仓库
1. caveman 用原始人式说话帮编码代理省 65% token，今天涨 433 星        ← 中文主体(可点击)
   原题：JuliusBrussee/caveman（今日 +433 star）                     ← 英文降为灰色小字
   GitHub Trending · 今日涨 433 星 · AI 摘要
2. rea 用代理逆向分析应用行为到原生二进制，今天暴涨 3.2 万星
   原题：morluto/rea（今日 +32,314 star）
─────────────────────────────────────────
🤖 AI 热点
6. OpenAI 借教师节介绍 Study Mode，帮老师用提问引导学生一步步学习
   原题：Teaching students to think and learn through questions
   OpenAI 官方 · AI 摘要
...
📰 其他热点
─────────────────────────────────────────
共 20 条 · 全部已有中文摘要 · 已去重，不会重复推送                  ← 页脚是大白话
```

**版式规则**：
- 有中文摘要 → 中文作链接主体，英文原题退到灰色小字
- 中文源的标题本身是中文 → 直接作主体
- 纯英文且摘要服务不可用 → 标红 `⚠️ 本条未翻译`，**页脚还会告知一共几条未翻译**，不会让你读完才发现看不懂

**构成**：

| 板块 | 条数 | 来源 |
|---|---|---|
| 🔥 GitHub 热门仓库 | 5 | GitHub Trending 当日榜（含今日新增星数） |
| 🤖 AI 热点 | 10 | OpenAI / Google DeepMind / HuggingFace / Hacker News(按分数) / 中文 AI 源 |
| 📰 其他热点 | 5 | TechCrunch / The Verge / Wired / Ars Technica / MIT Tech Review / Lobsters |

---

## 四、数据源（全部免费、无需 API Key，均经实测）

**中文源**：量子位、爱范儿、InfoQ 中文、Solidot、少数派、掘金、阮一峰周刊
**AI 官方/博客**：OpenAI、Google DeepMind、HuggingFace 博客、Simon Willison、GitHub 官方博客、Google AI 博客
**其他热点**：TechCrunch、The Verge、Wired、Ars Technica、MIT Tech Review、Lobsters
**特殊源**：GitHub Trending（HTML 解析）、Hacker News（Algolia 多关键词聚合，24h 内 ≥200 条候选）

> 已实测**不可用**故未纳入：36氪 / 品玩（返回 HTML 网页而非 RSS）、机器之心（畸形 XML）、
> HuggingFace Papers（401）、Anthropic（404）、Meta AI（400）、虎嗅（超时）、VentureBeat（429 限流）。

---

## 五、每天稳定运行的四道保障

| 保障 | 说明 |
|---|---|
| **计划任务已注册** | 周一至周五 **09:05**，勾选「错过计划开始时间后尽快启动」——没开机时开机后会自动补跑 |
| **自动补跑时窗** | 距上次成功推送超过 1 天时，时窗自动放宽：2天→72h、3天→96h、5天→144h，**上限 7 天**，避免几天没开机导致内容因过期而永久漏掉 |
| **失败不丢内容** | 推送失败则不写台账，下次重试同一批；卡片失败自动降级纯文本；纯文本再失败写 `logs/preview.md` |
| **防重入 + 幂等** | 文件锁防两个触发器撞车；台账保证「推过的永不重复」；当天已推送则直接跳过 |

---

## 六、密钥安全（重要）

`config.json` **含明文密钥，已被 `.gitignore` 永久忽略，不会提交到仓库**。

推送前请务必确认：
```powershell
git check-ignore -v config.json     # 应输出 .gitignore:2:config.json
git status --short                  # 不应出现 config.json
```

两种填密钥方式：

**方式 1：直接填 config.json**（最省事，也最不容易出错）
```json
"feishu":   { "webhook": "https://...", "secret": "" },
"deepseek": { "api_key": "sk-...", "model": "deepseek-chat" }
```

**方式 2：环境变量**（密钥不落文件）
```json
"feishu":   { "webhook": "${FEISHU_WEBHOOK}" },
"deepseek": { "api_key": "${DEEPSEEK_API_KEY}" }
```
```powershell
[Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", "sk-...", "User")
```
> ⚠️ **用户级环境变量不会注入已在运行的进程**：设完要**重开终端**。
> 计划任务启动的是新进程，能读到（本项目已实测验证）。

> ⚠️ **Webhook 等同于往你群里发消息的凭证**。若它出现在截图、聊天记录、公开仓库里，
> 请立刻在飞书「设置 → 群机器人」里**重置**并更新 config.json。

---

## 七、排错

**1. 收不到消息？** 看 `logs/digest.log` 里最后一行「推送结果」：

| 错误码 | 原因 | 处理 |
|---|---|---|
| `19001` | webhook token 不对 | 检查 config.json 的 webhook 是否完整 |
| `19021` | 签名校验失败 / 时间戳超 1 小时 | 检查 `feishu.secret` 是否与机器人里复制的**完全一致**（注意别带空格）；校准系统时间 |
| `19022` | IP 不在白名单 | 家庭宽带 IP 会变，建议改用签名校验 |
| `19024` | 自定义关键词未命中 | 把该关键词加进消息正文，或改用签名校验 |
| `11232` | 触发限流 | 把计划任务时间挪到非整点/半点（09:05 已是安全时刻） |

**2. 摘要没生效（英文没变中文）？** 看日志里这行：
```
摘要成功：20/20 条      ← 正常
摘要：LLM 0 条 / 降级 20 条   ← 没生效
```
检查：`config.json` 里 `deepseek.api_key` 是否填了真实 key（`sk-` 开头）；
若用环境变量，**重开终端**再试。

**3. 某个源总是失败？** 在 `config.json` 的 `sources` 里把该源 `"enabled": false`。单源失败不影响整体。

**4. 想手动跑一次看效果？** `python daily_digest.py --dry-run --preview`

**5. 台账想重置（会重新推送历史内容）？** 删除 `data/ledger.json`。

**6. 想看计划任务上次跑得怎么样？**
```powershell
.\register_task.ps1 -Status
Get-Content .\logs\digest.log -Tail 30 -Encoding UTF8
```

---

## 八、文件说明

| 文件 | 作用 |
|---|---|
| `daily_digest.py` | 主程序（入口 / 抓取编排 / 组版 / 离线自检） |
| `digestlib.py` | 核心库（解析 / 去重 / 台账 / 锁 / 卡片 / 加签 / 推送） |
| `config.example.json` | 配置模板，**含全部可调参数与中文说明** |
| `config.json` | 你的实际配置（含密钥，**不提交**） |
| `register_task.ps1` | 计划任务注册/卸载/查看 |
| `docs/` | 技术调研文档（飞书卡片限制、加签算法、同类开源项目对比等） |
| `data/ledger.json` | 去重台账（**推送成功后**才写入，自动创建） |
| `data/daily.lock` | 防重入锁（同一天只跑一次，自动创建） |
| `logs/digest.log` | 追加式日志（自动创建） |
| `logs/preview.md` | `--preview` 输出的 Markdown 预览 |

---

## 九、实现要点

- **零第三方依赖**：`urllib` / `xml.etree` / `re` / `html` / `json` / `hmac` / `hashlib` / `base64` / `logging`，全部标准库
- **RSS/Atom/RDF 三格式**一个解析器通吃（含 RSS1.0 默认命名空间陷阱的处理）
- **XML 容错清洗**：去非法控制字符、转义裸 `&`（部分中文源存在此问题）
- **URL 归一化去重**：过滤 30 个跟踪参数 + 默认端口 + 末尾斜杠；**不对 path 小写化**（GitHub 仓库大小写敏感）
- **AI 关键词词边界匹配**：避免 `ai` 误命中 `said`/`chair`/`domain`
- **飞书加签**：`key=f"{ts}\n{secret}"`、`msg=b""`、秒级时间戳（已用官方文档向量验证）
- **卡片**：实测 26 个元素 / 约 7 KB，远低于元素上限（200）与请求体上限（20 KB），无需分片；**绝不使用 table 元素**（表格上限仅 3~5 个）
- **成功判定**：必须 `body.code == 0`（HTTP 200 也可能是失败）
- **重试**：3 次退避 2/8/30 秒，且每次重建连接（本机网络有间歇性 TLS 断连）
- **不使用 `zoneinfo`**：Windows 无 tz database，统一用 `datetime.now().astimezone()`

---

## 十、许可

个人自用项目。第三方数据源版权归各源站所有，本项目仅做聚合与摘要。
