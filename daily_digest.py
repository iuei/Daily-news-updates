#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日资讯推送 · 主程序

用法：
  python daily_digest.py                 # 正常运行（工作日 08:35 由计划任务调用）
  python daily_digest.py --dry-run       # 只抓取/组版并打印，不推送、不写台账
  python daily_digest.py --preview       # 输出 Markdown 预览文件（便于手机外查看）
  python daily_digest.py --selftest      # 离线单元测试（不联网）
  python daily_digest.py --force         # 忽略「今天已推送」检查

退出码：0 正常（含"无新内容不推送"）；1 失败。
"""

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import digestlib as D  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------

def collect_sources(cfg, now_utc, window_hours, stats):
    """并发抓取所有源。单源失败只记日志并返回空列表，绝不拖垮整批。"""
    src_cfg = cfg.get("sources") or {}
    tasks = []  # (group, name, callable)

    gt = src_cfg.get("github_trending") or {}
    if gt.get("enabled", True):
        tasks.append(("github", "GitHub Trending", lambda: fetch_trending(cfg, gt, stats)))

    hn = src_cfg.get("hacker_news") or {}
    if hn.get("enabled", True):
        tasks.append(("hn", "Hacker News", lambda: D.fetch_hacker_news(cfg, window_hours)))

    for key in ("gn_ai", "official", "media"):
        for src in (src_cfg.get(key) or []):
            if not src.get("enabled", True):
                continue
            tasks.append((key, src["name"],
                          (lambda s: (lambda: fetch_rss(cfg, s, stats)))(src)))

    results = {"github": [], "hn": [], "gn_ai": [], "official": [], "media": []}
    workers = min(8, max(1, len(tasks)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn): (grp, name) for grp, name, fn in tasks}
        for fut in as_completed(futures):
            grp, name = futures[fut]
            try:
                items = fut.result() or []
                results[grp].extend(items)
                stats["sources"][name] = len(items)
                D.LOG.info("  %-18s %d 条", name, len(items))
            except Exception as e:  # noqa: BLE001
                stats["sources"][name] = 0
                stats["failed"].append(f"{name}: {type(e).__name__}: {e}")
                D.LOG.warning("  %-18s 失败：%s", name, str(e)[:140])
    return results


def fetch_trending(cfg, gt_cfg, stats):
    url = gt_cfg.get("url") or "https://github.com/trending?since=daily"
    raw = D.http_get(url, cfg)
    page = raw.decode("utf-8", "replace")
    items = D.parse_github_trending(page)
    min_expected = int(gt_cfg.get("min_expected", 5))
    if len(items) < min_expected:
        # 漂移告警：绝不静默变瘦（所有调研过的项目都缺这一步）
        stats["alerts"].append(
            f"GitHub Trending 仅解析出 {len(items)} 条（预期 ≥{min_expected}），"
            f"页面结构可能已变更")
        D.LOG.warning("Trending 解析条数异常：%d < %d", len(items), min_expected)
    return items


def fetch_rss(cfg, src, stats):
    raw = D.http_get(src["url"], cfg)
    per_stats = {}
    items = D.parse_feed(raw, src["name"], per_stats)
    if per_stats.get("raw", 0) == 0:
        stats["alerts"].append(f"{src['name']} 未解析出任何条目（feed 可能已变更）")
    if per_stats.get("repaired"):
        D.LOG.debug("%s XML 需容错清洗后解析", src["name"])
    return items


def apply_low_value_filter(cfg, groups, stats):
    """剔除促销/导购等低价值条目（实测 Wired 会推送大量优惠券内容）。

    只在『其他热点』与『中文 AI』组生效；GitHub Trending 与 HN 不受影响。
    """
    if not (cfg.get("digest") or {}).get("filter_low_value", True):
        return groups
    removed = 0
    for key in ("media", "gn_ai"):
        if key not in groups:
            continue
        kept = []
        for it in groups[key]:
            if D.is_low_value(it):
                removed += 1
                D.LOG.debug("剔除低价值条目：%s", (it.get("title") or "")[:60])
            else:
                kept.append(it)
        groups[key] = kept
    stats["low_value_removed"] = stats.get("low_value_removed", 0) + removed
    return groups


# ---------------------------------------------------------------------------
# 过滤 / 分组
# ---------------------------------------------------------------------------

def filter_and_dedupe(items, window_hours, now_utc, ledger, stats, skip_ledger=False):
    """时窗过滤 + 跨源 URL 归一化去重 + 台账去重。"""
    lo = now_utc - timedelta(hours=window_hours)
    epochs = [i for i in items if i.get("ts") is None]
    fresh = [i for i in items if i.get("ts") is not None and i["ts"] >= lo]
    stats["raw"] = len(items)
    stats["no_timestamp"] = len(epochs)
    stats["in_window"] = len(fresh)

    # 跨源同链接去重：保留信息量最大的（描述最长者）
    fresh.sort(key=lambda x: (len(x.get("desc") or ""), x.get("ts") or now_utc), reverse=True)
    seen, uniq = set(), []
    for it in fresh:
        k = D.url_key(it.get("url"))
        if k in seen:
            stats["dup_cross_source"] += 1
            continue
        seen.add(k)
        uniq.append(it)

    # 台账去重（推过的永不重复）
    out = []
    for it in uniq:
        if not skip_ledger and ledger.seen(it):
            stats["dup_ledger"] += 1
            continue
        out.append(it)
    stats["after_dedupe"] = len(out)
    return out


def group_items(items):
    """按来源归入配额分组。

    中文源与官方博客内部按『AI 相关度优先』排序（命中强特征词越多越靠前），
    再按时间倒序。原因：中文源里混杂大量非 AI 内容（实测少数派 0/4、掘金 7/20、
    InfoQ 10/20 是 AI），若单纯按时间排序，会把「前端拖拽选型」这类无关内容选进 AI 组。
    """
    groups = {"github": [], "official": [], "hn": [], "cn_ai": [], "media": []}
    official_names = _source_names("official")
    media_names = _source_names("media")

    for it in items:
        if it.get("group") == "github" or it.get("source") == "GitHub Trending":
            groups["github"].append(it)
        elif it.get("group") == "hn":
            groups["hn"].append(it)
        elif it.get("source") in official_names:
            groups["official"].append(it)
        elif it.get("source") in media_names:
            groups["media"].append(it)
        else:
            groups["cn_ai"].append(it)

    ts_min = datetime_min()
    for it in groups["cn_ai"] + groups["official"]:
        strong, hits = D.score_ai(it)
        it["ai_strong"] = strong
        it["ai_hits"] = len(hits)

    # HN 是「关键词搜索」来的，会混入非 AI 内容（实测出现过 Porsche Mission S、
    # 气候报告、Chromium 包等）。按 AI 相关度过滤：只保留强命中或命中 ≥2 个邻域词的。
    hn_total = len(groups["hn"])
    hn_kept = []
    for it in groups["hn"]:
        strong, hits = D.score_ai(it)
        it["ai_strong"] = strong
        it["ai_hits"] = len(hits)
        if strong or len(hits) >= 2:
            hn_kept.append(it)
    if hn_total and len(hn_kept) < hn_total:
        D.LOG.info("HN 条目 AI 相关度过滤：%d → %d 条（剔除 %d 条非 AI 内容）",
                   hn_total, len(hn_kept), hn_total - len(hn_kept))
    groups["hn_unfiltered"] = groups["hn"]
    groups["hn"] = hn_kept

    groups["github"].sort(key=lambda x: x.get("ts") or ts_min, reverse=True)
    groups["hn"].sort(key=lambda x: (x.get("points") or 0, x.get("ts") or ts_min), reverse=True)
    groups["hn_unfiltered"].sort(key=lambda x: (x.get("points") or 0, x.get("ts") or ts_min),
                                 reverse=True)
    groups["media"].sort(key=lambda x: x.get("ts") or ts_min, reverse=True)
    # AI 相关度优先 → 时间次之
    for key in ("cn_ai", "official"):
        groups[key].sort(key=lambda x: (1 if x.get("ai_strong") else 0,
                                        x.get("ai_hits", 0),
                                        x.get("ts") or ts_min),
                         reverse=True)
    return groups


_CONFIG_REF = {"cfg": None}


def _source_names(groups_of):
    cfg = _CONFIG_REF.get("cfg") or {}
    src = (cfg.get("sources") or {})
    if groups_of == "official":
        return {s["name"] for s in (src.get("official") or [])}
    if groups_of == "media":
        return {s["name"] for s in (src.get("media") or [])}
    return {s["name"] for s in (src.get("gn_ai") or [])}


def datetime_min():
    from datetime import datetime
    return datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(args):
    cfg_path = args.config or os.path.join(BASE_DIR, "config.json")
    cfg = D.load_config(cfg_path)
    _CONFIG_REF["cfg"] = cfg

    files = cfg.get("files") or {}
    log_path = os.path.join(BASE_DIR, files.get("log", "logs/digest.log"))
    D.setup_logging(log_path, verbose=args.verbose)

    D.LOG.info("=" * 72)
    D.LOG.info("每日资讯推送 v%s 启动（config=%s）", D.VERSION, cfg_path)

    errors = D.validate_config(cfg, need_feishu=not args.dry_run)
    if errors:
        for e in errors:
            D.LOG.error("配置错误：%s", e)
        if not args.dry_run:
            return 1
        D.LOG.warning("dry-run 模式下忽略飞书配置问题")

    now_local = D.local_now()
    now_utc = now_local.astimezone(timezone.utc)

    ledger = D.Ledger(os.path.join(BASE_DIR, files.get("ledger", "data/ledger.json"))).load()

    # 时窗：周一自适应 + 按「距上次成功推送的天数」自动补跑
    window_hours, catchup_note = D.compute_window_hours_with_catchup(cfg, ledger, now_local)
    D.LOG.info("本地时间 %s | 时窗 %dh%s",
               now_local.strftime("%Y-%m-%d %H:%M:%S %Z"), window_hours,
               f"（{catchup_note}）" if catchup_note else "")
    if catchup_note:
        D.LOG.warning("%s", catchup_note)

    # --force = 测试模式：忽略「当日已推送」检查并跳过台账去重，但**不写台账**，
    # 因此永远不会污染幂等状态。正式场景请勿使用（会多推一张卡）。
    if ledger.pushed_today(now_local) and not args.dry_run and not args.force:
        D.LOG.info("今天已成功推送过，跳过。台账共 %d 次运行记录", len(ledger.data.get("runs", [])))
        return 0
    if args.force and not args.dry_run:
        D.LOG.warning("--force 测试模式：会实际推送，但忽略去重且不写台账（可能重复推送已发过的内容）")

    lock = None
    if not args.dry_run and not args.selftest:
        lock_path = os.path.join(BASE_DIR, files.get("lock", "data/daily.lock"))
        if D.lock_is_stale(lock_path):
            D.LOG.warning("发现过期锁文件（>6h），清除：%s", lock_path)
            try:
                os.remove(lock_path)
            except OSError:
                pass
        lock = D.FileLock(lock_path)
        if not lock.acquire():
            D.LOG.error("已存在运行中的实例（锁文件 %s），本次退出以避免重复推送", lock_path)
            return 0

    stats = {"sources": {}, "failed": [], "alerts": [], "dup_cross_source": 0,
             "dup_ledger": 0, "raw": 0, "in_window": 0, "after_dedupe": 0,
             "no_timestamp": 0}
    try:
        D.LOG.info("开始抓取 %d 个源 ...", _count_sources(cfg))
        groups_raw = collect_sources(cfg, now_utc, window_hours, stats)

        all_items = [i for v in groups_raw.values() for i in v]
        if args.force:
            D.LOG.warning("--force：测试模式，忽略台账去重（未推送过的条目集合可能不同）")
        kept = filter_and_dedupe(all_items, window_hours, now_utc, ledger, stats,
                                 skip_ledger=args.force)
        D.LOG.info("时窗 %dh：抓取 %d → 时窗内 %d → 去重后 %d（跨源重复 %d，台账命中 %d）",
                   window_hours, stats["raw"], stats["in_window"], stats["after_dedupe"],
                   stats["dup_cross_source"], stats["dup_ledger"])

        groups = group_items(kept)
        groups = apply_low_value_filter(cfg, groups, stats)
        if stats.get("low_value_removed"):
            D.LOG.info("已剔除 %d 条促销/低价值内容", stats["low_value_removed"])
        chosen, report = D.allocate_quota(cfg, groups, window_hours)
        D.LOG.info("配额：github %d | ai_official %d | ai_hn %d | ai_cn %d | media %d | 合计 %d",
                   report.get("github", 0), report.get("ai_official", 0),
                   report.get("ai_hn", 0), report.get("ai_cn", 0),
                   report.get("media", 0), report.get("total", 0))

        if not chosen:
            D.LOG.info("无新增内容，不推空卡片（正常退出）")
            return 0

        # 唯一一次 LLM 调用
        summaries = D.generate_summaries(cfg, chosen)
        hit, miss = D.apply_summaries(chosen, summaries)
        D.LOG.info("摘要：LLM %d 条 / 降级 %d 条", hit, miss)

        # 若 LLM 不可用且条目多为英文，给一次中文源补充机会（保证可读性）
        if hit == 0 and not (cfg.get("deepseek") or {}).get("api_key"):
            cn = [c for c in chosen if D.is_chinese_text(c.get("title", ""))]
            D.LOG.info("摘要降级模式：%d/%d 条为中文标题（无需翻译）", len(cn), len(chosen))

        footer = build_footer(chosen, window_hours, hit, miss)

        ok, desc, mode, sent_items = D.push_feishu(cfg, chosen, dry_run=args.dry_run,
                                                   footer=footer)
        D.LOG.info("推送结果：%s（方式=%s，实际推送 %d 条）", desc, mode, len(sent_items))

        if args.dry_run or args.preview:
            write_preview(cfg, sent_items or chosen, footer)

        if not ok:
            D.LOG.error("推送失败，台账未写入（下次运行会重试同一批内容）")
            return 1

        # 只在推送成功后写台账，且只记「实际推送出去」的条目：
        # 若卡片因体积被截断，未推送的条目下次仍会入选，不会永久丢失。
        # --force（测试模式）不写台账，避免污染幂等状态。
        if not args.dry_run and not args.force:
            dropped = len(chosen) - len(sent_items)
            if dropped:
                D.LOG.warning("有 %d 条因体积截断未推送，不写入台账（下次运行会重试）", dropped)
            ledger.record(sent_items, now_local)
            removed = ledger.prune(now_local)
            ledger.save()
            D.LOG.info("台账已写入：本次 %d 条，清理历史 %d 次运行", len(sent_items), removed)
        elif args.force:
            D.LOG.warning("--force 测试模式：台账未写入（%d 条不会去重）", len(sent_items))

        if stats["failed"]:
            D.LOG.warning("本次失败的源 %d 个：%s", len(stats["failed"]),
                          "；".join(stats["failed"])[:300])
        for a in stats["alerts"]:
            D.LOG.warning("告警：%s", a)

        D.LOG.info("本次运行完成：入选 %d 条", len(chosen))
        return 0
    except Exception as e:  # noqa: BLE001
        D.LOG.exception("运行异常：%s", e)
        return 1
    finally:
        if lock:
            lock.release()


def _count_sources(cfg):
    src = cfg.get("sources") or {}
    n = len([s for k in ("gn_ai", "official", "media") for s in (src.get(k) or [])
             if s.get("enabled", True)])
    if (src.get("github_trending") or {}).get("enabled", True):
        n += 1
    if (src.get("hacker_news") or {}).get("enabled", True):
        n += 1
    return n


def build_footer(items, window_hours, hit, miss):
    """页脚用大白话，不出现技术术语（读者只看中文）。

    untranslated = 既没有中文摘要、也不是中文标题的条目 —— 这些是读者看不懂的，
    必须在页脚显式告知条数，避免"以为都读了、其实漏了"。
    """
    total = len(items)
    untranslated = 0
    for i in items:
        has_zh = bool((i.get("summary_zh") or "").strip()) and i.get("summary_is_zh", True)
        if not has_zh and not D.is_chinese_text(i.get("title") or ""):
            untranslated += 1
    parts = [f"共 {total} 条"]
    if untranslated:
        parts.append(f"其中 {untranslated} 条未翻译（服务异常，下次恢复）")
    else:
        parts.append("全部已有中文摘要")
    if window_hours > 24:
        parts.append(f"已回看 {window_hours // 24} 天，覆盖周末/假期")
    parts.append("已去重，不会重复推送")
    return " · ".join(parts)


def write_preview(cfg, items, footer):
    files = cfg.get("files") or {}
    path = os.path.join(BASE_DIR, files.get("preview", "logs/preview.md"))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    lines = [f"# 每日资讯日报预览 · {D.local_now().strftime('%Y-%m-%d %H:%M')}", ""]
    last = None
    # 与卡片保持一致：板块归一（AI 合并为一块）+ 固定板块顺序
    for i, it in enumerate(D.order_for_display(items), 1):
        g = D.display_group(it)
        if g != last:
            last = g
            lines += ["", f"## {D.GOAL_LABEL.get(g, g)}", ""]
        zh = (it.get("summary_zh") or "").strip()
        raw_title = (it.get("title") or "").strip()
        title_is_zh = D.is_chinese_text(raw_title)
        if zh and it.get("summary_is_zh", True):
            lines.append(f"{i}. **{D.clip(zh, 90)}**  `{it.get('source')}`")
            if not title_is_zh:
                lines.append(f"   - 原题：{D.clip(raw_title, 90)}")
        elif title_is_zh:
            lines.append(f"{i}. **{D.clip(raw_title, 90)}**  `{it.get('source')}`")
            if zh:
                lines.append(f"   - {D.clip(zh, 90)}")
        else:
            lines.append(f"{i}. {D.clip(raw_title, 90)}  `{it.get('source')}`")
            lines.append("   - ⚠️ 未翻译（需配置摘要服务）")
        lines.append(f"   - {it.get('url')}")
    lines += ["", "---", footer]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    D.LOG.info("预览已写入：%s", path)
    print("\n" + D.render_text(items, footer=footer))


# ---------------------------------------------------------------------------
# 离线自检
# ---------------------------------------------------------------------------

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>T</title>
<item><title>RSS 条目</title><link>https://ex.com/b?utm_source=x</link>
<pubDate>Thu, 16 Oct 2026 01:02:03 GMT</pubDate>
<description>&lt;p&gt;正文&lt;/p&gt;</description><guid>https://ex.com/b</guid></item>
</channel></rss>"""

SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>T</title>
<entry><title>Atom 条目</title>
<link rel="self" href="https://ex.com/self"/>
<link rel="alternate" href="https://ex.com/a"/>
<published>2026-10-16T01:02:03Z</published>
<summary>&lt;b&gt;摘要&lt;/b&gt; 内容</summary></entry></feed>"""

SAMPLE_RDF = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<item><title>RDF 条目</title><link>https://ex.com/c</link>
<dc:date>2026-10-16T05:06:07+00:00</dc:date><description>RDF 摘要</description></item></rdf:RDF>"""

SAMPLE_BAD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><item><title>A &amp; B</title>
<link>https://ex.com/d</link><pubDate>Thu, 16 Oct 2026 01:02:03 GMT</pubDate>
<description>a & b 未转义</description></item></channel></rss>"""

SAMPLE_TRENDING = """<html><body>
<article class="Box-row">
  <h2><a href="/owner/repo-one">owner / repo-one</a></h2>
  <p class="col-9 color-fg-muted my-1 tmp-pr-4">  A test repository description.  </p>
  <div class="f6 color-fg-muted mt-2">
    <span class="repo-language-color"></span>
    <span itemprop="programmingLanguage">Python</span>
    <a href="/owner/repo-one/stargazers">1,234</a>
    <a href="/owner/repo-one/forks">56</a>
    <span> 789 stars today </span>
  </div>
</article>
<article class="Box-row">
  <h2><a href="/owner/repo-two">owner / repo-two</a></h2>
  <p class="col-9 color-fg-muted my-1 tmp-pr-4">Second repo.</p>
  <div class="f6 color-fg-muted mt-2">
    <span itemprop="programmingLanguage">Go</span>
    <a href="/owner/repo-two/stargazers">42</a>
    <span> 7 stars today </span>
  </div>
</article>
</body></html>"""


def selftest():
    """离线单元测试（覆盖需求文档 §7 验收标准 1~9，外加推送/台账/测试卡片三组）。"""
    from datetime import datetime, timezone
    import contextlib
    import io
    failures = []
    checks = 0
    _lg = logging.getLogger("digest")
    _saved_level = _lg.level
    _lg.setLevel(logging.CRITICAL)   # 自检期间静音业务日志，输出只留 [PASS]/[FAIL]

    @contextlib.contextmanager
    def quiet():
        """抑制被测函数自身的 print 噪音（自检的 [PASS] 由 check() 直接写真实 stdout）。"""
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            yield sink

    def check(cond, label, extra=""):
        nonlocal checks
        checks += 1
        if cond:
            print(f"  [PASS] {label}")
        else:
            print(f"  [FAIL] {label} {extra}")
            failures.append(label)

    print("== 1. RSS 2.0 与 Atom 两种格式解析 ==")
    rss = D.parse_feed(SAMPLE_RSS, "T")
    atom = D.parse_feed(SAMPLE_ATOM, "T")
    check(len(rss) == 1 and rss[0]["title"] == "RSS 条目", "RSS 2.0 解析")
    check(rss[0]["desc"] == "正文", "RSS description 去标签", repr(rss[0]["desc"]))
    check(len(atom) == 1 and atom[0]["url"] == "https://ex.com/a", "Atom 解析 + link rel 优先级",
          atom[0]["url"] if atom else "empty")
    check(atom[0]["ts"] is not None and atom[0]["ts"].year == 2026, "Atom 时间解析")

    print("\n== 2. GitHub Trending HTML 解析 ==")
    gh = D.parse_github_trending(SAMPLE_TRENDING)
    check(len(gh) == 2, "切出 2 个仓库块", f"got {len(gh)}")
    check(gh[0]["url"] == "https://github.com/owner/repo-one", "仓库 URL")
    check(gh[0]["desc"].startswith("A test repository"), "描述提取", repr(gh[0]["desc"]))
    check(gh[0]["lang"] == "Python" and gh[1]["lang"] == "Go", "语言提取")
    check(gh[0]["stars_total"] == "1,234", "总星数（含千分位）", gh[0]["stars_total"])
    check(gh[0]["stars_today"] == "789", "今日新增星", gh[0]["stars_today"])

    print("\n== 3. 时间窗边界 ==")
    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    cases = [("正好24h", timedelta(hours=24), True), ("23h59m", timedelta(hours=23, minutes=59), True),
             ("24h01m", timedelta(hours=24, minutes=1), False),
             ("正好72h", timedelta(hours=72), False), ("72h01m", timedelta(hours=72, minutes=1), False)]
    for label, delta, expect in cases:
        got = (now - (now - delta)) <= timedelta(hours=24)
        check(got == expect, f"时窗边界 {label}")
    check(D.compute_window_hours({"digest": {"window_hours": 24, "window_hours_monday": 72,
                                             "window_hours_max": 72}},
                                 datetime(2026, 10, 19, 9, 0)) == 72, "周一回看 72h")
    check(D.compute_window_hours({"digest": {"window_hours": 24, "window_hours_monday": 72,
                                             "window_hours_max": 72}},
                                 datetime(2026, 10, 20, 9, 0)) == 24, "周二回看 24h")

    print("\n== 4. URL 归一化 ==")
    same = [
        ("跟踪参数", "https://ex.com/a?utm_source=x&utm_medium=y", "https://ex.com/a"),
        ("fbclid", "https://ex.com/a?fbclid=1", "https://ex.com/a"),
        ("www 前缀", "https://www.ex.com/a", "https://ex.com/a"),
        ("末尾斜杠", "https://ex.com/a/", "https://ex.com/a"),
        ("fragment", "https://ex.com/a#s1", "https://ex.com/a"),
        ("主机大小写", "https://EX.com/a", "https://ex.com/a"),
        ("默认端口", "https://ex.com:443/a", "https://ex.com/a"),
        ("参数顺序", "https://ex.com/a?id=1&p=2", "https://ex.com/a?p=2&id=1"),
        ("空 URL", "", ""),
    ]
    for label, u1, u2 in same:
        check(D.url_key(u1) == D.url_key(u2), f"归一化等价：{label}")
    diff = [
        ("http vs https", "http://ex.com/a", "https://ex.com/a"),
        ("不同路径", "https://ex.com/a", "https://ex.com/b"),
        ("有效参数不同", "https://ex.com/a?id=1", "https://ex.com/a?id=2"),
        ("GitHub 仓库大小写敏感", "https://github.com/OpenAI/gpt", "https://github.com/openai/gpt"),
    ]
    for label, u1, u2 in diff:
        check(D.url_key(u1) != D.url_key(u2), f"归一化区分：{label}")

    print("\n== 5. 跨源同链接去重 + 台账命中 ==")
    k = D.url_key("https://ex.com/a?utm_source=hn")
    check(k == D.url_key("https://ex.com/a"), "跨源同链接判定为同一键")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        lp = os.path.join(td, "ledger.json")
        lg = D.Ledger(lp).load()
        item = {"title": "X", "url": "https://ex.com/a", "desc": "d"}
        check(not lg.seen(item), "新条目未见过")
        lg.record([item], D.local_now())
        lg.save()
        lg2 = D.Ledger(lp).load()
        check(lg2.seen(item), "写入后台账命中")
        check(lg2.seen({"title": "X", "url": "https://ex.com/a?utm_source=t", "desc": "d"}),
              "带跟踪参数的同一 URL 也命中")
        check(lg2.seen({"title": "X", "url": "https://other.com/z", "desc": "d"}),
              "内容哈希命中（URL 变了）")

    print("\n== 6. AI 关键词打分（不得误命中 chair/said）==")
    must_hit = ["OpenAI releases GPT-5", "The chair said the AI model is ready",
                "Llama 4 is out", "Comparison of RAG pipelines"]
    must_miss = ["She said it again", "A chair for sale", "Understanding the domain name system",
                 "Email marketing best practices", "Fixing a broken chain",
                 "The mainframe is back", "Available now: cheaper GPUs",
                 "BeBox is a Haiku application"]
    for t in must_hit:
        got, _ = D.score_ai({"title": t, "desc": ""})
        check(got, f"应命中：{t[:40]}")
    for t in must_miss:
        got, _ = D.score_ai({"title": t, "desc": ""})
        check(not got, f"不应命中：{t[:40]}")

    print("\n== 7. 配额与补齐（总数为 20）==")
    cfg = {"digest": {"max_items": 20, "quota_github": 5, "quota_ai": 10, "quota_media": 5,
                      "reserve_cn_ai": 3, "max_per_source": 2},
           "ai_filter": {"hn_min_points": 10, "ai_host_points_72h": 30}}
    groups = {
        "github": [{"title": f"g{i}", "url": f"https://g.com/{i}", "group": "github"}
                   for i in range(13)],
        "official": [{"title": f"o{i}", "url": f"https://o.com/{i}", "source": "OpenAI 官方"}
                     for i in range(2)],
        "hn": [{"title": f"h{i}", "url": f"https://h.com/{i}", "source": "Hacker News",
                "points": 100 - i} for i in range(20)],
        "cn_ai": [{"title": f"c{i}", "url": f"https://c.com/{i}", "source": f"中文源{i}"}
                  for i in range(10)],
        "media": [{"title": f"m{i}", "url": f"https://m.com/{i}", "source": f"媒体{i % 2}"}
                  for i in range(30)],
    }
    chosen, rep = D.allocate_quota(cfg, groups, 24)
    check(rep["github"] == 5, "GitHub 取 5 条", rep.get("github"))
    ai_total = sum(v for k, v in rep.items() if k.startswith("ai_"))
    check(ai_total == 10, "AI 组取 10 条", rep)
    check(rep.get("ai_cn", 0) >= 3, "中文源保底 ≥3 条", rep.get("ai_cn"))
    check(rep["media"] == 5, "媒体取 5 条", rep.get("media"))
    check(len(chosen) == 20, "总数为 20", len(chosen))
    check(len({id(x) for x in chosen}) == 20, "无重复条目")
    # 单源集中度：媒体源只有 2 个，每个上限 2 → 媒体位会不足，应被其他组回填到 20
    check(max(rep["by_source"].values()) <= 5, "单源集中度受限", rep["by_source"])
    # 官方源不足时的补齐
    groups2 = dict(groups)
    groups2["official"] = []
    chosen2, rep2 = D.allocate_quota(cfg, groups2, 24)
    check(len(chosen2) == 20, "官方源为 0 时仍能凑满 20", len(chosen2))
    # HN 也为空时，仅靠中文源 + 媒体也要尽量凑
    groups3 = dict(groups)
    groups3["official"] = []
    groups3["hn"] = []
    chosen3, rep3 = D.allocate_quota(cfg, groups3, 24)
    check(len(chosen3) >= 15, "官方+HN 均空时仍有兜底条数", len(chosen3))

    print("\n== 8. 卡片渲染 + 元素数上限 ==")
    items = []
    for i in range(20):
        items.append({"title": f"标题 {i}" * 3, "url": f"https://ex.com/{i}",
                      "summary_zh": "这是一句中文摘要。" * 4, "source": "TechCrunch",
                      "group_final": "media"})
    card = D.render_card(items, footer="共 20 条")
    n = len(card["elements"])
    check(n <= D.CARD_MAX_ELEMENTS, f"元素数 {n} ≤ {D.CARD_MAX_ELEMENTS}")
    size = len(json.dumps({"msg_type": "interactive", "card": card},
                          ensure_ascii=False).encode("utf-8"))
    check(size <= D.CARD_MAX_BYTES, f"报文 {size} 字节 ≤ {D.CARD_MAX_BYTES}")
    check(card["header"]["title"]["tag"] == "plain_text", "header 结构")
    big = {"header": card["header"], "elements": [{"tag": "markdown", "content": "x"}] * 60}
    trimmed, removed = D.truncate_elements(big)
    check(len(trimmed["elements"]) <= D.CARD_MAX_ELEMENTS and removed > 0,
          f"超限截断生效（删除 {removed} 个）")
    check(all(e.get("tag") != "table" for e in card["elements"]), "不使用 table 元素")

    print("\n== 9. 飞书签名算法与官方向量一致 ==")
    ts, sign = D.feishu_sign("a", "1")
    check(ts == "1", "时间戳原样传递")
    check(sign == "rWZ84lcag1x9aBFhn1gtV4ZN+4gme3pilfQNMk86vKg=",
          "官方向量 gen_sign('a',1)", sign)
    ts2, sign2 = D.feishu_sign("demo", "1599360473")
    check(sign2 == "l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8=",
          "向量 gen_sign('demo',1599360473)（key=ts\\nsecret, msg=b''）", sign2)
    ts3, sign3 = D.feishu_sign("xxx", 1661860880)
    check(sign3 == "QnWVTSBe6FmQDE0bG6X0mURbI+DnvVyu1h+j5dHOjrU=",
          "go-lark 向量 gen_sign('xxx',1661860880)", sign3)

    print("\n== 10. 推送成功判定与台账只记实际推送内容 ==")
    for code, expect in [(0, True), ("0", True), (0.0, True), (19001, False),
                         ("19001", False), (None, False), (True, False), ("abc", False)]:
        check(D._is_ok_code(code) == expect, f"成功码判定 code={code!r} → {expect}")
    # dry-run 分支：必须返回 4 元组，且 sent_items 非空
    fake_items = [{"title": f"T{i}", "url": f"https://ex.com/{i}", "summary_zh": "摘要",
                   "source": "TechCrunch", "group_final": "media"} for i in range(20)]
    ret = D.push_feishu({"feishu": {"webhook": ""}}, fake_items, dry_run=True)
    check(isinstance(ret, tuple) and len(ret) == 4, "push_feishu 返回 4 元组")
    check(ret[0] is True and len(ret[3]) == 20, "dry-run 返回全部 20 条")
    # 无 webhook 分支：必须失败且 sent_items 为空（避免误写台账）
    ret2 = D.push_feishu({"feishu": {"webhook": ""}}, fake_items, dry_run=False)
    check(ret2[0] is False and ret2[3] == [], "无 webhook 时失败且不返回可记账条目")
    # 超大卡片：截断后返回的条目数必须 ≤ 原数量，且长度自洽
    huge = []
    for i in range(20):
        huge.append({"title": "很长的标题" * 20, "url": f"https://ex.com/{i}",
                     "summary_zh": "很长的摘要内容" * 30, "source": "TechCrunch",
                     "group_final": "media"})
    ret3 = D.push_feishu({"feishu": {"webhook": ""}}, huge, dry_run=True)
    check(len(ret3[3]) <= 20, "截断后条目数不超过原始数量", len(ret3[3]))
    check(len(ret3[3]) >= 5, "截断保留至少 5 条", len(ret3[3]))

    print("\n== 11. 台账幂等（模拟推送成功后的写入）==")
    with tempfile.TemporaryDirectory() as td:
        lp = os.path.join(td, "ledger.json")
        lg = D.Ledger(lp).load()
        lg.record(fake_items[:3], D.local_now())
        lg.save()
        lg2 = D.Ledger(lp).load()
        check(all(lg2.seen(x) for x in fake_items[:3]), "已推送的 3 条全部命中台账")
        check(not lg2.seen(fake_items[5]), "未推送的条目未命中台账（下次仍会入选）")
        check(lg2.pushed_today(), "pushed_today() 判定为真（用于当日二次运行跳过）")
        # 只记实际推送的条目：截断后未推送的不入账
        lg3 = D.Ledger(os.path.join(td, "l2.json")).load()
        lg3.record(fake_items[:1], D.local_now())
        lg3.save()
        lg4 = D.Ledger(os.path.join(td, "l2.json")).load()
        check(len(lg4.data["runs"][0]["items"]) == 1, "台账仅记录传入的条目数")

        # prune 清理：datetime / date / None 三种入参都不得抛异常
        from datetime import date as _date, datetime as _dt
        for label, arg in (("datetime", _dt(2026, 10, 16, 12, 0)),
                           ("date", _date(2026, 10, 16)), ("None", None)):
            lg5 = D.Ledger(os.path.join(td, "l3.json"))
            lg5.data = {"version": 1, "retain_days": 90,
                        "runs": [{"date": "2026-10-16", "items": []},
                                 {"date": "2020-01-01", "items": []}]}
            try:
                removed5 = lg5.prune(arg)
                check(removed5 == 1 and len(lg5.data["runs"]) == 1,
                      f"prune({label}) 清理过期运行且不抛异常", f"removed={removed5}")
            except Exception as e:  # noqa: BLE001
                check(False, f"prune({label}) 不应抛异常", repr(e))
        lg6 = D.Ledger(os.path.join(td, "l4.json"))
        lg6.data = {"version": 1, "runs": [{"date": "坏日期", "items": []}]}
        lg6.prune(_date(2026, 10, 16))
        check(len(lg6.data["runs"]) == 1, "prune 对无法解析的日期予以保留")

    print("\n== 12. 真实推送路径（本地桩服务器，验证报文/加签/降级）==")
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received = []

    class _Stub(BaseHTTPRequestHandler):
        # 依次返回的响应；用尽后一直用最后一个
        replies = [{"code": 0, "msg": "success"}]
        idx = 0

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n)
            try:
                received.append(json.loads(body.decode("utf-8")))
            except Exception:
                received.append({"_raw": body[:200].decode("utf-8", "replace")})
            i = min(_Stub.idx, len(_Stub.replies) - 1)
            _Stub.idx += 1
            out = json.dumps(_Stub.replies[i]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    def _reset_stub(replies):
        _Stub.replies = replies
        _Stub.idx = 0
        received.clear()

    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    hook = f"http://127.0.0.1:{port}/hook/fake"

    try:
        # 12.1 卡片推送成功 + 加签字段齐全
        _reset_stub([{"code": 0, "msg": "success"}])
        cfg_push = {"feishu": {"webhook": hook, "secret": "test-secret"},
                    "network": {"timeout": 10, "retries": 2, "backoff_seconds": [1, 1]}}
        ok, desc, mode, sent = D.push_feishu(cfg_push, fake_items)
        check(ok and mode == "card", "卡片推送成功路径", f"{ok} {desc}")
        check(len(sent) == 20, "返回实际推送 20 条")
        check(len(received) == 1, "只发了一次请求")
        p = received[0]
        check(p.get("msg_type") == "interactive", "报文 msg_type=interactive")
        check("card" in p and "elements" in p["card"], "卡片体在顶层 card 字段")
        check("timestamp" in p and "sign" in p, "加签字段在报文顶层")
        exp_ts, exp_sign = D.feishu_sign("test-secret", p["timestamp"])
        check(p["sign"] == exp_sign, "签名与 secret+timestamp 匹配")
        check(len(p["card"]["elements"]) <= D.CARD_MAX_ELEMENTS, "元素数在阈值内")
        check(all(e.get("tag") != "table" for e in p["card"]["elements"]), "报文不含 table 元素")

        # 12.2 卡片被拒（230099）→ 纯文本成功（用响应序列模拟真实两连发）
        _reset_stub([{"code": 230099, "msg": "card table number over limit"},
                     {"code": 0, "msg": "success"}])
        ok2, desc2, mode2, sent2 = D.push_feishu(cfg_push, fake_items)
        check(ok2 and mode2 == "text", "卡片失败后降级为纯文本", f"{ok2} {mode2} {desc2}")
        check(len(received) == 2, "共发两次请求（卡片+纯文本）")
        check(received[1].get("msg_type") == "text", "第二次是 text 报文")
        check("sign" in received[1], "降级报文同样带签名")
        check(len(sent2) == 20, "降级后同样返回 20 条可记账")

        # 12.3 HTTP 200 但 code 非 0 → 视为失败（假成功防护）
        _reset_stub([{"code": 19021, "msg": "sign match fail"}])
        ok3, desc3, mode3, sent3 = D.push_feishu(cfg_push, fake_items)
        check(not ok3 and sent3 == [], "假成功被拦截（19021 两次都失败）")

        # 12.4 code 为字符串 "0" 也应判成功
        _reset_stub([{"code": "0", "msg": "ok"}])
        ok4, _, mode4, _ = D.push_feishu(cfg_push, fake_items)
        check(ok4 and mode4 == "card", "字符串 code='0' 判为成功")

        # 12.5 无 secret 时不应带 timestamp/sign
        _reset_stub([{"code": 0, "msg": "success"}])
        D.push_feishu({"feishu": {"webhook": hook, "secret": ""},
                       "network": {"timeout": 10, "retries": 1}}, fake_items)
        check("sign" not in received[0], "未配置 secret 时不带签名字段")

        # 12.6 服务端 5xx 触发重试，最终成功
        _reset_stub([{"code": 0}])
        calls = {"n": 0}

        class _Flaky(BaseHTTPRequestHandler):
            def do_POST(self):
                calls["n"] += 1
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                if calls["n"] == 1:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                out = b'{"code":0,"msg":"success"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        srv2 = HTTPServer(("127.0.0.1", 0), _Flaky)
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        try:
            ok6, desc6, mode6, _ = D.push_feishu(
                {"feishu": {"webhook": f"http://127.0.0.1:{srv2.server_address[1]}/hook/x"},
                 "network": {"timeout": 10, "retries": 3, "backoff_seconds": [1, 1, 1]}},
                fake_items)
            check(ok6 and calls["n"] == 2, "5xx 后自动重试并成功", f"{ok6} calls={calls['n']}")
        finally:
            srv2.shutdown()
            srv2.server_close()
    finally:
        srv.shutdown()
        srv.server_close()

    print("\n== 13. --test-push 测试卡片路径 ==")
    srv3 = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv3.serve_forever, daemon=True).start()
    try:
        hook3 = f"http://127.0.0.1:{srv3.server_address[1]}/hook/test"
        cfg_t = {"feishu": {"webhook": hook3, "secret": "s3cr3t"},
                 "network": {"timeout": 10, "retries": 2, "backoff_seconds": [1, 1]}}

        _reset_stub([{"code": 0, "msg": "success"}])
        with quiet():
            ok_t = D.test_push(cfg_t)
        check(ok_t is True, "test-push 成功返回 True")
        check(len(received) == 1, "test-push 只发一条")
        tp = received[0]
        check(tp.get("msg_type") == "interactive", "测试卡片 msg_type 正确")
        check("连接测试" in tp["card"]["header"]["title"]["content"], "测试卡片标题可识别")
        check("timestamp" in tp and "sign" in tp, "测试卡片带签名")
        _, exp_s = D.feishu_sign("s3cr3t", tp["timestamp"])
        check(tp["sign"] == exp_s, "测试卡片签名与 secret 匹配")
        check(all(e.get("tag") != "table" for e in tp["card"]["elements"]),
              "测试卡片不含 table 元素")

        _reset_stub([{"code": 19021, "msg": "sign match fail"}])
        with quiet():
            check(D.test_push(cfg_t) is False, "test-push 失败返回 False")

        # 未配置 webhook 时必须直接失败，且不发请求
        with quiet():
            check(D.test_push({"feishu": {"webhook": ""}}) is False,
                  "无 webhook 时 test-push 失败")

        # dry-run 不发送
        _reset_stub([{"code": 0}])
        n_before = len(received)
        with quiet():
            check(D.test_push(cfg_t, dry_run=True) is True, "test-push dry-run 返回 True")
        check(len(received) == n_before, "test-push dry-run 不发送请求")
    finally:
        srv3.shutdown()
        srv3.server_close()

    print("\n== 14. 中文优先版式（确保用户只读中文也能看懂）==")
    M_build_footer = build_footer
    # 14.1 有 LLM 中文摘要：中文作主体，英文原题降为灰色附注
    it_zh = {"title": "OpenAI releases GPT-5", "url": "https://ex.com/a",
             "summary_zh": "OpenAI 发布 GPT-5，推理能力显著提升。", "summary_source": "llm",
             "summary_is_zh": True, "source": "OpenAI 官方", "group_final": "ai_official"}
    card = D.render_card([it_zh], footer="测试")
    body = card["elements"][1]["content"] if card["elements"][0].get("tag") == "markdown" \
        and "AI 热点" in card["elements"][0]["content"] else card["elements"][0]["content"]
    joined = "\n".join(e.get("content", "") for e in card["elements"])
    check("OpenAI 发布 GPT-5" in joined, "中文摘要出现在卡片中")
    check(joined.index("OpenAI 发布 GPT-5") < joined.index("OpenAI releases GPT-5"),
          "中文在英文原题之前（中文优先）")
    check("原题：" in joined, "英文原题标注为『原题：』")
    check("font color='grey'" in joined, "英文原题使用灰色小字")
    check("AI 摘要" in joined, "标注了摘要来源（AI 摘要）")
    check("1. " in joined, "条目带编号（便于核对不遗漏）")

    # 14.2 中文源自带中文描述（无 LLM）：中文描述作主体
    it_cn = {"title": "库克：苹果智能接入阿里千问大模型", "desc": "苹果将在国行版接入阿里千问大模型",
             "url": "https://ex.com/b", "summary_zh": "苹果将在国行版接入阿里千问大模型",
             "summary_source": "source", "summary_is_zh": True, "source": "量子位",
             "group_final": "ai_cn"}
    joined2 = "\n".join(e.get("content", "") for e in D.render_card([it_cn])["elements"])
    check("千问大模型" in joined2, "中文描述出现在卡片中")
    check("⚠️" not in joined2, "中文源不应出现未翻译警告")

    # 14.3 只有英文（无 LLM）：明确标注未翻译，且英文描述不占主体
    it_en = {"title": "Matt Mullenweg Orders Accessibility Revert",
             "desc": "Some English description that is long enough to be real content.",
             "url": "https://ex.com/c", "summary_zh": "Some English description that is long enough.",
             "summary_source": "source", "summary_is_zh": False, "source": "Lobsters",
             "group_final": "media"}
    joined3 = "\n".join(e.get("content", "") for e in D.render_card([it_en])["elements"])
    check("未翻译" in joined3, "纯英文条目标注『未翻译』")
    check("**[Matt Mullenweg" in joined3, "英文标题仍作为链接主体")
    check(joined3.index("Matt Mullenweg") < joined3.index("Some English description"),
          "英文描述降为附注、不占主体")
    # 页脚必须告知未翻译条数，避免"以为都读了其实漏了"
    foot = M_build_footer([it_en, it_zh], 24, 1, 1)
    check("未翻译" in foot and "1 条" in foot, "页脚提示未翻译条数", foot)
    foot2 = M_build_footer([it_zh], 24, 1, 0)
    check("全部已有中文摘要" in foot2, "全部中文时页脚正面表述", foot2)
    check("时窗" not in foot2 and "h" not in foot2.replace("HN", ""), "页脚不含技术术语", foot2)

    # 14.4 占位符描述不得被当成摘要
    items_junk = [{"title": "Pizza Bot：开源的后台 AI 智能体收件箱", "desc": "点击查看原文>",
                   "url": "https://ex.com/d", "source": "InfoQ 中文", "group_final": "ai_cn"}]
    D.apply_summaries(items_junk, {})
    check(items_junk[0]["summary_zh"] == "", "『点击查看原文>』不被当作摘要",
          repr(items_junk[0]["summary_zh"]))
    # 促销内容应被识别
    check(D.is_low_value({"title": "HP Coupon Codes and Deals October 2026", "desc": ""}),
          "优惠券类内容被识别为低价值")
    check(D.is_low_value({"title": "Save up to 87% with a Surfshark coupon code", "desc": ""}),
          "促销类内容被识别为低价值")
    check(not D.is_low_value({"title": "OpenAI releases GPT-5", "desc": "model launch"}),
          "正常内容不误判为低价值")
    # 纯文本降级版同样中文优先
    txt = D.render_text([it_zh])
    check("OpenAI 发布 GPT-5" in txt and "原题：" in txt, "纯文本降级版也中文优先")

    # 14.5 板块归一：AI 的三个来源必须合并为一块，且顺序为 GitHub → AI → 其他
    mixed = [
        {"title": "g", "url": "https://e.com/1", "group_final": "github"},
        {"title": "o", "url": "https://e.com/2", "group_final": "ai_official"},
        {"title": "c", "url": "https://e.com/3", "group_final": "ai_cn"},
        {"title": "h", "url": "https://e.com/4", "group_final": "fill_hn_any"},
        {"title": "m", "url": "https://e.com/5", "group_final": "media"},
        {"title": "m2", "url": "https://e.com/6", "group_final": "fill_media"},
    ]
    groups_seen = [D.display_group(x) for x in D.order_for_display(mixed)]
    check(groups_seen == ["github", "ai", "ai", "ai", "media", "media"],
          "板块归一为 GitHub→AI→其他", str(groups_seen))
    card_mixed = D.render_card(mixed)
    # 板块标题 = 纯加粗文本且不含链接的元素（条目元素都含 [链接]）
    headers = [e.get("content", "") for e in card_mixed["elements"]
               if e.get("tag") == "markdown"
               and e.get("content", "").startswith("**")
               and "[" not in e.get("content", "")]
    check(len(headers) == 3, f"卡片只出现 3 个板块标题，实际 {len(headers)}", str(headers))
    check(headers == ["**🔥 GitHub 热门仓库**", "**🤖 AI 热点**", "**📰 其他热点**"],
          "板块标题顺序与文案正确", str(headers))
    joined_mixed = "\n".join(e.get("content", "") for e in card_mixed["elements"])
    check("ai_official" not in joined_mixed and "fill_" not in joined_mixed,
          "卡片不泄露内部组名")

    # 14.6 补跑时窗：几天没开机也不能漏内容
    from datetime import date as _d2, timedelta as _td2
    cfg_cu = {"digest": {"window_hours": 24, "window_hours_monday": 72,
                         "window_hours_max": 72, "auto_catchup": True,
                         "catchup_max_hours": 168}}
    for gap, expect in ((0, 24), (1, 24), (2, 72), (3, 96), (5, 144), (10, 168), (99, 168)):
        lg_cu = D.Ledger(":memory:")
        lg_cu.data = {"version": 1, "runs": [
            {"date": (_d2(2026, 10, 16) - _td2(days=gap)).strftime("%Y-%m-%d"), "items": []}]}
        got, _note = D.compute_window_hours_with_catchup(
            cfg_cu, lg_cu, datetime(2026, 10, 16, 9, 0))
        check(got == expect, f"空档 {gap} 天 → 时窗 {expect}h", f"got {got}")
    lg_empty = D.Ledger(":memory:")
    lg_empty.data = {"version": 1, "runs": []}
    check(D.compute_window_hours_with_catchup(cfg_cu, lg_empty,
                                              datetime(2026, 10, 16, 9, 0))[0] == 24,
          "无历史记录时不误放宽时窗")
    # 关闭补跑后应保持基础时窗
    cfg_nocu = {"digest": dict(cfg_cu["digest"], auto_catchup=False)}
    lg_cu2 = D.Ledger(":memory:")
    lg_cu2.data = {"version": 1, "runs": [{"date": "2026-10-01", "items": []}]}
    check(D.compute_window_hours_with_catchup(cfg_nocu, lg_cu2,
                                              datetime(2026, 10, 16, 9, 0))[0] == 24,
          "auto_catchup=false 时不放宽")

    print("\n== 附加：XML 容错、摘要解析、配置校验 ==")
    bad = D.parse_feed(SAMPLE_BAD_XML, "T")
    check(len(bad) == 1, "未转义 & 的 XML 容错解析成功")
    got = D._extract_summaries({"items": [{"id": 1, "summary": "A"}, {"id": 3, "summary": "B"}]})
    check(got.get(1) == "A" and got.get(3) == "B", "摘要 items 形态解析")
    got2 = D._extract_summaries([{"summary": "X"}, {"summary": "Y"}])
    check(got2.get(1) == "X" and got2.get(2) == "Y", "摘要数组形态解析")
    got3 = D._extract_summaries({"1": "甲", "2": "乙"})
    check(got3.get(1) == "甲", "摘要数字键形态解析")
    errs = D.validate_config({"feishu": {"webhook": ""},
                              "digest": {"max_items": 20, "quota_github": 5,
                                         "quota_ai": 10, "quota_media": 5}})
    check(any("webhook" in e for e in errs), "配置校验能发现缺失 webhook")
    check(D.is_chinese_text("这是一个中文标题"), "中文判定为正")
    check(not D.is_chinese_text("This is an English title"), "英文判定为负")
    check(D.clip("abcdefghij", 5) == "abcd…", "截断函数")

    print("\n" + "=" * 60)
    _lg.setLevel(_saved_level)
    if failures:
        print(f"自检完成：{checks - len(failures)}/{checks} 通过，{len(failures)} 项失败")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print(f"自检全部通过：{checks}/{checks}")
    return 0


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="每日资讯推送（飞书）")
    ap.add_argument("--config", help="配置文件路径，默认 ./config.json")
    ap.add_argument("--dry-run", action="store_true", help="不推送、不写台账，仅打印")
    ap.add_argument("--preview", action="store_true", help="输出 Markdown 预览文件")
    ap.add_argument("--force", action="store_true",
                    help="测试模式：忽略当日已推送检查与台账去重并实际推送，但不写台账")
    ap.add_argument("--selftest", action="store_true", help="离线单元测试（不联网）")
    ap.add_argument("--test-push", action="store_true",
                    help="只发一条测试卡片验证 webhook/加签，不写台账")
    ap.add_argument("--verbose", "-v", action="store_true", help="输出调试日志")
    args = ap.parse_args()

    if args.selftest:
        D.setup_logging(None)
        return selftest()
    if args.test_push:
        cfg = D.load_config(args.config or os.path.join(BASE_DIR, "config.json"))
        D.setup_logging(None, verbose=args.verbose)
        # --dry-run 时允许未填 webhook：只打印将要发送的报文，便于先看结构
        if not args.dry_run:
            errors = D.validate_config(cfg, need_feishu=True)
            if errors:
                for e in errors:
                    print(f"❌ 配置错误：{e}")
                return 1
        return 0 if D.test_push(cfg, dry_run=args.dry_run) else 1
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
