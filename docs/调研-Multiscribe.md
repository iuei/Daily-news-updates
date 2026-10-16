# Multiscribe-Agent (vinkiYu) 源码调研

> 调研对象：`vinkiYu/Multiscribe-Agent`，默认分支 `master`。全部结论均给出「文件 + 函数/类」证据，未读到的部分明确标注。
> 取码方式：`raw.githubusercontent.com` 直连（GitHub REST API 中途触发未鉴权 403 限流，仓库元信息已在限流前取到）。

## 0. 规模与元信息

| 项 | 值 | 证据 |
| --- | --- | --- |
| 版本 / 语言 | `multiscribe-agent` 1.1.2，Python（`requires-python >= 3.12`） | `pyproject.toml` |
| 许可证 | **GPL-3.0-only**（`license = "GPL-3.0-only"`，README 声明 GPL-3.0；GitHub API 的 `license` 字段为 `null`，未识别到 LICENSE 文件） | `pyproject.toml`、`README.md` |
| 最后更新（API 快照） | `pushed_at=2026-10-04`、`updated_at=2026-10-14`、`created_at=2026-07-16` | GitHub REST `repos/vinkiYu/Multiscribe-Agent` |
| 热度 | 32 stars / 1 fork / 235 commits | GitHub API + 仓库页 HTML |
| 依赖规模 | **主依赖 27 个、dev 15 个、test 1 个、可选项 9 个**（`opentelemetry`×5、`prometheus-client`、`jieba`）；主依赖含 `fastapi`+`uvicorn`+`sse-starlette`（Web）、`asyncpg`+`psycopg`、`redis`、`qdrant-client`、`sentence-transformers`、`haystack-ai`、`langchain*`、`mcp`、`apscheduler` | `pyproject.toml` |
| 体量 | 仓库 762 个 blob（含 `frontend/prototype` TS 前端、`codex/reviews` 60+ 篇评审文档、`docs/phases` 分阶段文档） | GitHub tree API（限流前完整抓取） |
| 容器 | 单阶段 `python:3.12-slim` + `uv sync --frozen`，默认命令 `python -m multiscribe_agent serve --port 8000` | `Dockerfile` |

结论先行：**这是一个「平台」，不是脚本**。它的日报能力只是 `daily_digest` 五节点 DAG 跑在自研工作流引擎 + 插件体系 + Postgres/Redis/Qdrant 之上。可借鉴的是**细节手法**（卡片报文、去重键、锁语义、prompt 规则），不是结构。

### 架构骨架（真实数据流）

```
ScheduleTask(cron="0 9 * * *", config={adapter_ids, fetch_days, top_n, targets})
  └─ SchedulerService.execute_task()            services/scheduler.py:148
       ├─ RedisSchedulerLock.acquire(lock_key, ttl=7200)   services/scheduler_lock.py:77
       ├─ TaskLog(running) → callback(task, run_id=f"{task.id}:{run_date}")
       └─ finally: lock.release(lock_key, token)
            └─ DailyDigestPipeline.run()        agents/pipelines/daily_digest.py:463
                 └─ WorkflowEngine.stream(WORKFLOW_ID)   5 个 WorkflowStep（DAG）
                      ingest  → IngestionService.run_all()（并发 4）→ source_data 表
                      dedupe  → _DailyDigestStepExecutor._dedupe()      :861
                      curate  → CURATE_PROMPT + 质量循环（max_iterations=3, exit_condition=llm）
                      overview→ DIGEST_OVERVIEW_PROMPT → 180 字中文概览
                      fanout  → PublishingService.fanout() → renderer → Publisher.publish()
                                 └─ render_digest_card() renderers/feishu_card.py:25
                                 └─ FeishuPublisher.publish() plugins/builtin/publishers/feishu.py:49
                                 └─ 回写 PublishHistory / PushedContentRepository
```

文件映射（我们关心的部分）：抓取 `plugins/builtin/adapters/*`；过滤 `services/candidate_filter.py`、`services/blocked_sources.py`；渲染 `renderers/feishu_card.py`、`renderers/wecom_markdown.py`；推送 `plugins/builtin/publishers/*.py`；台账 `core/pushed_content.py` + `core/publish_history.py` + `infra/postgres/schema_dedup.py`；调度 `services/scheduler*.py`；prompt `agents/pipelines/prompts.py` + `resources/prompts/digest.md`。

---

## 1. 可直接借鉴的点

### 借鉴点 1：整个卡片就是一个 `header` + `elements` 数组，一条资讯一个 `markdown` 元素
- 出处：`src/multiscribe_agent/renderers/feishu_card.py` :: `render_digest_card`
- 关键代码摘录：
```python
elements: list[dict[str, object]] = [
    {"tag": "markdown", "content": _item_markdown(item)} for item in items
]
if footer is not None and footer.strip():
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": footer}]})
return {"header": {"title": {"tag": "plain_text", "content": title}}, "elements": elements}

def _item_markdown(item: DigestItem) -> str:
    link = f"[{item.title}]({item.url})" if item.url else item.title
    source = f"\n\nSource: {item.source}" if item.source else ""
    score = f" | Score: {item.score:g}" if item.score is not None else ""
    summary = f"\n{item.summary}" if item.summary else ""
    return f"**{link}**{score}{summary}{source}"
```
- 为什么值得借鉴：报文极简、可预测，`elements` 是纯列表，**天然支持按元素数分片**；`footer` 用 `note` 元素承载「日报概览」。照抄时：把 `Source:`/`Score:` 换成我们自己的元信息，把我们没有的 `footer`（LLM 概览）换成「共 20 条 · 数据源统计」。

### 借鉴点 2：长文本一律在「进入报文前」截断，而不是在报文里截
- 出处：`renderers/wecom_markdown.py` :: `_summary`（`MAX_SUMMARY_LENGTH = 300`，`value[:297] + "..."`）；`agents/pipelines/daily_digest.py` :: `CURATE_SUMMARY_MAX_CHARS = 100`（构造 `DigestItem` 时 `summary=_plain_text(...)[:100]`）；`adapters/rss.py` :: `_to_unified_data`（`description=summary[:300]`）
- 为什么值得借鉴：截断点分散在**采集→策展→渲染**三级，每级各有上限（300/100/300），保证下游永远拿不到超长串。我们单文件脚本可直接收敛成 `clip(text, n)` 一个函数 + 三个常量。

### 借鉴点 3：去重键 = `sha256(title + "\n" + description)`，URL 另做归一化，二者同时入库
- 出处：`agents/pipelines/daily_digest.py` :: `digest_content_hash`（`hashlib.sha256(f"{title}\n{description}".encode()).hexdigest()`）；`core/pushed_content.py` :: `_normalize_url`（`value.strip().rstrip("/").casefold()`）；`_dedupe`（同时比对 `seen_urls`/`seen_hashes`/`pushed_urls`/`pushed_hashes`）
- 为什么值得借鉴：**URL 变了但内容一样**（带 utm 参数、改标题的重复投递）和**URL 一样但标题变了**都能各挡一半；双键比单键稳。我们只需把两个 `set` 换成 `shelve`/JSON 台账里的两个列表。

### 借鉴点 4：台账在「发布成功」之后才写，且只在至少一个目标成功时写
- 出处：`daily_digest.py` :: `_fanout`（`if ... and any(result.get("status") == "success" for result in targets.values()): for item in items: await self._pushed_content_repo.add(...)`）
- 为什么值得借鉴：避免「推送失败但已记账 → 第二天再也推不出去」的经典坑。我们照抄这个条件，并额外把「失败项」写进一个 `pending` 列表以便重试。

### 借鉴点 5：`pushed_content` 主键是 `(content_hash, digest_date)`，天然幂等
- 出处：`infra/postgres/schema_dedup.py` :: `PUSHED_CONTENT_PG`；`core/pushed_content.py` :: `add`（`ON CONFLICT DO NOTHING`）
- 为什么值得借鉴：同一天重复跑同一批内容不会产生重复行；`digest_date` 参与主键，同一条内容**隔天可以再推**。SQLite 里等价写法就是 `PRIMARY KEY(content_hash, digest_date)` + `INSERT OR IGNORE`，我们甚至可以直接用 `sqlite3` 标准库——**这是本仓库里唯一能零成本搬到我们方案里的一整块设计**。

### 借鉴点 6：防重入锁用「任务类型 + 日期」做 key，并按业务操作而非任务 ID 加锁
- 出处：`services/scheduler.py` :: `execute_task`（`run_date = now(UTC).strftime("%Y-%m-%d")`；`lock_key = f"multiscribe:scheduler:lock:{task.task_type}:{run_date}"`，注释明确写「cron / 手动 / CLI 三个入口竞争同一把锁」）
- 为什么值得借鉴：同一天无论从计划任务、命令行还是手工点击触发，都只跑一次。我们不需要 Redis：`O_CREAT|O_EXCL` 创建一个 `locks/daily_digest-YYYY-MM-DD.lock` 文件即可等价实现（Windows 上 `os.open(..., os.O_CREAT|os.O_EXCL)` 可用）。

### 借鉴点 7：重试只包「发请求 + 校验响应」，签名与报文在重试之外构造
- 出处：`plugins/base.py` :: `BasePublisher._send_with_retry`（`RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)`，最多 `len(delays)+1 = 4` 次尝试，`await asyncio.sleep(delays[attempt])`）；`publishers/feishu.py` :: `publish`（`timestamp/sign` 在闭包外算好后塞进 `payload`，再交给 `_send_with_retry`）
- 关键代码摘录：
```python
for attempt in range(len(delays) + 1):
    try:
        return await send_func()
    except retry_exceptions as exc:
        last_error = exc
        if attempt >= len(delays):
            break
        await asyncio.sleep(delays[attempt])
raise PublisherError(f"{publisher_name} publish failed after retries") from last_error
```
- 为什么值得借鉴：注释点出了真正的坑——**若签名在重试闭包内计算，重试会生成不同签名**（飞书/钉钉都会因此被判重放或超时）。我们同步脚本用 `time.sleep` + `for attempt in (0,1,2)` 照搬这个分层。

### 借鉴点 8：飞书签名算法完整可抄（一行都不能错）
- 出处：`publishers/feishu.py` :: `gen_sign`
```python
string_to_sign = f"{timestamp}\n{secret}"
digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
return base64.b64encode(digest).decode("utf-8")
```
- 注意：`hmac.new` 的 **key 位置留空**（没有传 secret），因为待签串里已含 secret——这与钉钉相反（见不足 6）。签名时 `payload["timestamp"] = str(int(time.time()))`（**秒级**）。
- 响应判定：`body.get("StatusCode") == 0 or body.get("code") == 0`（`_is_success`），兼容新旧两种返回。

### 借鉴点 9：适配器是可替换的「候选池」，单源失败不拖垮整批
- 出处：`plugins/base.py` :: `BaseAdapter.fetch_and_transform`（`except Exception: log.warning(...); return []`）；`services/ingestion.py` :: `run_all`（`asyncio.Semaphore(max(1, max_concurrency))` + `asyncio.gather(..., return_exceptions=True)`）；`adapters/rss.py` :: `fetch_and_transform`（多 feed 时每源独立，注释「one failed source cannot discard the other sources」）
- 为什么值得借鉴：我们的 5 条 Trending + 10 条 HN/OpenAI/Google + 5 条 TechCrunch/Vergen 恰好是三组来源，**每组独立 try/except 且各自设最低配额**，任何一组挂了还能凑出日报。

### 借鉴点 10：即时窗口「近 N 天 + 7 天兜底 + 快照类来源单独看抓取时间」
- 出处：`daily_digest.py` :: `_ingest`（`fetch_days` 默认 2，`_CONTENT_FALLBACK_DAYS = 7`，`start = run_date-(fetch_days-1)`、`fallback_start = run_date-6`）与 `_recent_daily_candidates`（`_SNAPSHOT_ADAPTER_IDS = frozenset({"github_trending"})`，Trending 用 `fetched_at` 落在窗口内即算「当天」；无发布时间的记录**不入日报**）
- 为什么值得借鉴：GitHub Trending 没有发布时间，用「抓取时间」判定，并给条目打 `metadata["digest_freshness"] ∈ {recent, fallback, snapshot}` 让 LLM 知道该优先谁。

### 借鉴点 11：摘要兜底（标题当摘要）+ 板块配平
- 出处：`daily_digest.py` :: `fallback_summary`（正则去 emoji、压缩空白、取前 150 字，注释说明「否则空摘要会先被 prompt 的低密度规则拒掉」）；`_prioritize_digest_sections`（先按 4 个 section 各取 1 条最高分，再按分数补满 `top_n`）；`_supplement_curated_items`（LLM 选不够时用候选补齐，且 **GitHub Trending 最多 2 条**）
- 为什么值得借鉴：LLM 少选/多选时确定性兜底，保证日报「条数够、板块不空、某来源不霸屏」。我们 20 条配额正好需要这种硬约束。

### 借鉴点 12：测试极轻量（值得抄的验证思路）
- 出处：`tests/renderers/test_feishu_card.py`（**纯字典等值断言**，无网络无 mock：`assert card["header"] == {...}`、`elements[0] == {"tag":"markdown","content":"**[Item 1](...)** | Score: 0.6\nSummary 1\n\nSource: Example"}`、空列表 → `elements == []`）；`tests/core/test_pushed_content.py`（查 `pg_index` 断言主键顺序为 `content_hash=1, digest_date=2`；重复 add 同一 hash+date 只留 1 行且保留**首行标题**；`since_date` 为**闭区间**，大小写/尾斜杠 URL 归一化后相等）；`tests/services/test_scheduler_lock.py`（`FakeRedis` 模拟 `SET NX EX`，断言**陈旧 token 不能删掉别人的锁**）
- 为什么值得借鉴：三个「契约式」测试覆盖了我们最怕出错的点：报文形状、台账幂等+归一化、锁的归属权。我们的单文件脚本同样可以写 3 个 `assert` 跑 `python script.py --selftest`。

---

## 2. 关键问题的答案（必须明确）

### 2.1 飞书交互卡片 JSON 结构？50 元素上限？超长文本？
- **结构**（`render_digest_card`，`bootstrap.py:458` 里 `lambda digest: render_digest_card(digest.title, digest.items, footer=digest.summary)`）：
```json
{"header": {"title": {"tag": "plain_text", "content": "<日报标题>"}},
 "elements": [{"tag": "markdown", "content": "**<标题链接>** | Score: 8\n<摘要>\n\nSource: <来源>"},
              ...,
              {"tag": "note", "elements": [{"tag": "plain_text", "content": "<日报概览 180 字>"}]}]}
```
发送时包成 webhook 信封：`{"msg_type": "interactive", "card": {...}}`（`FeishuPublisher._payload`，会剔除 `webhook` 字段）；纯文本降级为 `{"msg_type":"text","content":{"text":...}}`。`render_digest_text` 是给不支持卡片的通道用的纯文本兜底。
- **50 元素上限：仓库里没有做任何处理。** 我在 `renderers/feishu_card.py` 全文、以及全仓库 grep `truncat` / `[:50]` / `element` 后确认：`elements` 长度 = `len(items) + (1 if footer) `，完全由 `top_n` 决定（默认 12，`bootstrap.py` 把历史默认 5/10 升级为 12，README 建议 10–15 条）。**它是靠「日报只选 12 条」从源头规避上限，而不是靠分片**。对我们的 20 条（20+1 元素）仍在 50 以内，安全；但要在代码里写下这个理由，并留一条 `if len(elements) > 45: 分片` 的保险。
- **超长文本**：三级截断，见借鉴点 2。卡片路径**没有**对「单条 markdown 元素长度」做字节级限制（WeCom 路径做了 300 字）。飞书单元素上限约 30KB，我们按「摘要 ≤ 100 字、标题 ≤ 60 字」自设即可。

### 2.2 去重键如何构造？台账在哪？何时写？
- **键**：`content_hash = sha256(f"{title}\n{description}")`（`digest_content_hash`，`daily_digest.py:118`），**不含 URL、不含来源、不含日期**；`url` 归一化 = `strip().rstrip("/").casefold()`（`core/pushed_content.py:11`）。
- **表/文件**：`pushed_content(content_hash, url, digest_date, pushed_at, title)`，主键 `(content_hash, digest_date)`，索引 `idx_pushed_content_pushed_at`（`infra/postgres/schema_dedup.py:5`）。仓储 `core/pushed_content.py` :: `PushedContentRepository.add/recent_hashes/recent_urls`（`recent_*` 按 `digest_date >= since_date` 取，`since_date = run_date - (fetch_days-1)`）。
- **第二本台账**：`publish_history(publisher_id, digest_date)` 上有 **UNIQUE 索引**（`schema_dedup.py:32`），写入用 `ON CONFLICT(publisher_id, digest_date) DO NOTHING`（`core/publish_history.py:32`）——作用是「同一天同一通道不重复发」，`_filter_already_succeeded_targets` 发前先查这里（`daily_digest.py:1151`）。
- **写入时机**：先去重（读台账）→ 策展 → 发布 → **fanout 里发布成功后写 `pushed_content`**（`daily_digest.py:1103-1119`），同时 `_record_publish_history` 写 `publish_history`（`content_hash` 仅 success 才写）。

### 2.3 调度与防重入锁？能移植到 Windows 计划任务吗？
- 调度：APScheduler `AsyncIOScheduler` + `CronTrigger.from_crontab(task.cron, timezone="Asia/Shanghai")`，任务存在 DB 的 `schedules` 实体里，`start()` 时加载（`services/scheduler.py:88`、`register`、`reload`）。默认 cron `0 9 * * *`（`config.py` `daily_ai_news_cron`）。
- 锁：Redis `SET key token NX EX ttl`（ttl 默认 7200s），释放用 Lua **比对 owner token 才 DEL**（`scheduler_lock.py:17-22`）；`strict_mode=True` 时 Redis 不可用 = 任务报 error 不执行，`False` 时 `allow_without_lock=True` 放行（`AcquireResult`）。异常路径统一降级为 `unavailable` 而不是抛。
- **移植结论：可以，且不需要 Redis。** Windows 计划任务本身已保证单机串行；只需再加一层「同一天只跑一次」的文件锁：`open(lock_path, "x")`（或 `os.open(O_CREAT|O_EXCL)`）成功才执行、失败即退出、`finally` 删锁并在异常时保留锁文件+写日志。要照抄的是**语义**：锁键 = `任务类型 + 本地日期`、失败不静默、释放要有归属校验（文件锁天然满足）。仓库里 `NoOpSchedulerLock` 也证明了「锁是可替换依赖」。

### 2.4 LLM 摘要的 prompt 与批量/降级策略？
- **两段式**：① `CURATE_PROMPT`（`agents/pipelines/prompts.py`）——一次性把**整批候选**（`curate_candidate_limit` 默认 **100** 条，`_curate_item_dict` 投影后 JSON）交给模型，要求**严格 JSON 数组**、每条含 `id/title/summary/score/score_reason/section`；`title` 改写为中文、`summary` 中文 ≤180 字、`score` 1–10 且 **<6 必须剔除**、`section` 只能是四选一；含「读者画像 / 选的标准 / 拒的硬规则 / 边界判断 / 数量约束（≤target_count，目标 10–12，下限规则：≥7 必选 1，只有 ≥6 也要选 1 避免 F1=0）/ 上一轮反馈」六个段落。② `DIGEST_OVERVIEW_PROMPT`——对已选条目写 ≤180 字中文概览，**只返回正文，不许 Markdown**。
- **批量**：单次调用、不做 item 级批处理；靠 `curate_candidate_limit=100` 控制 prompt 体积。
- **降级**（多层，均有代码）：
  1. 无记忆服务时 `CandidateFilter.fallback_rank()` 按 `published_date` 倒序截断（`daily_digest.py:945`）；
  2. 记忆构建抛异常 → `log.warning("daily_digest_memory_degraded")` 后回落 fallback_rank（:941）；
  3. 模型返回的 `id` 不在候选池 → `continue` 丢弃（:977）；
  4. 选不够 → `_supplement_curated_items` 用候选补，摘要取 `description[:180]` 而**不请模型编造**（:1498）；
  5. 选超了 → `_prioritize_digest_sections` 按分数截到 `top_n`；
  6. 空摘要 → `fallback_summary(title)`（去 emoji 取 150 字）。
- 若我们要零 LLM：可直接用 `score_reason`/`score` 的替代物——按来源配额 + 时间倒序 + 关键词命中排序（`CandidateFilter.filter_and_rank` 的 `(tag_matches, published_date)` 就是现成的纯规则实现）。

### 2.5 GitHub Trending 与 HN 的具体获取手段？
- **GitHub Trending：无官方 API，抓 HTML。** `adapters/github_trending.py`：`BASE_URL="https://github.com/trending"`，`quote(language, safe='-')` 拼语言路径、`urlencode({"spoken_language_code": ...})` 拼查询串；`httpx` 带 `User-Agent: MultiscribeAgent/1.0`、超时 30s、`raise_for_status()`；解析用 **selectolax** `HTMLParser`，选择器 **`article.Box-row`**（条目容器）、**`h2 a`**（取 `href` → `full_name = href.strip("/")`）、**`span[itemprop="programmingLanguage"]`**（语言）、**`p`**（描述）、**`a[href$="/stargazers"]`**（星数，文本可能是 `1,234` / `12.3k` / `1.2m`，`_parse_star_count` 去逗号 + 处理 k/m 后缀 + `int(float(x)*multiplier)`）；`published_date` 填死 `1970-01-01T00:00:00+00:00`（`UNKNOWN_PUBLISHED_DATE`），另在 `metadata` 存 `language/stars/repository`，`id=f"github:{full_name}"`，标题格式 `[{language}] {full_name} ({stars:,} stars)`。防 SSRF/注入无关，但**没做重试、没做 HTML 结构漂移告警**（选择器一改就静默返回 `[]`）。
- **HN：官方 RSS，不碰 Firebase API。** `adapters/hacker_news.py` 只是 `RSSAdapter` 的薄子类：`DEFAULT_FEED_URL = "https://news.ycombinator.com/rss"`，通过 `_defaults()` 注入 `source_name="Hacker News"`、`category="Hacker News"`，`transform` 委托 `super().transform(raw, self._defaults(config))`。**没有用 `hacker-news.firebaseio.com/v0/topstories.json`**。
- 顺带：`config.py` 默认 RSS 列表 = HuggingFace daily-papers、`https://tldr.tech/api/rss/ai`、**HN rss**、lastweekin.ai、`https://openai.com/news/rss.xml`、**`https://blog.google/technology/ai/rss/`**、`simonwillison.net/atom/everything/`——即 OpenAI/Google 走的是 RSS，**没有 TechCrunch / The Verge**（这两家需我们自加）。

---

## 3. 它的不足与坑

1. **卡片无元素数保护**：`render_digest_card` 直接把 `len(items)` 个元素塞进 `elements`，无上限校验、无分片（`top_n` 若被配成 60 就会超飞书 50 元素上限，且失败发生在 HTTP 阶段才暴露）。我们要显式分片。
2. **`pushed_content` 不去重 `url` 唯一性**：主键只含 `(content_hash, digest_date)`，同一 URL 标题改写后 hash 变化即可再次入库；`recent_urls` 只在内存 `set` 里挡。
3. **同一天多目标部分失败 → 台账照写**：`_fanout` 只要求 `any(status=="success")` 就为**全部** items 记账，未成功的通道第二天被 `_filter_already_succeeded_targets` 之外的因素影响时可能出现「部分通道永远收不到」的错位（它靠 `publish_history` 的 per-publisher 唯一索引兜底，逻辑分散在两处，易踩）。
4. **Trending 解析脆弱且无告警**：`article.Box-row` / `h2 a` 纯靠 GitHub 页面结构；`transform` 匹配到 0 条时不抛错（`ingestion` 只记 `result_count=0`），日报会静默变瘦。它靠 `AdapterHealth`（连续失败 3 次禁用，`config.adapter_health_failure_threshold=3`）间接发现。
5. **强依赖 `feedparser`**：RSS 解析、`published_parsed`、`tags`、`media_*` 全押在它身上（`adapters/rss.py:11`）。标准库只有 `xml.etree`，我们要自己写 RSS/Atom 命名空间兼容 + 容错，这是**移植成本最高的一块**。
6. **钉钉/飞书签名语义相反**：飞书 `hmac.new(f"{ts}\n{secret}".encode(), digestmod=sha256)`（key 为空、secret 在待签串里），钉钉 `hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), sha256)` 再 `quote_plus`（`dingtalk.py:25`）。照抄时别串。
7. **重架构耦合**：`daily_digest.py` 单文件 **1727 行**，`_DailyDigestStepExecutor` 一处 `__init__` 收 16 个依赖；`bootstrap.py` 942 行负责装配。任何一处想「只改一下推送」都要穿过 DI + 插件注册表 + 工作流引擎。
8. **日志/预览会碰敏感数据**：`PublishHistory.sanitize` 用 9 条正则 redact `sk-`/`bearer`/token+4 个厂商 webhook（`core/publish_history.py:68-78`）——说明默认会把内容预览落库，是它们自己承认的风险点，我们要避免落库。
9. **时间语义混用**：DB 里时间存 TEXT（ISO 字符串）与 `DOUBLE PRECISION`（epoch）两种（`schema_dedup.py` 的 `pushed_at TEXT` vs `recorded_at DOUBLE PRECISION`），比较靠字符串字典序，只对同格式 ISO 成立。
10. **未取到的部分**（限流）：`tests/renderers/` 之外的渲染测试、`renderers/` 目录完整清单（我通过 GitHub 页面拿到了 `services/` 列表，但 `renderers/` 列表未取到）、`eval/curation_benchmark.py` 全文、`data/eval/datasets/curation_recall.yaml` 内容（已下载 12KB 未逐行读）。这些不影响上述结论。

---

## 4. 与我们需求的差距

### 4.1 多出来的（明确不要）
| 它有 | 我们有 | 处理 |
| --- | --- | --- |
| FastAPI + uvicorn + SSE + 认证/CSRF/限流（`api/` 21 个路由） | 无 Web | 全砍 |
| Postgres（`asyncpg`）/ SQLite 双方言 + 方言抽象 + 启动期幂等 DDL | 单文件脚本 | 换成 `sqlite3` 单表或 JSON 台账；连 PK 语义都保留 |
| Redis 分布式锁 + APScheduler | Windows 计划任务 | 换文件锁 + 系统调度 |
| Qdrant + sentence-transformers + haystack/langchain（RAG、记忆、知识库） | 无 | 全砍；`CandidateFilter.fallback_rank` 的纯规则排序替代 |
| MCP 服务、插件发现/沙箱/签名、Skill 目录 | 无 | 全砍 |
| 自研 WorkflowEngine + 循环评审（reflexion）+ 迭代存储 + 评测基准 | 无 | 5 个顺序函数替代 DAG |
| 4 个板块、图片抓取（`og:image`/`twitter:image`，8s 超时并发抓取） | 不需要？ | 按需保留图片解析（标准库 `html.parser` 即可，无需 selectolax） |
| 9 个 dev + 5 个 otel 依赖、GPL-3.0 | 零第三方依赖 | **注意：只借鉴思路/算法，不复制代码文件**（GPL 传染性） |

### 4.2 缺的（我们要自己补）
1. **TechCrunch / The Verge 的取法**：它只有通用 `RSSAdapter`，没有这两家。我们用同一套 `feedparser` 思路改造：`https://techcrunch.com/feed/`、`https://www.theverge.com/rss/index.xml`（Atom）——**需自行验证 URL 与命名空间**，仓库里无证据。
2. **固定 5/10/5 的来源配额**：它是「LLM 自由选 + GitHub ≤2 条 + 每 section ≥1 条」。我们要的是硬配额（Trending 5 / HN+OpenAI+Google 10 / TC+Verge 5），比它的规则更简单，但要自己写「配额不足时如何回填」。
3. **零依赖 XPath/HTML 解析**：selectolax → `html.parser.HTMLParser` 子类（它自己就在 `daily_digest.py:1568` 写了 `_ArticleImageParser(HTMLParser)` 作为范例，可参考写法）。
4. **零依赖 RSS/Atom 解析**：`feedparser` → `xml.etree.ElementTree` + 命名空间处理 + `email.utils.parsedate_to_datetime`；容错策略照抄它的「缺 id 就跳过（`_to_unified_data` 中 `_first_non_empty(guid, link, id)` 为空则记 `rss_entry_skipped` 并 return None）、缺 title 用 `(untitled)`、缺日期兜底 `1970-01-01T00:00:00+00:00` 且视为过期不入选」。
5. **单文件可 `--selftest`**：照抄 `tests/renderers/test_feishu_card.py` 的字典等值断言 + `tests/core/test_pushed_content.py` 的幂等/归一化断言。

### 4.3 冲突的
- **架构哲学冲突**：它把「可配置/可插拔/可观测」放在第一位，我们只要「一台 Windows 机器、`python 脚本.py`、跑完退出」。任何 DI/Protocol/Port 都是为了多实例与多租户，我们照抄会直接毁掉可读性。
- **数据流冲突**：它是「先落库再读库」的两段式（`ingestion` 落 `source_data`，`_ingest` 再从库里按时间窗读），这是为了跨天复用与断点续跑；我们不需要 DB 中转，应当**内存直传**（fetch → normalize → filter → render → push 一条流水线）。
- **许可证冲突**：GPL-3.0-only。**可借鉴算法与参数（去重键公式、三级截断上限、`(1,2,4)` 退避、锁键命名、prompt 的拒选规则），不要整段复制源码**；如果一定要复制，需评估 GPL 传染（我们的脚本若分发则需 GPL 兼容）。

### 4.4 一句话结论
它对我们最大的价值是**三张「已经踩过坑的参数表」**：① 飞书报文的形状与签名（`feishu_card.py` + `gen_sign`，可直接重写实现）；② 去重台账的双键 + `(hash, date)` 幂等主键 + 「发布成功后才写」（`pushed_content.py` + `schema_dedup.py`，可用 `sqlite3` 1:1 复刻）；③ 时间窗与配额兜底（近 2 天 + 7 天兜底 + 快照来源单列 + 不足时用候选补齐 + 单来源限 2 条）。其余（DAG、插件、Postgres/Redis/Qdrant、记忆/知识库、评测）与我们的目标正交，应整体舍弃。
