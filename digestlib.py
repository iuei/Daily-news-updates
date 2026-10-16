#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日资讯推送 · 核心库（零第三方依赖）

本模块只依赖 Python 标准库，提供：
  1. 配置加载与校验
  2. 网络请求（带重试/退避，应对本机间歇性 TLS 断连）
  3. RSS 2.0 / Atom / RDF 三格式解析（含容错清洗）
  4. GitHub Trending HTML 解析
  5. Hacker News Algolia 聚合搜索
  6. URL 归一化身份键（用于跨源/跨日去重）
  7. 去重台账（JSON，推送成功后才写入）
  8. 文件锁（防计划任务与登录补推撞车）
  9. 飞书交互卡片渲染 + 加签 + 推送（含纯文本降级）

设计约束（来自需求文档）：
  - 零第三方依赖
  - LLM 只放在"增值"位置，失败必须能降级
  - 台账只在推送成功后写
  - 网络调用一律带重试
  - 不使用 zoneinfo（Windows 无 tz database）→ 统一用 datetime.now().astimezone()
"""

import base64
import gzip
import hashlib
import hmac
import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

VERSION = "1.0.0"

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# URL 归一化时忽略的跟踪参数（合并自 Horizon / ai-daily-pulse / Multiscribe 三处实践）
TRACKING_PARAMS = {
    "_ga", "dclid", "fbclid", "gclid", "igshid", "li_fat_id", "mc_cid", "mc_eid",
    "msclkid", "ttclid", "twclid", "vero_id", "utm_source", "utm_medium",
    "utm_campaign", "utm_term", "utm_content", "utm_id", "spm", "from",
    "share_token", "ref", "ref_src", "s", "at_medium", "yclid", "wickedid",
    "guccounter", "guce_referrer", "guce_referrer_sig",
}

# XML 命名空间
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "rss1": "http://purl.org/rss/1.0/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "media": "http://search.yahoo.com/mrss/",
}

# 飞书卡片安全阈值（实测：20 条卡片仅 27 元素 / 4.7KB）
CARD_MAX_ELEMENTS = 45         # 官方上限 200（card JSON 2.0）；需求文档记 50；取 45 保守值
CARD_MAX_BYTES = 19000         # 官方 webhook 请求体硬限 20KB，留 1KB 余量

# 飞书错误码（来自官方 add-custom-bot.md 原文）
FEISHU_ERR_HINTS = {
    19001: "webhook access token 无效（实测返回体原文：incoming webhook access token invalid）",
    19021: "签名校验失败或时间戳超出 ±1 小时",
    19022: "IP 不在白名单",
    19024: "自定义关键词未命中（安全设置要求正文含关键词）",
    9499: "请求体格式错误（Bad Request）",
    11232: "触发频率限制（单租户单机器人 100 次/分、5 次/秒）",
}


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

class _FlushingFileHandler(logging.FileHandler):
    """每条日志立即刷盘，便于计划任务异常中断后排查。"""

    def emit(self, record):
        super().emit(record)
        self.flush()


def harden_stdio():
    """把 stdout/stderr 切到 UTF-8，避免计划任务在非 UTF-8 代码页下写出中文时崩溃。

    Windows 计划任务没有控制台时，Python 的 stdout 编码会退化为 ANSI 代码页（如 GBK），
    打印中文标题将抛 UnicodeEncodeError 并中断整次运行。
    """
    for stream in ("stdout", "stderr"):
        s = getattr(sys, stream, None)
        if s is None:
            continue
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def setup_logging(log_path=None, verbose=False):
    harden_stdio()
    logger = logging.getLogger("digest")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if log_path:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        fh = _FlushingFileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


LOG = logging.getLogger("digest")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "feishu": {"webhook": "", "secret": ""},
    "deepseek": {"api_key": "", "base_url": "https://api.deepseek.com",
                 "model": "deepseek-chat", "timeout": 90},
    "digest": {"max_items": 20, "quota_github": 5, "quota_ai": 10, "quota_media": 5,
               "window_hours": 24, "window_hours_monday": 72, "window_hours_max": 72},
    "ai_filter": {"hn_min_points": 10, "ai_host_points_72h": 30},
    "network": {"timeout": 25, "retries": 3, "backoff_seconds": [2, 8, 30]},
    "sources": {"gn_ai": [], "official": [], "media": [],
                "github_trending": {"enabled": True,
                                    "url": "https://github.com/trending?since=daily",
                                    "min_expected": 5},
                "hacker_news": {"enabled": True, "hits_per_page": 50, "max_pages": 1,
                                "min_points": 10,
                                "keywords": ["AI", "LLM", "agent", "OpenAI", "model"]}},
    "files": {"ledger": "data/ledger.json", "lock": "data/daily.lock",
              "log": "logs/digest.log", "preview": "logs/preview.md"},
    "schedule": {"task_name": "DailyDigestFeishu", "time": "08:35"},
}


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _expand_env(value, _depth=0):
    """递归展开配置里的 ${VAR} / ${VAR:-默认值}，让密钥可以只放环境变量、不落文件。

    借鉴 Horizon 的 _expand_env_vars 思路；密钥不落盘更安全。
    """
    if _depth > 10:
        return value
    if isinstance(value, dict):
        return {k: _expand_env(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, _depth + 1) for v in value]
    if not isinstance(value, str) or "${" not in value:
        return value

    def repl(m):
        name = m.group(1)
        default = m.group(3) or ""
        return os.environ.get(name, default)

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}", repl, value)


def load_config(path):
    """加载配置；文件不存在时用默认值并给出明确提示。支持 ${ENV} 展开。"""
    cfg = dict(DEFAULT_CONFIG)
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        cfg = _deep_merge(cfg, _expand_env(user_cfg))
    return cfg


def validate_config(cfg, need_feishu=True):
    """返回错误列表（借鉴 notbrighton/ai-daily-digest 的 Config.validate 思路）。"""
    errors = []
    if need_feishu:
        hook = (cfg.get("feishu") or {}).get("webhook", "")
        if not hook:
            errors.append("未配置 feishu.webhook（飞书机器人 Webhook URL）")
        elif "/hook/" not in hook:
            errors.append(f"feishu.webhook 格式可疑，应以 .../open-apis/bot/v2/hook/ 开头：{hook[:60]}")
        elif "在此填入" in hook or hook.rstrip("/").endswith("/hook"):
            errors.append("feishu.webhook 仍是占位符，请填入真实 webhook")
    d = cfg.get("digest") or {}
    if d.get("quota_github", 0) + d.get("quota_ai", 0) + d.get("quota_media", 0) != d.get("max_items", 20):
        errors.append("配额之和与 max_items 不一致")
    ds = cfg.get("deepseek") or {}
    if ds.get("api_key") and not str(ds.get("base_url", "")).startswith("http"):
        errors.append("deepseek.base_url 不是合法 URL")
    return errors


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def clip(text, limit):
    """按字符数截断（超长一律在进入报文前截断，不在报文里截）。"""
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def strip_tags(text):
    """去 HTML 标签 + 反转义 + 压缩空白。"""
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def url_key(url):
    """URL 身份键：跨源/跨日去重用。

    忽略：scheme/host 大小写、www. 前缀、默认端口、跟踪参数、末尾斜杠、fragment、URL 编码差异。
    注意：**不对 path 做小写化** —— GitHub 仓库路径大小写敏感，
    实测 /OpenAI/gpt 与 /openai/gpt 会被小写化误判为同一条（Horizon 与 ai-daily-pulse 都存在此缺陷）。
    """
    if not url:
        return ()
    try:
        p = urllib.parse.urlsplit(url.strip())
    except Exception:
        return ("raw", url.strip())
    scheme = p.scheme.lower()
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = p.port
    if (scheme, port) in {("http", 80), ("https", 443)}:
        port = None
    path = urllib.parse.unquote_plus(p.path).rstrip("/") or "/"
    try:
        q = sorted((k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
                   if k.lower() not in TRACKING_PARAMS)
    except Exception:
        q = []
    return (scheme, host, port or "", path, tuple(q))


def content_hash(title, desc=""):
    """内容哈希（挡『URL 变了但内容相同』的重复投递）。"""
    raw = f"{(title or '').strip()}\n{(desc or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def parse_datetime(text):
    """宽松时间解析：RFC822 / ISO8601 / 松散格式，统一返回带时区的 datetime。"""
    if not text:
        return None
    text = str(text).strip()
    try:
        dt = parsedate_to_datetime(text)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    iso = text.replace("Z", "+00:00")
    for cand in (iso, iso[:19], iso[:10]):
        try:
            dt = datetime.fromisoformat(cand)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def local_now():
    """本地当前时间（不使用 zoneinfo，Windows 兼容）。"""
    return datetime.now().astimezone()


def compute_window_hours(cfg, now=None):
    """时窗自适应：周一回看 72h（覆盖周末），其余 24h，上限 window_hours_max。"""
    now = now or local_now()
    d = cfg.get("digest") or {}
    hours = d.get("window_hours_monday", 72) if now.weekday() == 0 else d.get("window_hours", 24)
    return min(hours, d.get("window_hours_max", 72))


def compute_window_hours_with_catchup(cfg, ledger, now=None):
    """在上面的基础上，按『距上次成功推送的间隔』自动放宽时窗（补跑能力）。

    为什么需要：若电脑连续几天没开机，固定 24h 时窗会让这几天的内容全部过期丢失。
    这里用上次成功推送的日期把时窗拉到覆盖那段空档，再受 catchup_max_hours 上限约束
    （默认 7 天，避免把很旧的新闻当新闻推）。

    返回 (时窗小时数, 说明文本)。
    """
    now = now or local_now()
    base = compute_window_hours(cfg, now)
    d = cfg.get("digest") or {}
    if not d.get("auto_catchup", True):
        return base, ""

    last = None
    for run in reversed(ledger.data.get("runs", [])):
        try:
            last = datetime.strptime(run.get("date", ""), "%Y-%m-%d").date()
            break
        except Exception:
            continue
    if last is None:
        return base, ""

    gap_days = (now.date() - last).days
    if gap_days <= 1:
        return base, ""
    # 覆盖 gap_days 天（多加 1 天余量），再受上限约束
    wanted = (gap_days + 1) * 24
    cap = int(d.get("catchup_max_hours", 168))
    hours = min(max(base, wanted), cap)
    if hours <= base:
        return base, ""
    note = (f"距上次成功推送已 {gap_days} 天，时窗自动放宽到 {hours}h"
            f"（上限 {cap}h）以补回遗漏内容")
    return hours, note


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------

class FetchError(Exception):
    pass


def http_get(url, cfg, as_json=False, timeout=None, retries=None):
    """带重试/退避的 GET。

    每次重试都重建连接并带 Connection: close ——
    本机出口 IP 会轮换，复用坏连接会导致一直失败（实测首轮 6 个源全 SSL 失败，重试后 100% 成功）。
    """
    net = cfg.get("network") or {}
    timeout = timeout or net.get("timeout", 25)
    retries = retries if retries is not None else net.get("retries", 3)
    backoffs = net.get("backoff_seconds", [2, 8, 30])
    last = None
    for attempt in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": DEFAULT_UA,
                "Accept": ("application/json" if as_json else
                           "application/rss+xml, application/atom+xml, application/xml, "
                           "text/xml, text/html, */*"),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Connection": "close",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        pass
            return json.loads(raw) if as_json else raw
        except urllib.error.HTTPError as e:
            last = e
            # 4xx 除 429 外不重试（重试无意义）
            if 400 <= e.code < 500 and e.code != 429:
                raise FetchError(f"HTTP {e.code} {url}")
        except Exception as e:  # noqa: BLE001 - 需要捕获所有网络异常
            last = e
        if attempt < max(1, retries) - 1:
            wait = backoffs[min(attempt, len(backoffs) - 1)]
            LOG.debug("请求失败（第 %d 次）%s：%s，%ds 后重试", attempt + 1, url, last, wait)
            time.sleep(wait)
    raise FetchError(f"重试 {retries} 次仍失败：{url} -> {type(last).__name__}: {last}")


def http_post_json(url, payload, cfg, timeout=None, retries=None, headers=None):
    """带重试的 JSON POST。返回 (http_status, body_dict)。

    注意：飞书 HTTP 200 也可能是失败，必须由调用方检查 body 里的 code。
    headers 用于附加鉴权头（如 LLM 接口的 Authorization），不要用它覆盖 Content-Type。
    """
    net = cfg.get("network") or {}
    timeout = timeout or net.get("timeout", 25)
    retries = retries if retries is not None else net.get("retries", 3)
    backoffs = net.get("backoff_seconds", [2, 8, 30])
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    send_headers = {"Content-Type": "application/json; charset=utf-8",
                    "User-Agent": DEFAULT_UA, "Connection": "close"}
    if headers:
        send_headers.update(headers)
    last = None
    for attempt in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, data=body, headers=send_headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "ignore")
                return resp.status, _safe_json(text)
        except urllib.error.HTTPError as e:
            text = ""
            try:
                text = e.read().decode("utf-8", "ignore")
            except Exception:
                pass
            parsed = _safe_json(text)
            # 限流/服务端错误重试，其他 4xx 直接返回让调用方决定降级
            if e.code in (429, 500, 502, 503, 504):
                last = f"HTTP {e.code} {text[:120]}"
            else:
                return e.code, parsed or {"code": e.code, "msg": text[:200]}
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        if attempt < max(1, retries) - 1:
            wait = backoffs[min(attempt, len(backoffs) - 1)]
            LOG.debug("POST 失败（第 %d 次）：%s，%ds 后重试", attempt + 1, last, wait)
            time.sleep(wait)
    return None, {"code": -1, "msg": f"重试 {retries} 次仍失败：{last}"}


def _safe_json(text):
    try:
        return json.loads(text)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Feed 解析（RSS 2.0 / Atom / RDF）
# ---------------------------------------------------------------------------

def repair_xml(text, stats=None):
    """XML 容错清洗：去 BOM、去非法控制字符、转义裸 &。

    ElementTree 遇到这些问题会直接抛错，而部分中文源确实存在未转义的 &。
    """
    original = text
    if text.startswith("\ufeff"):
        text = text[1:]
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
    if stats is not None and text != original:
        stats["repaired"] = True
    return text


def _first_text(el, paths):
    """依次尝试多个路径；同时兼容『无命名空间』与『RSS1.0 默认命名空间』两种形式。

    RDF/RSS 1.0 的 title/link/description 全部落在 http://purl.org/rss/1.0/ 下，
    只用无限定名 findtext 会静默返回 None（实测踩到）。
    """
    for p in paths:
        for cand in (p, "{http://purl.org/rss/1.0/}" + p):
            try:
                v = el.findtext(cand, namespaces=NS)
            except Exception:
                continue
            if v and v.strip():
                return v.strip()
    return ""


def _atom_link(el):
    """Atom 的 link 在属性里，按 rel 优先级挑选。"""
    best = ""
    for ln in el.findall("atom:link", NS):
        rel = ln.get("rel", "alternate")
        href = ln.get("href", "")
        if not href:
            continue
        if rel == "alternate":
            return href
        if not best:
            best = href
    return best


def parse_feed(raw_bytes, source_name, stats=None):
    """解析 RSS 2.0 / Atom / RDF，返回条目列表。

    条目字段：title / url / ts / desc / source
    """
    text = raw_bytes.decode("utf-8", "replace") if isinstance(raw_bytes, bytes) else raw_bytes
    root = ET.fromstring(repair_xml(text, stats))
    tag = root.tag.split("}")[-1].lower()

    if tag == "feed":
        kind = "atom"
        nodes = root.findall("atom:entry", NS)
    elif tag == "rdf":
        kind = "rdf"
        nodes = root.findall("rss1:item", NS)
    else:
        kind = "rss"
        nodes = root.findall(".//item")
        if not nodes:
            # 有些源根节点是 channel 之外的结构
            nodes = root.findall(".//{http://purl.org/rss/1.0/}item", NS)

    items = []
    skipped = 0
    for el in nodes:
        title = strip_tags(_first_text(el, ["title", "atom:title"]))
        url = (_first_text(el, ["link", "atom:link"]) or _atom_link(el)
               or _first_text(el, ["guid", "id", "atom:id"])).strip()
        ts = parse_datetime(_first_text(
            el, ["pubDate", "published", "updated", "atom:published", "atom:updated",
                 "dc:date", "{http://purl.org/dc/elements/1.1/}date"]))
        desc = strip_tags(_first_text(
            el, ["description", "summary", "atom:summary", "atom:content",
                 "content:encoded", "{http://purl.org/rss/1.0/modules/content/}encoded"]))
        if not url:
            skipped += 1
            continue
        items.append({
            "title": title or "(无标题)",
            "url": url,
            "ts": ts,
            "desc": desc,
            "source": source_name,
        })

    if stats is not None:
        stats["kind"] = kind
        stats["raw"] = len(nodes)
        stats["skipped_no_url"] = skipped
    return items


# ---------------------------------------------------------------------------
# GitHub Trending（无官方 API，解析 HTML）
# ---------------------------------------------------------------------------

_ARTICLE_RE = re.compile(r'<article class="Box-row">(.*?)</article>', re.S)
_STAR_TODAY_RE = re.compile(r"([\d,]+)\s*stars?\s*today")
_REPO_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*href="/([^"]+)"', re.S)
_DESC_RE = re.compile(r'<p class="col-9[^"]*"[^>]*>(.*?)</p>', re.S)
_LANG_RE = re.compile(r'itemprop="programmingLanguage">\s*([^<]+?)\s*<')
_TOTAL_STAR_RE = re.compile(r'href="/[^"]+/stargazers"[^>]*>.*?([\d,]+)\s*</a>', re.S)


def parse_github_trending(page_text, now=None):
    """解析 github.com/trending 页面。

    实测：daily 页通常 11–25 个仓库块（不是固定 25），字段解析成功率 100%。
    Trending 无发布时间，因此用抓取时刻作为时间（并由调用方标记为 snapshot）。
    """
    now = now or datetime.now(timezone.utc)
    out = []
    for block in _ARTICLE_RE.findall(page_text):
        m = _REPO_RE.search(block)
        if not m:
            continue
        repo = m.group(1).strip()
        d = _DESC_RE.search(block)
        desc = strip_tags(d.group(1)) if d else ""
        lang_m = _LANG_RE.search(block)
        lang = lang_m.group(1).strip() if lang_m else ""
        star_m = _TOTAL_STAR_RE.search(block)
        total = star_m.group(1) if star_m else ""
        today_m = _STAR_TODAY_RE.search(block)
        today = today_m.group(1) if today_m else ""
        out.append({
            "title": f"{repo}（今日 +{today} star）" if today else repo,
            "url": f"https://github.com/{repo}",
            "ts": now,
            "desc": desc,
            "source": "GitHub Trending",
            "group": "github",
            "lang": lang,
            "stars_total": total,
            "stars_today": today,
            "freshness": "snapshot",
        })
    return out


# ---------------------------------------------------------------------------
# Hacker News（Algolia 聚合搜索）
# ---------------------------------------------------------------------------

def fetch_hacker_news(cfg, window_hours, now=None):
    """用 search_by_date 多关键词聚合取 HN 的 AI 条目。

    为什么不用全站热榜 + 关键词过滤：实测 front_page 24h 内仅 2–3 条命中 AI 关键词，
    打不满需求；聚合搜索 24h 内可得 220 条候选，并按 points 排序取高质量条目。
    """
    now = now or datetime.now(timezone.utc)
    hn_cfg = (cfg.get("sources") or {}).get("hacker_news") or {}
    if not hn_cfg.get("enabled", True):
        return []
    keywords = hn_cfg.get("keywords") or ["AI"]
    per = int(hn_cfg.get("hits_per_page", 50))
    pages = int(hn_cfg.get("max_pages", 1))
    lo_ts = (now - timedelta(hours=window_hours)).timestamp()

    pool = {}
    for kw in keywords:
        for page in range(max(1, pages)):
            url = ("https://hn.algolia.com/api/v1/search_by_date?query="
                   + urllib.parse.quote(kw)
                   + f"&tags=story&hitsPerPage={per}&page={page}")
            try:
                data = http_get(url, cfg, as_json=True)
            except FetchError as e:
                LOG.warning("HN 关键词 %s 拉取失败：%s", kw, e)
                continue
            for h in (data or {}).get("hits", []):
                ts_i = h.get("created_at_i")
                if not ts_i or ts_i < lo_ts:
                    continue
                oid = h.get("objectID")
                if not oid or oid in pool:
                    continue
                title = (h.get("title") or "").strip()
                if not title:
                    continue
                pool[oid] = {
                    "title": title,
                    "url": h.get("url") or f"https://news.ycombinator.com/item?id={oid}",
                    "ts": datetime.fromtimestamp(ts_i, timezone.utc),
                    "desc": f"HN {h.get('points') or 0} 分 · {h.get('num_comments') or 0} 评论",
                    "source": "Hacker News",
                    "group": "hn",
                    "points": h.get("points") or 0,
                    "comments": h.get("num_comments") or 0,
                }
    items = list(pool.values())
    items.sort(key=lambda x: (x["points"], x["ts"]), reverse=True)
    LOG.info("HN 聚合：%d 个关键词 → 时窗 %dh 内候选 %d 条",
             len(keywords), window_hours, len(items))
    return items


# ---------------------------------------------------------------------------
# AI 关键词打分（词边界匹配）
# ---------------------------------------------------------------------------

# 强特征：几乎必然是 AI 内容
AI_STRONG = [
    "ai", "artificial intelligence", "llm", "llms", "gpt", "chatgpt", "openai",
    "anthropic", "claude", "gemini", "deepseek", "genai", "copilot", "mistral",
    "llama", "qwen", "diffusion", "transformer", "neural", "inference", "embedding",
    "fine-tuning", "finetune", "rag", "multimodal", "machine learning", "agi",
    "大模型", "人工智能", "智能体", "多模态", "算力", "生成式",
]
# 邻域：AI 相关但不独占，用于补量
AI_NEAR = [
    "agent", "agents", "model", "models", "prompt", "prompts", "gpu", "cuda",
    "dataset", "training", "robotics", "autonomous", "ml", "token",
    "机器人", "芯片", "算法", "训练", "推理",
]


def _kw_hit(text, words):
    """词边界匹配。ASCII 词用 \\b 边界，中文词直接子串。

    实测：子串匹配 `"ai" in text` 在 13 个用例上错 8 个
    （said / chair / domain / email / Thailand / chain / mainframe / available 全误命中）。
    """
    if not text:
        return []
    hits = []
    for w in words:
        if w.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", text):
                hits.append(w)
        elif w in text:
            hits.append(w)
    return hits


def score_ai(item):
    """返回 (是否强 AI, 命中词列表)。"""
    hay = f"{item.get('title', '')} {item.get('desc', '')}".lower()
    hits = _kw_hit(hay, AI_STRONG)
    if hits:
        return True, hits
    return False, _kw_hit(hay, AI_NEAR)


# ---------------------------------------------------------------------------
# 去重台账
# ---------------------------------------------------------------------------

class Ledger:
    """去重台账。

    结构：{"version":1, "runs":[{"date":"2026-10-16","pushed_at":"...","items":[{"key":[...],
    "hash":"...","title":"...","url":"..."}]}]}
    关键约束：**只在推送成功后写入** —— 重复运行绝不重复推送。
    """

    def __init__(self, path, retain_days=90):
        self.path = path
        self.retain_days = retain_days
        self.data = {"version": 1, "runs": []}
        self.keys = set()
        self.hashes = set()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception as e:
                LOG.warning("台账损坏，按空台账继续（不会重复推送今日，但可能重推历史）：%s", e)
                self.data = {"version": 1, "runs": []}
        self._reindex()
        return self

    @staticmethod
    def _hashable(key):
        """JSON 往返会把 tuple 变成 list，这里递归转回可哈希结构。"""
        if isinstance(key, (list, tuple)):
            return tuple(Ledger._hashable(x) for x in key)
        return key

    def _reindex(self):
        self.keys, self.hashes = set(), set()
        for run in self.data.get("runs", []):
            for it in run.get("items", []):
                k = it.get("key")
                if k:
                    self.keys.add(self._hashable(k))
                if it.get("hash"):
                    self.hashes.add(it["hash"])

    def seen(self, item):
        k = url_key(item.get("url"))
        if k and k in self.keys:
            return True
        h = content_hash(item.get("title"), item.get("desc"))
        return bool(h and h in self.hashes)

    def prune(self, today=None):
        """清理超过保留期的历史（保留期上限避免台账文件无限增长）。

        today 允许传 datetime 或 date —— 统一取 .date() 再相减，
        否则 datetime - date 会抛 TypeError。
        """
        if today is None:
            today = local_now()
        if isinstance(today, datetime):      # 注意：datetime 是 date 的子类，必须先判 datetime
            today = today.date()
        elif not isinstance(today, date):
            today = local_now().date()
        keep = []
        for run in self.data.get("runs", []):
            try:
                d = datetime.strptime(run.get("date", ""), "%Y-%m-%d").date()
            except Exception:
                keep.append(run)
                continue
            if (today - d).days <= self.retain_days:
                keep.append(run)
        removed = len(self.data.get("runs", [])) - len(keep)
        self.data["runs"] = keep
        return removed

    def record(self, items, when=None):
        when = when or local_now()
        self.data.setdefault("runs", []).append({
            "date": when.strftime("%Y-%m-%d"),
            "pushed_at": when.isoformat(),
            "items": [{
                "key": list(url_key(it.get("url"))),
                "hash": content_hash(it.get("title"), it.get("desc")),
                "title": clip(it.get("title"), 120),
                "url": it.get("url"),
                "source": it.get("source"),
            } for it in items],
        })
        self._reindex()

    def save(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def pushed_today(self, when=None):
        when = when or local_now()
        today = when.strftime("%Y-%m-%d")
        return any(r.get("date") == today for r in self.data.get("runs", []))


# ---------------------------------------------------------------------------
# 文件锁（防重入）
# ---------------------------------------------------------------------------

class FileLock:
    """用 O_CREAT|O_EXCL 实现跨进程互斥；锁键含日期，保证『同一天三个入口只跑一次』。"""

    def __init__(self, path):
        self.path = path
        self.acquired = False

    def acquire(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()} at={local_now().isoformat()}\n".encode("utf-8"))
            os.close(fd)
            self.acquired = True
            return True
        except FileExistsError:
            return False

    def release(self):
        if self.acquired:
            try:
                os.remove(self.path)
            except OSError:
                pass
            self.acquired = False


def lock_is_stale(lock_path, max_age_hours=6):
    """锁文件过旧（进程被强杀）时允许清除。"""
    try:
        age = time.time() - os.path.getmtime(lock_path)
        return age > max_age_hours * 3600
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 配额与降级链
# ---------------------------------------------------------------------------

def allocate_quota(cfg, groups, window_hours):
    """分组配额 + 降级链，保证尽量凑满 max_items。

    groups: {"github": [...], "official": [...], "hn": [...], "cn_ai": [...], "media": [...]}
    策略（借鉴 Multiscribe 的板块保底 + Horizon 的纯规则截断，并补上它们都缺的『不足回填』）：
      1. GitHub Trending 取 quota_github
      2. AI 官方/博客取不超过 40% 的 AI 配额
      3. 中文 AI 源保底 reserve_cn_ai 条（LLM 不可用时它是唯一的中文可读来源）
      4. HN 按分数补足 AI 组
      5. 其他热点取 quota_media
      6. 仍不足则依次用 HN / 中文源 / 媒体源回填到 max_items
    另加**单源集中度限制**（max_per_source）：防止某个高产 feed（实测 MIT Tech Review
    一次占了全部 5 个媒体位）独占分组。
    """
    d = cfg.get("digest") or {}
    af = cfg.get("ai_filter") or {}
    max_items = int(d.get("max_items", 20))
    q_gh = int(d.get("quota_github", 5))
    q_ai = int(d.get("quota_ai", 10))
    q_media = int(d.get("quota_media", 5))
    reserve_cn = int(d.get("reserve_cn_ai", 3))
    max_per_source = int(d.get("max_per_source", 2))
    min_points = int(af.get("ai_host_points_72h", 30) if window_hours > 24
                     else af.get("hn_min_points", 10))

    chosen, used = [], set()
    src_count = {}
    report = {}

    def take(name, candidates, limit, per_source=None):
        """先按单源上限选，凑不满再放开上限补（保证『多样性优先，但配额必须满』）。"""
        per_source = max_per_source if per_source is None else per_source
        got = 0
        for relax in (False, True) if per_source else (True,):
            if got >= limit:
                break
            for it in candidates:
                if got >= limit:
                    break
                if id(it) in used:
                    continue
                src = it.get("source") or "?"
                if not relax and src_count.get(src, 0) >= per_source:
                    continue
                used.add(id(it))
                src_count[src] = src_count.get(src, 0) + 1
                it.setdefault("group_final", name)
                chosen.append(it)
                got += 1
        report[name] = report.get(name, 0) + got
        return got

    take("github", groups.get("github", []), q_gh, per_source=int(d.get("max_per_source_github", 5)))

    hn_pool = [x for x in groups.get("hn", []) if (x.get("points") or 0) >= min_points]
    if not hn_pool:
        LOG.warning("HN 无满足 ★≥%d 的条目，放宽门槛", min_points)
        hn_pool = list(groups.get("hn", []))

    official = groups.get("official", [])
    cn_ai = groups.get("cn_ai", [])

    ai_official_cap = max(1, q_ai * 2 // 5)
    take("ai_official", official, ai_official_cap)

    # 中文源保底（LLM 降级时唯一的中文可读来源）
    cn_reserved = min(reserve_cn, q_ai - report.get("ai_official", 0)) if cn_ai else 0
    if cn_reserved > 0:
        take("ai_cn", cn_ai, cn_reserved)

    ai_so_far = sum(v for k, v in report.items() if k.startswith("ai_"))
    take("ai_hn", hn_pool, max(0, q_ai - ai_so_far))

    ai_so_far = sum(v for k, v in report.items() if k.startswith("ai_"))
    if ai_so_far < q_ai and cn_ai:
        take("ai_cn", cn_ai, q_ai - ai_so_far)
    ai_so_far = sum(v for k, v in report.items() if k.startswith("ai_"))
    if ai_so_far < q_ai:
        take("ai_official", official, q_ai - ai_so_far)

    take("media", groups.get("media", []), q_media)

    # 回填（放宽单源限制，避免凑不满；最后一档才用未经 AI 相关度过滤的 HN）
    fill_pools = [("fill_hn", hn_pool), ("fill_cn", cn_ai),
                  ("fill_media", groups.get("media", [])),
                  ("fill_official", official),
                  ("fill_hn_any", groups.get("hn_unfiltered", []))]
    for name, pool in fill_pools:
        if len(chosen) >= max_items:
            break
        take(name, pool, max_items - len(chosen), per_source=0)

    report["total"] = len(chosen)
    report["hn_min_points"] = min_points
    report["by_source"] = dict(sorted(src_count.items(), key=lambda x: -x[1]))
    return chosen, report


# ---------------------------------------------------------------------------
# DeepSeek 摘要（唯一一次 LLM 调用，失败可降级）
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM_PROMPT = (
    "你是资讯摘要助手，为读者生成一句中文摘要。要求：\n"
    "1. 不超过 40 个汉字，直接说清这条内容是什么、有什么看点；\n"
    "2. 像给同事转述新闻一样自然，不要套话；\n"
    "3. 禁止使用『值得关注』『值得一看』『引发关注』这类空泛结尾；\n"
    "4. 只依据给定信息，不要编造数字、结论或原文没有的事实；\n"
    "5. 不要输出 Markdown、不要加引号、不要重复标题、不要用『该文章』开头。\n"
    # 注意：DeepSeek/OpenAI 在使用 response_format=json_object 时要求提示词中出现 "json"
    # 字样，否则返回 400。这里显式保留该词，勿删。
    "只输出 JSON 对象，格式为 {\"items\":[{\"id\":1,\"summary\":\"…\"}, …]}，不要任何解释。"
)


def build_summary_payload(cfg, items):
    lines = []
    for i, it in enumerate(items, 1):
        title = clip(it.get("title"), 160)
        desc = clip(it.get("desc"), 200)
        line = f"{i}. [{it.get('source')}] {title}"
        if desc and desc != "Comments":
            line += f"\n   说明: {desc}"
        lines.append(line)
    user = f"为下列 {len(items)} 条资讯各生成一句中文摘要（id 必须与序号一致）：\n" + "\n".join(lines)
    ds = cfg.get("deepseek") or {}
    return {
        "model": ds.get("model", "deepseek-chat"),
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "max_tokens": 2000,
    }


def _extract_summaries(body):
    """兼容多种返回形态：{"items":[...]} / [...] / {"1":"…"} / {"summaries":[...]}"""
    if body is None:
        return {}
    if isinstance(body, list):
        seq = body
    elif isinstance(body, dict):
        seq = None
        for k in ("items", "summaries", "data", "result", "list"):
            if isinstance(body.get(k), list):
                seq = body[k]
                break
        if seq is None:
            # 形如 {"1": "摘要", "2": "…"}
            numeric = {k: v for k, v in body.items() if str(k).isdigit()}
            if numeric:
                return {int(k): str(v).strip() for k, v in numeric.items()}
            return {}
    else:
        return {}

    out = {}
    for idx, entry in enumerate(seq, 1):
        if isinstance(entry, dict):
            sid = entry.get("id", entry.get("index", idx))
            text = (entry.get("summary") or entry.get("text")
                    or entry.get("content") or entry.get("zh") or "")
            try:
                sid = int(str(sid).strip())
            except Exception:
                sid = idx
            if str(text).strip():
                out[sid] = strip_tags(str(text))
        elif isinstance(entry, str) and entry.strip():
            out[idx] = strip_tags(entry)
    return out


def generate_summaries(cfg, items, timeout=None):
    """批量生成中文摘要。失败返回空 dict（调用方降级），绝不抛异常中断日报。"""
    ds = cfg.get("deepseek") or {}
    api_key = (ds.get("api_key") or "").strip()
    if not api_key:
        LOG.info("未配置 deepseek.api_key，跳过中文摘要（降级为原标题/自带描述）")
        return {}
    if not items:
        return {}

    base = (ds.get("base_url") or "https://api.deepseek.com").rstrip("/")
    url = base + "/chat/completions"
    payload = build_summary_payload(cfg, items)
    auth_headers = {"Authorization": f"Bearer {api_key}"}
    try:
        status, body = http_post_json(url, payload, cfg, timeout=timeout or ds.get("timeout", 90),
                                      retries=2, headers=auth_headers)
    except Exception as e:  # noqa: BLE001
        LOG.warning("摘要请求异常，降级：%s", e)
        return {}

    if not body:
        LOG.warning("摘要返回体无法解析，降级")
        return {}

    # 识别接口错误：兼容 OpenAI 风格 {"error":{...}} 与 DeepSeek 风格 {"code":401,"msg":...}
    err_detail = None
    if isinstance(body, dict):
        if body.get("error"):
            err_detail = str(body["error"])[:200]
        elif body.get("code") not in (None, 0, "0") and not body.get("choices"):
            err_detail = f"code={body.get('code')} msg={body.get('msg')}"
    if err_detail:
        LOG.warning("摘要接口返回错误，降级：%s", err_detail)
        if "401" in err_detail or "Authentication" in err_detail or "auth" in err_detail.lower():
            LOG.warning("→ 认证失败：请检查 deepseek.api_key 是否正确/是否已失效；"
                        "若用环境变量，注意 User 级变量对已运行的进程不生效，需重开终端。")
        return {}

    content = ""
    try:
        content = body["choices"][0]["message"]["content"]
    except Exception:
        LOG.warning("摘要返回结构异常，降级：%s", str(body)[:200])
        return {}

    parsed = _safe_json(content)
    if parsed is None:
        # 容错：从文本里抠出第一个 JSON 片段
        m = re.search(r"\{.*\}|\[.*\]", content, re.S)
        parsed = _safe_json(m.group(0)) if m else None
    summaries = _extract_summaries(parsed)
    LOG.info("摘要成功：%d/%d 条", len(summaries), len(items))
    return summaries


def apply_summaries(items, summaries):
    """把摘要写回条目；缺失的标记为降级。返回 (命中数, 缺失数)。

    关键：降级时必须区分「中文描述」与「英文描述」——
    英文描述不能当作可读主体（用户看不懂），只作为附注。
    因此额外标记 `summary_is_zh`，供渲染层决定版式。
    """
    hit = miss = 0
    for i, it in enumerate(items, 1):
        s = summaries.get(i)
        if s:
            it["summary_zh"] = clip(s, 60)
            it["summary_source"] = "llm"
            it["summary_is_zh"] = is_chinese_text(s)
            hit += 1
        else:
            fallback = strip_tags(it.get("desc") or "")
            # 占位符（如"点击查看原文>"）不能当作描述
            if fallback and not is_junk_desc(fallback):
                is_zh = is_chinese_text(fallback)
                it["summary_zh"] = clip(fallback, 60)
                it["summary_source"] = "source"
                it["summary_is_zh"] = is_zh
            else:
                it["summary_zh"] = ""
                it["summary_source"] = "none"
                it["summary_is_zh"] = False
            miss += 1
    return hit, miss


def is_chinese_text(text):
    """判断文本是否以中文为主（中文源无需 LLM 也能用）。"""
    if not text:
        return False
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return cjk >= max(3, len(text) // 6)


# 无信息量的描述占位符（实测 InfoQ 等源会给出这类文本，直接展示等于噪音）
JUNK_DESC_PATTERNS = [
    "点击查看原文", "阅读全文", "查看原文", "点击阅读", "click to read",
    "read more", "continue reading", "comments", "查看全文", "原文链接",
]

# 低价值内容关键词（促销/导购/优惠券等，实测 Wired 会推送大量此类内容）
LOW_VALUE_PATTERNS = [
    "coupon", "promo code", "discount code", "deals", "优惠券", "优惠码",
    "best deals", "save up to", "% off", "prime day", "black friday",
    "giveaway", "sponsored",
]


def is_junk_desc(text):
    """描述是否为无信息量占位符。"""
    if not text:
        return True
    t = strip_tags(text).strip().lower()
    if len(t) < 6:
        return True
    return any(p in t for p in JUNK_DESC_PATTERNS) and len(t) <= 40


def is_low_value(item):
    """判断条目是否为促销/导购等低价值内容。"""
    hay = f"{item.get('title', '')} {item.get('desc', '')}".lower()
    return any(re.search(p, hay) for p in LOW_VALUE_PATTERNS)


# ---------------------------------------------------------------------------
# 飞书：加签 / 卡片渲染 / 推送
# ---------------------------------------------------------------------------

def feishu_sign(secret, timestamp=None):
    """官方加签算法。

    官方原文（add-custom-bot.md）：
      「将 timestamp + "\\n" + 密钥 当做签名字符串，使用 HmacSHA256 算法计算空字符串的
        签名结果，再进行 Base64 编码。timestamp 距当前时间不超过 1 小时，单位：s。」
    => key = f"{ts}\\n{secret}"，被签名的 message = b""
    已用官方向量验证：feishu_sign("a", "1") == rWZ84lcag1x9aBFhn1gtV4ZN+4gme3pilfQNMk86vKg=
    """
    ts = str(timestamp if timestamp is not None else int(time.time()))
    string_to_sign = f"{ts}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return ts, base64.b64encode(digest).decode("utf-8")


GOAL_LABEL = {"github": "🔥 GitHub 热门仓库", "ai": "🤖 AI 热点", "media": "📰 其他热点"}

# 内部分组（配额用）→ 展示分组（用户看到的三块）
GROUP_ORDER = ["github", "ai", "media"]


def display_group(it):
    """把内部分组名归一为『用户看到的三个板块』。

    显式枚举而非前缀猜测：`fill_hn_any` 去掉 fill_ 后是 `hn_any`，不以 ai 开头，
    用 startswith 判断会被误归为 media（自检抓到过这个 bug）。
    """
    g = (it.get("group_final") or it.get("group") or "media").replace("fill_", "")
    if g == "github":
        return "github"
    if g in ("official", "ai_official", "ai", "hn", "hn_any",
             "ai_hn", "ai_cn", "ai_hn_any", "cn_ai", "gn_ai"):
        return "ai"
    return "media"


def order_for_display(items):
    """按板块固定顺序输出（GitHub → AI → 其他热点）。

    排序是稳定排序，因此板块内保持 allocate_quota 给出的分数/时间顺序。
    """
    rank = {g: i for i, g in enumerate(GROUP_ORDER)}
    return sorted(items, key=lambda x: rank.get(display_group(x), 99))


def render_card(items, title=None, footer=None):
    """渲染飞书交互卡片。

    排版目标（面向只读中文的用户）：
      1. 每条以**编号**开头，便于在手机上核对是否读完、有没有遗漏；
      2. 中文摘要是唯一的主体文字（加粗、可点击），英文原题退为灰色小字「原题」；
      3. 元信息（来源/热度）压缩成一行灰色小字，不抢视线；
      4. 板块之间用分隔线隔开。

    实测：20 条 + 3 个板块标题 + 2 条分隔线 + 页脚 = 26 个元素 / 约 7KB，
    远低于元素上限（200）与请求体上限（20KB），无需分片。
    设计约束：**一条资讯 = 一个 markdown 元素**；**绝不使用 table 元素**
    （表格上限仅 3~5 个，20 条必撞 230099/11310）。
    """
    card_title = title or f"📰 每日资讯日报 · {local_now().strftime('%Y年%m月%d日')}"
    elements = []
    last_group = None
    ordered = order_for_display(items)

    for idx, it in enumerate(ordered, 1):
        g = display_group(it)
        if g != last_group:
            last_group = g
            label = GOAL_LABEL.get(g, g)
            if elements:  # 板块之间加分隔线，视觉更清晰
                elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": f"**{label}**"})

        url = it.get("url")
        zh = (it.get("summary_zh") or "").strip()
        zh_ok = bool(zh) and it.get("summary_is_zh", True)
        zh_source = it.get("summary_source")
        raw_title = (it.get("title") or "").strip()
        title_is_zh = is_chinese_text(raw_title)

        # ---- 主体行：中文（加粗 + 可点击）----
        if zh_ok:
            body = f"{idx}. **[{clip(zh, 70)}]({url})**"
        elif title_is_zh:
            body = f"{idx}. **[{clip(raw_title, 70)}]({url})**"
        else:
            # 只有英文：仍然给出链接，但明确标注未翻译，避免读完才发现看不懂
            body = f"{idx}. **[{clip(raw_title, 70)}]({url})**"
            body += "\n<font color='red'>⚠️ 本条未翻译（摘要服务不可用）</font>"

        # ---- 附注行：英文原题（仅当有中文主体且原题是英文时）----
        if zh_ok and not title_is_zh and raw_title and raw_title not in zh:
            body += f"\n<font color='grey'>原题：{clip(raw_title, 80)}</font>"
        elif zh_ok and title_is_zh and zh != raw_title:
            body += f"\n<font color='grey'>{clip(raw_title, 80)}</font>"
        elif not zh_ok and not title_is_zh and zh:
            # 降级到英文描述：只作小字附注，不占主体
            body += f"\n<font color='grey'>{clip(zh, 80)}</font>"

        # ---- 元信息行：来源 + 热度，全部用中文表述 ----
        meta = []
        if it.get("source"):
            meta.append(str(it["source"]))
        if it.get("stars_today"):
            meta.append(f"今日涨 {it['stars_today']} 星")
        if it.get("points"):
            meta.append(f"HN {it['points']} 分")
        if zh_source == "llm":
            meta.append("AI 摘要")
        elif zh_source == "source" and zh_ok:
            meta.append("原文摘要")
        if meta:
            body += f"\n<font color='grey'>{' · '.join(meta)}</font>"

        elements.append({"tag": "markdown", "content": body})

    if footer:
        elements.append({"tag": "note", "elements": [
            {"tag": "plain_text", "content": clip(footer, 300)}]})

    return {"header": {"title": {"tag": "plain_text", "content": card_title}},
            "elements": elements}


def render_text(items, title=None, footer=None):
    """纯文本降级版（卡片推送失败时使用）。中文优先，英文原题放括号里。"""
    title = title or f"📰 每日资讯日报 · {local_now().strftime('%Y年%m月%d日')}"
    lines = [title, ""]
    last_group = None
    for i, it in enumerate(order_for_display(items), 1):
        g = display_group(it)
        if g != last_group:
            last_group = g
            lines += ["", GOAL_LABEL.get(g, g), ""]
        zh = (it.get("summary_zh") or "").strip()
        raw_title = (it.get("title") or "").strip()
        title_is_zh = is_chinese_text(raw_title)
        if zh:
            lines.append(f"{i}. {clip(zh, 80)}")
            if not title_is_zh:
                lines.append(f"   原题：{clip(raw_title, 80)}")
        elif title_is_zh:
            lines.append(f"{i}. {clip(raw_title, 80)}")
            if it.get("desc") and is_chinese_text(it["desc"]):
                lines.append(f"   {clip(strip_tags(it['desc']), 80)}")
        else:
            lines.append(f"{i}. {clip(raw_title, 80)}")
            lines.append("   ⚠️ 未翻译（摘要服务未配置）")
        meta = [it.get("source") or ""]
        if it.get("stars_today"):
            meta.append(f"今日 +{it['stars_today']}★")
        lines.append(f"   {' · '.join(m for m in meta if m)}")
        lines.append(f"   {it.get('url')}")
    if footer:
        lines += ["", clip(footer, 300)]
    return "\n".join(lines)


def truncate_elements(card, max_elements=CARD_MAX_ELEMENTS):
    """超限保护：从尾部删元素（保留 header、保留最后一条 note 尽量不动）。

    实测正常 20 条只有 27 个元素，此函数只是保险。
    """
    if len(card["elements"]) <= max_elements:
        return card, 0
    note = card["elements"][-1] if card["elements"] and card["elements"][-1].get("tag") == "note" else None
    body = card["elements"][:-1] if note else list(card["elements"])
    removed = 0
    while len(body) + (1 if note else 0) > max_elements and body:
        body.pop()
        removed += 1
    card["elements"] = body + ([note] if note else [])
    return card, removed


def _is_ok_code(code):
    """判断飞书返回码是否为成功。

    必须容忍字符串形式（部分网关/SDK 回传 "0"），否则合法的成功会被误判为失败。
    """
    if code is None:
        return False
    if isinstance(code, bool):
        return False
    if isinstance(code, (int, float)):
        return int(code) == 0
    try:
        return int(str(code).strip()) == 0
    except Exception:
        return False


def test_push(cfg, dry_run=False):
    """发送一条测试卡片，用于验证 webhook / 加签 / 权限配置是否正确。

    不写台账、不抓取任何数据，因此不会影响当天的正式推送。
    返回 True 表示飞书已确认接收（body.code == 0）。
    """
    hook = ((cfg.get("feishu") or {}).get("webhook") or "").strip()
    secret = ((cfg.get("feishu") or {}).get("secret") or "").strip()
    if not hook:
        LOG.error("未配置 feishu.webhook")
        return False

    now = local_now().strftime("%Y-%m-%d %H:%M:%S")
    card = {
        "header": {"title": {"tag": "plain_text", "content": "✅ 每日资讯推送 · 连接测试"}},
        "elements": [
            {"tag": "markdown",
             "content": f"**这是一条测试消息**\n收到它说明 webhook 配置正确。\n"
                        f"发送时间：{now}"},
            {"tag": "markdown",
             "content": f"加签：{'已启用' if secret else '未启用（secret 为空）'}\n"
                        f"卡片元素数：2（上限 200，本方案实际日报约 27）"},
            {"tag": "note", "elements": [{"tag": "plain_text",
                                          "content": "此消息不写入去重台账，不影响正式日报"}]},
        ],
    }
    payload = {"msg_type": "interactive", "card": card}
    if secret:
        payload["timestamp"], payload["sign"] = feishu_sign(secret)

    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:1200])
        return True

    status, body = http_post_json(hook, payload, cfg)
    code = (body or {}).get("code")
    msg = (body or {}).get("msg")
    if _is_ok_code(code):
        print(f"✅ 测试推送成功（HTTP {status}，飞书返回 code=0）。请检查飞书群是否收到卡片。")
        return True
    hint = FEISHU_ERR_HINTS.get(code) if isinstance(code, (int, str)) else ""
    print(f"❌ 测试推送失败：HTTP {status} code={code} msg={msg}")
    if hint:
        print(f"   可能原因：{hint}")
    if code == 19021:
        print("   排查：确认 secret 与机器人『签名校验』里复制的密钥完全一致（无多余空格）。")
    elif code == 19024:
        print("   排查：机器人开启了『自定义关键词』，需把该关键词放进消息正文。")
    elif code == 19001:
        print("   排查：webhook URL 不完整或被截断，应以 .../open-apis/bot/v2/hook/ 开头。")
    return False


def push_feishu(cfg, items, dry_run=False, footer=None):
    """推送日报。返回 (成功?, 结果描述, 使用的方式, 实际推送的条目列表)。

    流程：卡片（含体积/元素自检）→ 失败降级纯文本 → 仍失败则报错。
    成功判定：**必须 body.code == 0**（实测 HTTP 200 也会返回 19001 等失败）。

    第 4 个返回值很重要：若因体积超限被截断，调用方**只能把实际推送的条目写入台账**，
    否则会出现「台账记了 20 条、实际只推了 12 条」→ 被漏推的条目永久丢失。
    """
    hook = ((cfg.get("feishu") or {}).get("webhook") or "").strip()
    secret = ((cfg.get("feishu") or {}).get("secret") or "").strip()
    sent = list(items)

    def build(seq):
        c = render_card(seq, footer=footer)
        c, removed = truncate_elements(c)
        if removed:
            LOG.warning("卡片元素超限，已删除尾部 %d 个元素", removed)
        return c, {"msg_type": "interactive", "card": c}

    card, payload = build(sent)
    size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    LOG.info("卡片：%d 个元素 / %.1f KB", len(card["elements"]), size / 1024)

    truncated = False
    while size > CARD_MAX_BYTES and len(sent) > 5:
        sent = sent[:-1]
        truncated = True
        card, payload = build(sent)
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if truncated:
        LOG.warning("卡片超过 %d 字节阈值，已按条数截断为 %d 条 / %.1f KB",
                    CARD_MAX_BYTES, len(sent), size / 1024)

    if dry_run:
        return True, "dry-run 跳过实际推送", "none", sent
    if not hook:
        return False, "未配置 feishu.webhook", "none", []
    if not sent:
        return False, "无内容可推送", "none", []

    # 加签在重试闭包之外构造（否则每次重试都会生成不同签名）
    if secret:
        payload["timestamp"], payload["sign"] = feishu_sign(secret)

    status, body = http_post_json(hook, payload, cfg)
    code = (body or {}).get("code")
    if _is_ok_code(code):
        return True, f"卡片推送成功（HTTP {status}）", "card", sent

    msg = (body or {}).get("msg") or str(body)[:200]
    hint = FEISHU_ERR_HINTS.get(code) if isinstance(code, (int, str)) else ""
    LOG.warning("卡片推送失败：HTTP %s code=%s msg=%s %s", status, code, msg,
                f"（{hint}）" if hint else "")

    # 降级为纯文本
    text_payload = {"msg_type": "text",
                    "content": {"text": render_text(sent, footer=footer)}}
    if secret:
        text_payload["timestamp"], text_payload["sign"] = feishu_sign(secret)
    status2, body2 = http_post_json(hook, text_payload, cfg)
    code2 = (body2 or {}).get("code")
    if _is_ok_code(code2):
        return True, f"卡片失败（code={code}）已降级为纯文本并推送成功", "text", sent
    return False, (f"卡片与纯文本均失败：card code={code} msg={msg}；"
                   f"text code={code2} msg={(body2 or {}).get('msg')}"), "none", []
