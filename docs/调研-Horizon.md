# Horizon (qqhayate/Horizon) 源码调研

- **仓库规模/语言/许可证/最后更新**（GitHub API `/repos/qqhayate/Horizon`，限流前取到）：`language=Python`，`license=MIT`，`size=9750` KB，`created_at=2026-06-13`，`updated_at=2026-06-14`，`pushed_at=2026-10-16T01:11:58Z`，`default_branch=main`，`stars=0 / forks=0 / open_issues=0`，homepage `horizon1123.top`，tree 共 124 条目，描述“📡 Your own AI-powered news radar”。
- **依赖情况**（`pyproject.toml`，`requires-python>=3.11`）：`httpx`、`feedparser`、`pydantic>=2.9`、`python-dateutil`、`tenacity`、`python-dotenv`、`rich`、`beautifulsoup4`、`markdown`、`ddgs`(DuckDuckGo 搜索)、`mcp`；AI SDK 三套全装：`anthropic`、`openai`、`google-genai`；可选组 `openbb`、`twitter`(playwright)。入口脚本 4 个：`horizon=src.main:main`、`horizon-mcp`、`horizon-wizard`、`horizon-webhook`。
- **架构骨架**（实际数据流，名称均为源码原名）：
  1. `src/main.py:main()` → `load_dotenv()` → `StorageManager(data_dir="data")` → `load_config()` → `asyncio.run(HorizonOrchestrator.run(force_hours=args.hours))`；CLI 只有 `--hours` 一个参数。
  2. `src/orchestrator.py:HorizonOrchestrator.run()`：`_determine_time_window` → `fetch_all_sources`(并发 `Scraper.fetch`) → `merge_cross_source_duplicates`(URL 归一化) → `_analyze_content`(`ContentAnalyzer.analyze_batch` 逐条打分) → 阈值过滤 + `ai_score` 降序 → `merge_topic_duplicates`(AI 语义去重) → `_expand_twitter_discussion` → `apply_balanced_digest`(分组配额 + `max_items`) → `_enrich_important_items`(`ContentEnricher.enrich_batch`：联网搜索 + 第二次 AI 生成双语详情) → 每语言 `DailySummarizer.generate_summary` → `StorageManager.save_daily_summary` → `WebhookNotifier.send_daily_summary`；异常时 `WebhookNotifier.send_failure`。
  3. 抓取层：`BaseScraper`(ABC) → `github`/`hackernews`/`rss`/`reddit`/`telegram`/`twitter`/`openbb`/`ossinsight` 共 8 个 Scraper，统一产出 `models.ContentItem`。
  4. **没有跨日去重台账**：`StorageManager` 只写 config/summaries/subscribers（详见 §2、§3）。

## 1. 可直接借鉴的点（逐条）
### 借鉴点 1：`#{key?limit=N&split=DELIM}` 模板占位符 + 按“段”截断
- 出处：`src/services/webhook.py` 的 `_render()`、`_PLACEHOLDER_RE`、`_prepare_variables_for_body()`、`_truncate(value, limit, split)`
- 关键代码（摘录）：
```python
_PLACEHOLDER_RE = re.compile(r"#\{(\w+)(\?\w+=[^}]+)?\}")
def _render(template, variables):
    if isinstance(template, dict): return {k: _render(v, variables) for k, v in template.items()}
    if isinstance(template, list): return [_render(i, variables) for i in template]
    if isinstance(template, str):
        def _replace(m):
            value = variables.get(m.group(1))
            return m.group(0) if value is None else str(value)   # 缺失变量原样保留
        return _PLACEHOLDER_RE.sub(_replace, template)
    return template                                             # int/float/None 原样
# _truncate：以 split 分段丢弃整段；第一段即使超限也保留
for seg in value.split(split):
    n = len(seg) + (len(split) if kept else 0)
    if kept and cur + n > limit: break
    kept.append(seg); cur += n
return split.join(kept)
```
- 为什么值得借鉴：飞书卡片 JSON 整块放进 `config.json`，Python 只提供 `#{date}/#{summary}/#{important_items}`，改卡片不用改代码；dict 形态 + `json.dumps(ensure_ascii=False)` 天然解决中文与引号转义（测试 `test_post_request_with_dict_body_and_special_chars` 专测）。变量缺失保留占位符，比 `str.format` 抛 KeyError 好排错；按段截断不会把某条摘要切成半句话，配置写 `#{summary?limit=20000&split=---}` 即可。
- 我们照抄时的改动点：换 `json` + `urllib.request`；两函数原样搬（纯标准库），但要保留“第一段超限也保留”的语义（`test_single_segment_exceeds_limit_still_kept`）并在外层加硬上限兜底。
### 借鉴点 2：飞书 Card JSON 2.0 `collapsible_panel` 折叠日报结构
- 出处：`src/services/webhook.py` 的 `_build_feishu_collapsible_body()`、`_collapsible_panel()`、`_markdown()`、`_text()`
- 关键代码（摘录）：
```python
def _collapsible_panel(title, content):
    return {"tag": "collapsible_panel", "expanded": False,
        "header": {"title": {"tag": "plain_text", "content": title},
                   "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined",
                            "size": "16px 16px"},
                   "icon_position": "right", "icon_expanded_angle": -180},
        "border": {"color": "grey", "corner_radius": "5px"},
        "elements": [{"tag": "markdown", "content": content}]}
# 面板标题 f"{index}. {title} ⭐️ {score}/10"；外层 {"msg_type":"interactive","card":{"schema":"2.0",
#   "config":{"wide_screen_mode":True,"update_multi":True},"header":{…,"template":"blue"},"body":{"elements":[overview, …]}}}
```
- 为什么值得借鉴：20 条资讯一条消息会刷屏；折叠面板 = 一条消息里 20 个可展开条目，标题行 `f"{index}. {title} ⭐️ {score}/10"` 正好匹配“每日 20 条”。`schema:"2.0"` 是飞书官方卡片结构。
- 我们照抄时的改动点：只保留 `msg_type=interactive` + `card` 骨架，`_text`/`_markdown` 两个 helper 直接复制。
### 借鉴点 3：HTTP 2xx 也要检查响应体里的平台错误码
- 出处：`src/services/webhook.py` 的 `_check_body_error_code()` + `_handle_response_status()`
- 关键代码（摘录）：
```python
if 200 <= status < 300:
    error_hint = self._check_body_error_code(body)  # 飞书 code≠0 / 钉钉 errcode≠0 / Slack ok=false
    if error_hint: logger.warning(...)              # 黄色告警而非绿色成功
```
- 为什么值得借鉴：飞书自定义机器人 token 失效时返回 **HTTP 200 + `{"code":19001,...}`**。只看 `status_code` 会误判成功、静默漏报一天；测试 `test_2xx_feishu_error_code_prints_yellow_warning` 就用了 `code=19001` 这个真实场景。
- 我们照抄时的改动点：`Console.print` 换成写本机日志文件/`print`，判定逻辑原样。
### 借鉴点 4：URL 归一化作跨源去重，保留内容最丰富那条并合并元数据
- 出处：`src/orchestrator.py` 的 `merge_cross_source_duplicates()`（内嵌 `normalize_url`）
- 关键代码（摘录）：
```python
def normalize_url(url):
    p = urlparse(str(url)); host = p.hostname or ""
    if host.startswith("www."): host = host[4:]
    return f"{host}{p.path.rstrip('/')}"           # 丢 scheme/query/fragment
primary = max(group, key=lambda x: len(x.content or ""))
for item in group:                                  # 合并 metadata + 追加评论正文
    if item is not primary and item.content and item.content not in primary.content:
        primary.content += f"\n\n--- From {item.source_type.value} ---\n" + item.content
```
- 为什么值得借鉴：同一条新闻（HN + RSS + GitHub release）会重复占配额。纯标准库 `urllib.parse` 即可，不哈希 URL，保留可读性便于排查。
- 我们照抄时的改动点：去掉 httpx/pydantic，条目用 dict 表示。
### 借鉴点 5：分组配额 + 全局封顶，正好就是“20 条配额”的实现
- 出处：`src/orchestrator.py` 的 `apply_balanced_digest()` + `BalancedDigestResult`；配置 `src/models.py:FilteringConfig` / `CategoryGroupConfig`
- 关键代码（摘录）：
```python
sorted_items = sorted(items, key=lambda i: i.ai_score or 0, reverse=True)
for item in sorted_items:                          # 先按分组限流
    category = item.metadata.get("category")
    group_key = category_to_group.get(category, default_group) if isinstance(category, str) else default_group
    limit = groups[group_key].limit if group_key in groups else filtering.default_group_limit
    if limit is not None and group_counts[group_key] >= limit: continue
    selected.append((item, group_key)); group_counts[group_key] += 1
if max_items is not None: selected = selected[:max_items]    # 全局封顶在分组之后
```
- 为什么值得借鉴：算法极简（一次遍历 + 计数器）、确定性可测、不需要 AI；高分的先占位，天然实现“质量优先 + 来源均衡”。`max_items=20` + `default_group_limit` 就是我们要的配额模型。
- 我们照抄时的改动点：`metadata["category"]` 换成我们自己的来源标签（`hackernews`/`github`/rss 名称）；照抄“同一 category 出现在多个组时取配置中第一个并告警”的语义（`duplicate_categories`）。
### 借鉴点 6：`parse_json_response()` 五级降级解析 LLM 输出
- 出处：`src/ai/utils.py:parse_json_response()`（被 `analyzer.py`、`enricher.py`、`orchestrator.merge_topic_duplicates` 复用）
- 关键代码（摘录）：
```python
try: return json.loads(text)                        # 1 直接解析
except (json.JSONDecodeError, ValueError): pass
if "```json" in text: ...                           # 2 ```json 代码块
if "```" in text: ...                               # 3 普通代码块
start = text.find("{")                              # 4 手写花括号配平
if start != -1:
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{": depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0: try: return json.loads(text[start:i+1]) ...
match = re.search(r"\{[\s\S]*\}", text)             # 5 正则兜底
```
- 为什么值得借鉴：LLM 常加 “Here is the JSON:” 或 markdown 围栏；五级降级纯 `json`+`re`，0 依赖，可直接复制。
- 我们照抄时的改动点：原样照抄。
### 借鉴点 7：单源失败隔离 —— 每源 `try/except` 返回 `[]`
- 出处：`src/orchestrator.py:fetch_all_sources()`、`src/scrapers/rss.py:_fetch_feed()`（双层 except）、`src/scrapers/hackernews.py:fetch()` 的 `except httpx.HTTPError: return []`
- 关键代码（摘录）：
```python
results = await asyncio.gather(*tasks, return_exceptions=True)
for result in results:
    if isinstance(result, Exception): self.console.print(f"[red]Error fetching source: {result}[/red]")
    elif isinstance(result, list): all_items.extend(result)
```
- 为什么值得借鉴：Windows 计划任务最怕一个 RSS 挂掉导致整轮无输出；分源 try/except（吞异常 + 记日志）+ 顶层照常出报是正确姿势。
- 我们照抄时的改动点：去掉 asyncio，改同步 `for src: try/except: continue`；异常粒度保持“每源一个 except”。
### 借鉴点 8：RSS 时间字段兼容 + 确定性 ID
- 出处：`src/scrapers/rss.py` 的 `_parse_date()`、`_fetch_feed()`(ID 生成)、`_extract_content()`
- 关键代码（摘录）：
```python
for field in ["published", "updated", "created"]:      # 三字段依次尝试
    if field in entry:
        try:
            if f"{field}_parsed" in entry and entry[f"{field}_parsed"]:
                return datetime.fromtimestamp(calendar.timegm(entry[f"{field}_parsed"]), tz=timezone.utc)
            return parsedate_to_datetime(entry[field])  # RFC822 字符串兜底
        except Exception: continue
return None                                             # 无时间 → 丢弃该条
feed_id = str(source.url).split("//")[1].replace("/", "_")
id = f"rss:{feed_id}:{hashlib.sha256(str(entry_id).encode()).hexdigest()[:16]}"
# 例：rss:example.com_feed.xml:5e2d5d1e58e94d76
```
- 为什么值得借鉴：Atom 用 `updated`、RSS 用 `pubDate`、部分源只有 `created`，三字段回退是必需品；`*_parsed` 结构体配 `calendar.timegm` 比字符串解析稳。`sha256(entry.id or entry.link)[:16]` 做 ID **幂等**，正是跨日去重台账的键基础（`tests/test_rss.py::test_rss_ids_are_deterministic` 断言两次抓取 ID 相同并给出字面量期望值）。
- 我们照抄时的改动点：**`feedparser` 必须换掉** —— 用 `xml.etree.ElementTree` 解析 `rss/channel/item` 与 Atom `feed/entry`，复刻三字段回退；`email.utils.parsedate_to_datetime` 是标准库可留。注意 `feedparser` 容错极强，标准库解析畸形 XML 会直接抛错，必须逐 feed `try/except`。
### 借鉴点 9：HN 走 Firebase 官方只读接口（避开 RSS 的 419/SSL）
- 出处：`src/scrapers/hackernews.py` 的 `fetch()`、`_fetch_story()`、`_fetch_comments()`、`_parse_story()`
- 关键代码（摘录）：
```python
self.base_url = "https://hacker-news.firebaseio.com/v0"
story_ids = (await self.client.get(f"{self.base_url}/topstories.json")).json()[:fetch_top_stories]
stories = await asyncio.gather(*[self._fetch_story(s) for s in story_ids], return_exceptions=True)
if story.get("score", 0) < min_score: continue           # min_score 默认 100
if published_at < since: continue
comment_ids = story.get("kids", [])[:TOP_COMMENTS_LIMIT]  # TOP_COMMENTS_LIMIT = 5
# metadata: {"score", "descendants", "discussion_url": f"https://news.ycombinator.com/item?id={id}", "comment_count"}
# 评论正文：text = re.sub(r'<[^>]+>', ' ', text).strip()；超过 500 字 → text[:497] + "..."
```
- 为什么值得借鉴：`/v0/topstories.json` + `/v0/item/{id}.json` 是 HN 官方公开只读 API，**无需 key、没有 419、没有 RSS 的 TLS 兼容问题**。热度用 `score`（`min_score` 前置裁剪省请求）+ `descendants` 作权重，`discussion_url` 单独存 metadata 供卡片展示；评论一条正则去 HTML + 硬截 500 字，控制喂 LLM 的长度。
- 我们照抄时的改动点：`asyncio.gather` → `concurrent.futures.ThreadPoolExecutor`（或直接串行前 30 条）；`httpx` → `urllib.request` + 自定义 `User-Agent`。
### 借鉴点 10：`${VAR}` 递归展开，配置里不落任何密钥
- 出处：`src/storage/manager.py:_expand_env_vars()` + `_ENV_VAR_PATTERN`；同一约定在 `src/scrapers/rss.py:_fetch_feed()` 对 feed URL 再实现一遍
- 关键代码（摘录）：
```python
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
def _expand_env_vars(value):
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):  return {k: _expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):  return [_expand_env_vars(v) for v in value]
    if isinstance(value, tuple): return tuple(_expand_env_vars(v) for v in value)
    return value
```
- 为什么值得借鉴：飞书 webhook URL 本身就是凭证，`"url": "${FEISHU_WEBHOOK_URL}"` 让 `config.json` 可随便备份/转发；未设置的变量**保留原样**（`${MISSING}` 原样留下），下游报错清晰，不会静默变空串把请求打到错误地址。RSS URL 也能带 `?key=${LWN_KEY}`。
- 我们照抄时的改动点：原样照抄（纯标准库）；不引 python-dotenv，直接读 `os.environ`。
### 借鉴点 11：飞书不认的 HTML 要扁平化，并对链接做 scheme 白名单（防注入）
- 出处：`src/ai/markdown_utils.py:clean_app_summary_markdown()`、`_convert_details_to_markdown()`、`_is_safe_markdown_link_url()`；调用点 `src/services/webhook.py:_format_markdown_for_webhook()`
- 关键代码（摘录）：
```python
_SAFE_URL_SCHEMES = {"http", "https", "mailto"}
def _is_safe_markdown_link_url(value):
    if _CONTROL_CHAR_RE.search(value): return False
    if _UNSAFE_MARKDOWN_URL_CHAR_RE.search(value): return False   # [\s<>\[\]\\]
    if value.count("(") != value.count(")"): return False
    try: parsed = urlsplit(value)
    except ValueError: return False
    if parsed.scheme.lower() not in _SAFE_URL_SCHEMES: return False
    if parsed.scheme.lower() in {"http","https"}: return bool(parsed.netloc)
    return bool(parsed.path)
def clean_app_summary_markdown(value):
    value = _ANCHOR_ID_RE.sub("", value)                # 去掉 <a id="item-1"></a> 锚点
    return _convert_details_to_markdown(value)          # <details><summary>…</summary><ul><li>
                                                        #   → **References** + "- [x](url)"
```
- 为什么值得借鉴：给浏览器看的 markdown 可以写 `<details>`，飞书卡片不渲染 HTML。转换时不校验 `href`，RSS 里一条 `<a href="javascript:alert(1)">` 就会被拼成 `[click](javascript:alert(1))` 注入卡片。测试 `test_details_references_with_unsafe_href_remain_plain_text` 用 `javascript:` 与畸形 href 各测一次，把标签降级为纯文本并转义 `[`/`]`。
- 我们照抄时的改动点：若摘要纯 AI 生成、不含 HTML，可只保留 `_SAFE_URL_SCHEMES` 校验，`<details>` 转换可省。
### 借鉴点 12：测试手法 —— 用 `__new__` 绕过 `__init__` 做纯函数级测试
- 出处：`tests/test_balanced_digest.py` 的 `make_orchestrator()`；`tests/conftest.py` 把仓库根注入 `sys.path`
- 关键代码（摘录）：
```python
def make_orchestrator(filtering):                    # HorizonOrchestrator.__new__(HorizonOrchestrator)
    o = HorizonOrchestrator.__new__(HorizonOrchestrator)
    o.config = SimpleNamespace(filtering=filtering); o.console = Console(record=True)
    return o                                         # Console(record=True) 可 export_text() 断言日志
```
- 为什么值得借鉴：`apply_balanced_digest` 只依赖 `self.config.filtering` 与 `self.console`，用 `__new__` 跳过 `EmailManager`/`WebhookNotifier` 构造，测试无需 API key、无需网络，秒级跑完；`Console(record=True)` + `export_text()` 把“配额冲突取第一个组”这类隐性规则写成断言。`test_run_applies_balanced_digest_before_enrichment` 更用 `monkeypatch.setattr` 替换 pipeline 各阶段，断言送进 enrichment 的 id 只有 `["ai"]`，**锁死“配额必须在 enrichment 之前”这一性能关键点**。
- 我们照抄时的改动点：把 `apply_balanced_digest(items, filtering)` 写成纯函数（非类方法），直接喂 dict 列表断言即可。

## 2. 关键问题的答案

- **GitHub Trending 用什么方式拿？有没有官方 API？解析器是什么？选择器/正则长什么样？失效风险？**
  **答案：Horizon 根本不抓 GitHub Trending 页面，“未找到”任何 trending HTML 解析代码。** `src/scrapers/github.py` 全程走 **GitHub 官方 REST API**（`self.base_url = "https://api.github.com"`），只做两类事：`_fetch_user_events()` → `GET /users/{username}/events/public`（过滤 `PushEvent/CreateEvent/ReleaseEvent/PublicEvent/WatchEvent`）、`_fetch_repo_releases()` → `GET /repos/{owner}/{repo}/releases`；Header 为 `{"Accept": "application/vnd.github.v3+json", "User-Agent": "Horizon-Aggregator"}`，可选 `Authorization: token $GITHUB_TOKEN`（`.env.example` 注明未带 token 60 次/小时、带 token 5000 次/小时）。Trending 的替代方案是 `src/scrapers/ossinsight.py:OSSInsightScraper`，打 `https://api.ossinsight.io/v1/trends/repos?period=past_24_hours&language=All`，取 `payload["data"]["rows"]`，把 `stars` 当“周期内新增 star”排序后按 `stars_gained` 降序截 `max_items`；`docs/configuration.md` 原话：“without needing to scrape GitHub Trending or query BigQuery”。全仓库唯一使用 HTML 解析器的地方是 `src/scrapers/telegram.py`（`from bs4 import BeautifulSoup` + `BeautifulSoup(html, "html.parser")`），**没有任何 CSS selector、没有 trending 正则**。
  **失效风险**：OSS Insight 是无 key 公开接口，文档注明 `past_7_days` 上游已坏；GitHub API 未鉴权 60/h 是硬限制，本次调研已实测触发 403 rate limit。若我们要拿 trending，只能自研 HTML 解析，**Horizon 无法提供可借鉴实现**。
- **HN 用哪个接口？如何避免官方 RSS 的 419/SSL 问题？**
  用 **Firebase 官方只读 API**（`https://hacker-news.firebaseio.com/v0`），不是 Algolia、不是官方 RSS：`/topstories.json` 取 ID 列表 → 取前 `fetch_top_stories`（默认 30，示例配置 20）→ 并发 `/item/{id}.json` 取详情 → `kids[:5]` 再并发取一级评论。热度靠 `score`（`min_score` 默认 100 前置裁剪）+ `descendants`，评论数写进 `metadata.comment_count`。它压根不请求 `news.ycombinator.com/rss`，**从根上绕开 419/SSL**；唯一 HTML 接触点是评论正文 `re.sub(r'<[^>]+>', ' ', text)` 粗剥标签后截 500 字。
- **去重键如何构造？台账文件格式？写入时机（推送前还是推送后）？**
  **没有跨次运行的台账文件 —— “未找到”任何 seen-items/去重台账持久化。** `StorageManager`（`src/storage/manager.py`）只写 `data/config.json`(+`.bak`)、`data/summaries/horizon-{date}-{lang}.md`、`data/subscribers.json`，无 item 级状态。去重全在单次运行内存里分两层：① `merge_cross_source_duplicates()` 用 `normalize_url()`（`host + path.rstrip('/')`，去 www/scheme/query/fragment）分组，保留 `len(content)` 最长者并合并 metadata/正文；② `merge_topic_duplicates()` 把全部标题+标签+摘要**一次**喂给 AI（`TOPIC_DEDUP_SYSTEM/USER`），返回 `{"duplicates": [[主索引, 重复索引...]]}`，索引是**列表下标**而非 ID。条目级唯一键是 `BaseScraper._generate_id(source, subtype, native_id)` → `f"{source}:{subtype}:{native_id}"`（RSS 的 native_id = `sha256(entry.id or entry.link)[:16]`）。重复控制实际靠 `filtering.time_window_hours`（默认 24）滑窗，同一窗口重跑会重复推送。写入时机上，摘要在抓取/分析/去重全部完成后、推送**之前**落盘（`save_daily_summary` 先于 `send_daily_summary`），因此推送失败不影响日报文件。
- **LLM 调用是批量一次还是逐条？超时/token 上限/失败降级怎么处理？**
  **主链路逐条**：`ContentAnalyzer.analyze_batch()` 对每条 item 各发一次 `client.complete()`（`asyncio.Semaphore(analysis_concurrency)` 控并发、默认 1；`throttle_sec` 逐条 sleep；单条包 `@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2,max=10))`）。**个别阶段批量一次**：`merge_topic_duplicates` 全部标题一次调用；`ContentEnricher._extract_concepts` 每条一次 + `_enrich_item` 每条一次（2 次/条，`enrichment_concurrency` 默认 1）。长度控制：分析时 content 截 800/1000 字、评论截 1500 字；补充时 content 截 4000 字、评论截 2000 字；`max_tokens` 默认 4096、`temperature` 0.3；OpenAI 兼容路径强制 `response_format={"type":"json_object"}`（MiniMax 除外，且 temperature≤0 时强制 0.01）。失败降级四层：① 单条异常 → `ai_score=0.0`、`ai_reason="Analysis failed"`、`ai_summary=item.title`（不丢条目，只会被阈值筛掉）；② 解析失败 → `parse_json_response` 五级降级，仍失败则同 ①；③ enrichment 解析失败 → 调 `_translate_item()` 只翻标题+摘要；④ 多供应商 `ChainedAIClient`（`provider_chain` 逗号分隔）顺序 fallback，`_should_fallback()` 仅在 429/rate limit、401/403/quota/exceeded、502/503/service unavailable、空响应时切换，其它异常直接抛。另有两个“学习式”兼容：`OpenAIClient._supports_temperature` 首次 400 后不再发送 `temperature`；`AzureOpenAIClient._token_fallback_mode` 在 `max_tokens` ↔ `max_completion_tokens` 间自动切换。Token 由 `src/ai/tokens.py:record_usage()` 记账并汇总打印。
- **飞书 webhook 报文体长什么样？是否实现了签名？重试策略？**
  报文体完全来自 `data/config.example.json` 的 `webhook.request_body`：`msg_type=interactive` + `card.schema="2.0"` + `header.title` + `body.elements[{tag:markdown},{tag:hr},{tag:markdown}]`；折叠模式由 `_build_feishu_collapsible_body()` 现场构造（`msg_type:"interactive"`、`card.schema:"2.0"`、`config:{wide_screen_mode:True, update_multi:True}`、`header.template:"blue"`、`body.elements` = overview markdown + N 个 `collapsible_panel`）。**签名：未找到。** 全仓库没有任何 `timestamp + "\n" + secret` 的 HMAC-SHA256 计算（`hashlib` 只出现在 RSS 的 ID 生成里）；`_SENSITIVE_HEADER_RE` 里的 `signature` 只用于日志脱敏 `redact_headers()`，`_extract_headers()` 支持的手写 header 里也没有签名字段；`url_env` 只放 URL（token 在 query 里）。**重试：未找到。** `notify()` 里 `async with httpx.AsyncClient(timeout=30.0)` 单次 GET/POST，捕获 `InvalidURL/ConnectError/TimeoutException/Exception` 后只打印并记日志，**不重试、不抛出**；响应按 2xx/3xx/4xx/5xx 分级打印，2xx 内再查 body 错误码。另做了日志安全：`redact_url()` 去 query/fragment、`redact_headers()` 打码敏感头、`_validate_url()` 会剥掉 shell 转义残留 `\? \= \& \%`。
- **分组配额/补齐逻辑怎么实现的？**
  在 `src/orchestrator.py:apply_balanced_digest()`（配置 `FilteringConfig.category_groups` / `default_group` / `default_group_limit` / `max_items`）。步骤：① 若 `category_groups` 为空且 `max_items is None` → 直接返回原列表（`enabled=False`，保持旧行为）；② 按 `ai_score` 降序重排；③ 展开每个 `group.categories` 成 `category_to_group` 映射，同一 category 属多组时**保留配置顺序第一个**并记入 `duplicate_categories` 打黄字告警；④ 单次线性遍历，`item.metadata["category"]` 命中分组用 `group.limit`，未命中用 `default_group`(默认 `"other"`) 的 `default_group_limit`，`limit is None` 表示不限，超限 `continue` 丢弃；⑤ 最后统一 `selected[:max_items]` 全局封顶；⑥ 返回 `BalancedDigestResult(items, enabled, group_counts, group_limits, duplicate_categories)`。**没有任何“补齐”逻辑** —— 某组不足额不会被别组填充，也不回填被丢弃的条目，最终条数 ≤ `max_items` 且可能远小于目标。日志形如 `⚖️ Balanced digest selected {n}/{total} items` 加每组 `{实际}/{limit}`。`FilteringConfig` 用 pydantic 约束 `max_items>0`、`default_group_limit>0`、`CategoryGroupConfig.limit>0` 且 `categories` 非空（`min_length=1`），对应测试 `test_balanced_digest_config_rejects_non_positive_or_empty_limits`。执行位置在阈值过滤 + 主题去重**之后**、enrichment **之前**，目的是省 LLM 调用。

## 3. 它的不足与坑（我们不学的地方）

1. **没有跨日去重台账 + 配额只截断不补齐**：只靠 24h 滑窗，同日重跑或窗口重叠必然重复推送（须自建 `seen.json`，键可沿用它的 `rss:{host}_{path}:{sha256(entry_id)[:16]}` 风格，并在**推送成功后**写入）；`apply_balanced_digest` 分组配额不足时不会互相填充，也没有“最少 N 条”下限，最终条数不确定，要“稳定 20 条”需在其上加“不足则按分从剩余池回填”。
2. **飞书无签名、无重试**：`notify()` 单次调用，网络抖动即当日漏报；`send_daily_summary` 在消息循环里 `await notify()`，失败仅打印。必须自补 HMAC-SHA256 签名与指数退避重试（3 次）。
3. **`asyncio` 渗透全链路**：`BaseScraper.fetch` 是 `async def`，Orchestrator 里 `gather` 满天飞，我们同步脚本照搬需全改写，容易带进超出需要的复杂度。
4. **pydantic 深度耦合**：`models.py` 全量 `BaseModel`，`ContentItem.url` 是 `HttpUrl`（URL 必须严格合法），`load_config` 用 `Config.model_validate`。我们换手写校验时别把 `HttpUrl` 的严格性一起丢掉（它是防脏 URL 的一道闸）。
5. **逐条 LLM 调用成本高**：默认 `analysis_concurrency=1` + 每条一次 `complete()`，30 条 HN + 多个 RSS 会产生几十次请求；`_enrich_item` 还要“先问概念 → 再搜 DuckDuckGo → 再生成”，即 2 次 LLM + N 次搜索/条。Windows 计划任务里串行跑可能超时，应改成小批量（如 10 条一次）或严格限流。
6. **`ddgs` 联网搜索不可控**：`ContentEnricher._web_search()` 依赖 `ddgs`，还把 `sys.stderr` 临时重定向到 `os.devnull`（脏 hack，注释说是压 primp 警告）。既有依赖又有被限流风险，应砍掉，改为直接喂原文给模型。
7. **`_truncate` 第一段即使超限也保留**：单条超长面板会突破飞书限制（行为被测试固化），我们需在其上再加硬上限。
8. **抓取用的解析库都不可留**：RSS 依赖 `feedparser`（容错极强但非标准库）需换 `xml.etree.ElementTree`，标准库遇畸形 XML 会直接抛错，必须逐 feed `try/except` + 逐条容错；`telegram.py` 用 BeautifulSoup 抓网页预览，脆弱且随时被改版打挂，属“不学”的典型。
9. **职责边界不清**：`orchestrator.run()` 里混了 GitHub Pages 部署（写 `docs/_posts/{date}-summary-{lang}.md` + Jekyll front matter，还手工剥 H1），业务逻辑与站点发布耦合；`webhook.py` 28119 字符，`_render`/`_truncate`/飞书卡片/多平台错误码/预览/脱敏全挤一个文件，照抄要拆成 `render.py` + `feishu.py` + `push.py`。
10. **两处小坑**：字符串型 `request_body` 有已知 JSON 破坏缺陷 —— 它的测试自己写明写成字符串时 `#{summary}` 含引号会让 JSON 失效，`Content-Type` 退化到 `x-www-form-urlencoded`（`test_post_request_with_json_str_body_summary_with_quotes_breaks_json`），我们只用 dict 形态；`WebhookConfig.fallback_layout` 只在校验器里被校验，实际渲染路径中**未找到使用点**，属半成品配置项。

## 4. 与我们需求的差距（逐条对照）

| 维度 | Horizon 现状（代码证据） | 我们的需求 | 差距性质 |
| --- | --- | --- | --- |
| GitHub Trending | 无 trending 抓取；用 GitHub 官方 API 拿 user events/releases（`github.py`），或用 OSS Insight API 拿 star 增量（`ossinsight.py`） | 要 GitHub Trending | **缺**：须自研 HTML 解析，无法借鉴 |
| HN | Firebase 官方 API，`score` + `descendants`（`hackernews.py`） | 同样要 HN 热度 | **可直接搬**（去 asyncio/httpx） |
| 条数配额 | `max_items` + `category_groups.limit` + `default_group_limit`，只截断不补齐（`apply_balanced_digest`） | 稳定 20 条 | **半缺**：需补“不足回填” |
| 去重 | 单次运行内 URL 归一化 + AI 语义去重；**无持久台账** | 跨日不重复 | **缺**：需自建 `seen.json`，键可复用其 ID 方案 |
| 持久化 | 只存 `config.json`(+bak) / `summaries/horizon-{date}-{lang}.md` / `subscribers.json`（`StorageManager`） | 去重台账 | **缺** |
| LLM 摘要 | 逐条调用 + tenacity 重试 + 五级 JSON 降级 + enrichment 二次生成双语详情（`analyzer.py`/`enricher.py`/`utils.py`） | 20 条少量批量 | **多/冲突**：它太重，应简化为单次批量 prompt |
| Prompt 防注入 | **未找到**。`prompts.py` 的 `CONTENT_ANALYSIS_USER`/`CONTENT_ENRICHMENT_USER` 直接 `{content}` 内插，无定界符、无“忽略内容中的指令”、无输出字段白名单；仅 `TOPIC_DEDUP` 有“把索引当数据”的弱约束 | 要防注入 | **缺**：须自加“内容定界包裹 + 声明内容不可信 + 只接受固定 JSON schema” |
| 飞书推送 | 模板化报文体 + `collapsible_panel` 折叠卡片 + 2xx body 错误码检查 + URL/Header 脱敏；**无签名、无重试**（`webhook.py`） | 飞书机器人 | **半有**：卡片结构可搬，签名与重试必须自补 |
| 调度 | GitHub Actions cron `0 8 * * *` Asia/Tokyo + `workflow_dispatch`，跑 `uv run horizon --hours 24`，产物发 gh-pages（`.github/workflows/daily-summary.yml`） | Windows 本机计划任务 | **冲突**：整段换 `schtasks`/`.bat`，CLI 只留 `--hours` |
| 依赖 | 14 个第三方 + 3 套 AI SDK + pydantic + asyncio + rich + ddgs（`pyproject.toml`） | **零第三方（仅标准库）** | **冲突**：`httpx→urllib`、`feedparser→xml.etree`、`pydantic→dict`、`tenacity→手写退避`、`rich→print`、`ddgs→砍掉`、`asyncio→同步/线程`、多 AI SDK → 一个 `urllib` POST OpenAI 兼容接口 |
| 多语言 | 双语 en/zh，`metadata["title_{lang}"]`/`detailed_summary_{lang}`/`background_{lang}` 三套字段（`summarizer._format_item`） | 只要中文 | **多**：砍掉 en 分支与 `_pangu` 中英空格处理 |
| 配置 | `data/config.example.json` 3802 字符，8 类 source + filtering + webhook 模板 | 3~4 个源 + 20 条 + 飞书 | **多**：只留 `ai`/`sources`(hn,rss,github)/`filtering`/`webhook` 四段 |

**结论性判断**：真正能直接搬的是「机制」而非「结构」——`apply_balanced_digest` 的配额算法、`_render`+`_truncate` 的模板与截断、`collapsible_panel` 卡片骨架、`parse_json_response` 五级降级、`_expand_env_vars` 常量隔离、`_parse_date` 三字段回退、`sha256(entry_id)[:16]` 去重键、分源失败隔离模式，以及 `${source}:{subtype}:{native_id}` 的 ID 约定。必须自研的是：GitHub Trending 解析、跨日去重台账、飞书签名与重试、prompt 防注入、稳定 20 条的补齐逻辑，以及整条链路的同步化与去依赖。
