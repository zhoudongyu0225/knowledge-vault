#!/usr/bin/env python3
"""
公众号周度增量检查器

从「持续关注公众号池.md」读取 active 账号，逐个走搜狗微信全量检索，
按账号名精确匹配、按窗口日期过滤，输出可核验的增量清单。

设计要点（均来自 2026-08-03 实测）：
- 不使用 tsn 时间过滤参数：实测 10 个账号全部返回 0 条，该参数在搜狗微信侧已失效。
- 不使用 sortType=1 排序参数：实测无效，仍按相关度返回。
- 正确做法是全量检索后在本地按时间戳过滤。
- 搜狗是关键词检索而非账号检索，必须用 <span class="all-time-y2"> 的账号名精确过滤，
  否则会混入同名关键词的其他账号（例如「老侯说投放」会混入「老侯说消防」）。
- 搜狗索引深度因账号而异，「未检出」只代表当前公开路径拿不到，不等于账号没发文。

用法:
    python wechat_watch.py                     # 按池中各账号的「最近检查」日期做增量
    python wechat_watch.py --since 2026-07-01  # 统一指定起始日期
    python wechat_watch.py --days 14           # 统一回看 N 天
    python wechat_watch.py --json out.json     # 额外输出 JSON
"""

import argparse
import datetime as dt
import html
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

POOL = Path(__file__).resolve().parents[2] / "40_资料库/游戏发行与广告/公众号专题/持续关注公众号池.md"

# 分账号备用数据源（2026-08-03 实测后接入）。
# 背景：搜狗对账号的覆盖是幸存者偏差——王董近一月约 7 篇搜狗只命中 1 篇，
# 曾嵘 7 月 4 篇搜狗 0 篇。对这类账号在搜狗之外叠加备用入口，按标题合并去重。
# 新增入口时在此登记，并在「持续关注公众号池.md」的检索入口一节同步记录。
EXTRA_SOURCES = {
    "王董的新游戏": {
        "type": "jintiankansha",
        "url": "https://www.jintiankansha.com/column/yiKLO5DWpr",
        # 今日看啥只给相对日期（N 周前/个月前），日期为换算约值，归档时需标注口径。
    },
    "曾嵘胡扯的地方": {
        "type": "rss",
        "url": "https://blog.zengrong.net/index.xml",
        # 作者博客与公众号同源，RSS 有准确发布日期，实测 7 月 4 篇全部命中。
    },
}

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

LI_SPLIT = re.compile(r"<li[\s>]")
RE_TITLE = re.compile(r'uigs="article_title_\d+"[^>]*>(.*?)</a>', re.S)
RE_HREF = re.compile(r'<a[^>]*href="([^"]+)"[^>]*uigs="article_title_\d+"', re.S)
RE_HREF_ALT = re.compile(r'uigs="article_title_\d+"[^>]*href="([^"]+)"', re.S)
RE_ACCOUNT = re.compile(r'<span class="all-time-y2">(.*?)</span>', re.S)
RE_TS = re.compile(r"timeConvert\('(\d+)'\)")
RE_SUMMARY = re.compile(r'<p class="txt-info"[^>]*>(.*?)</p>', re.S)


def clean(raw: str) -> str:
    """去标签、解实体、压空白。"""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", raw))).strip()


def normalize_title(title: str) -> str:
    """标题标准化，用于跨文件去重。

    搜狗返回的时间戳不等于公众号实际发布日期，实测同一篇文章会在不同时间
    以不同日期重复出现（例如「类《羊了个羊》玩法增加订单制的融合」既出现在
    2026-07-09 也出现在 2026-08-03）。因此必须按标题与已归档条目去重，
    不能只信日期。
    """
    t = html.unescape(title)
    t = re.sub(r"[\s，,。.！!？?、：:；;「」『』《》〈〉（）()\[\]【】\"'`~—\-…]+", "", t)
    return t.lower()


def load_archived_titles(topic_dir: Path) -> set:
    """收集专题目录下已归档的文章标题，避免重复登记。

    排除以日期开头的执行报告类文件：报告会提及当轮检出的标题，
    若计入已归档集合，会出现"被报告提及=已归档"的假阳性，
    导致真实未入档的文章被错误拦截（2026-08-03 王董案例实测）。"""
    titles = set()
    if not topic_dir.exists():
        return titles
    for md in topic_dir.glob("*.md"):
        if re.match(r"^\d{4}-\d{2}-\d{2}-", md.name):
            continue
        text = md.read_text(encoding="utf-8", errors="ignore")
        # 表格单元格内的链接标题与纯文本标题
        for m in re.finditer(r"\[([^\]]{6,})\]\(", text):
            titles.add(normalize_title(m.group(1)))
        for line in text.splitlines():
            if not line.startswith("|"):
                continue
            for cell in line.strip("|").split("|"):
                cell = cell.strip()
                if len(cell) >= 8 and not cell.startswith("20") and "---" not in cell:
                    titles.add(normalize_title(cell))
        for m in re.finditer(r"「([^」]{6,})」", text):
            titles.add(normalize_title(m.group(1)))
    titles.discard("")
    return titles


def parse_pool(path: Path):
    """解析关注池表格，返回 active 账号列表。"""
    if not path.exists():
        print(f"[错误] 找不到关注池文件: {path}", file=sys.stderr)
        return []
    accounts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or "---" in line:
            continue
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) < 7 or cols[0] in ("账号",):
            continue
        if cols[6].lower() != "active":
            continue
        aliases = [a.strip() for a in re.split(r"[/、]", cols[1]) if a.strip() and a.strip() != "-"]
        accounts.append({
            "name": cols[0],
            "aliases": aliases,
            "focus": cols[2],
            "domain": cols[3],
            "level": cols[4],
            "last_check": cols[5],
        })
    return accounts


def fetch(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": random.choice(UA_POOL),
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://weixin.sogou.com/",
    })
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")


def search(keyword: str, page: int = 1) -> list:
    """检索并解析为文章列表。按 <li> 分块以保证字段不错位。"""
    q = urllib.parse.quote(keyword)
    url = f"https://weixin.sogou.com/weixin?type=2&query={q}&ie=utf8&page={page}"
    try:
        content = fetch(url)
    except Exception as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc

    start = content.find("news-list")
    if start == -1:
        return []

    items = []
    for block in LI_SPLIT.split(content[start:]):
        t = RE_TITLE.search(block)
        acc = RE_ACCOUNT.search(block)
        ts = RE_TS.search(block)
        if not (t and acc and ts):
            continue
        href_match = RE_HREF.search(block) or RE_HREF_ALT.search(block)
        href = href_match.group(1) if href_match else ""
        if href.startswith("/"):
            href = "https://weixin.sogou.com" + href
        summary = RE_SUMMARY.search(block)
        items.append({
            "title": clean(t.group(1)),
            "account": clean(acc.group(1)),
            "date": dt.datetime.fromtimestamp(int(ts.group(1))).strftime("%Y-%m-%d"),
            "url": html.unescape(href),
            "summary": clean(summary.group(1))[:160] if summary else "",
        })
    return items


def fetch_rss(url: str) -> list:
    """解析 RSS item，返回 [{title, date, url, source}]。日期为准确值。

    用正则而非 ElementTree：实测部分博客 RSS 内容含未转义字符，
    严格 XML 解析会整体失败。只提取 title/link/pubDate 三个字段。
    """
    raw = fetch(url)
    items = []
    for block in re.findall(r"<item>(.*?)</item>", raw, re.S):
        def field(tag: str) -> str:
            m = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
            if not m:
                return ""
            return clean(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1)))
        title, link, pub = field("title"), field("link"), field("pubDate")
        date = ""
        if pub:
            try:
                date = parsedate_to_datetime(pub).date().isoformat()
            except (TypeError, ValueError):
                pass
        if title:
            items.append({"title": title, "date": date, "url": link,
                          "summary": "", "source": "rss", "date_approx": False})
    return items


def fetch_jintiankansha(url: str) -> list:
    """解析今日看啥专栏页文章列表。

    结构：每个条目以 <span class="item_title"> 开头，块内 hide-content 依次是
    标题、账号名，后跟相对日期「N 小时/天/周/个月前」。只有相对日期，
    换算成绝对日期是约值（date_approx=True），归档时必须标注口径。
    """
    content = fetch(url)
    today = dt.date.today()
    items = []
    for chunk in content.split('<span class="item_title">')[1:]:
        spans = re.findall(r'<span class="hide-content">(.*?)</span>', chunk, re.S)
        # 条目标题有两种形态：无链接的在 hide-content 里，有链接的直接是 <a>标题</a>。
        m_link = re.search(r'<a target="_blank" href="(http[^"]*/t/[^"]+)">(.*?)</a>', chunk, re.S)
        if m_link:
            title, link = clean(m_link.group(2)), m_link.group(1)
            account = clean(spans[0]) if spans else ""
        else:
            if len(spans) < 2:
                continue
            title, account = clean(spans[0]), clean(spans[1])
            lm = re.search(r'href="(/t/[^"]+)"', chunk)
            link = "https://www.jintiankansha.com" + lm.group(1) if lm else ""
        # 相对日期实测形态：「N 小时前 / N 天前 / N 周前 / N 月前」（注意是"月前"不是"个月前"）。
        ago = re.search(r"(\d+)\s*(周|个?月|天|小时)前", chunk)
        if not ago or not title:
            continue
        n, unit = int(ago.group(1)), ago.group(2)
        days = {"小时": 0, "天": n, "周": 7 * n, "月": 30 * n, "个月": 30 * n}[unit]
        date = (today - dt.timedelta(days=days)).isoformat()
        items.append({"title": title, "account": account, "date": date, "url": link,
                      "summary": "", "source": "jintiankansha", "date_approx": True})
    return items


EXTRA_FETCHERS = {"rss": fetch_rss, "jintiankansha": fetch_jintiankansha}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="统一起始日期 YYYY-MM-DD，默认用池中各账号的最近检查日期")
    ap.add_argument("--days", type=int, help="统一回看天数")
    ap.add_argument("--json", help="额外输出 JSON 文件路径")
    ap.add_argument("--pages", type=int, default=2, help="每个账号检索页数，默认 2")
    ap.add_argument("--rounds", type=int, default=2,
                    help="每个账号重复检索轮数取并集，默认 2。搜狗同一查询在不同时刻返回结果存在波动，"
                         "单轮会漏，多轮并集可显著降低漏检。")
    args = ap.parse_args()

    accounts = parse_pool(POOL)
    if not accounts:
        sys.exit(1)

    archived = load_archived_titles(POOL.parent)
    today = dt.date.today()
    if args.days:
        default_since = (today - dt.timedelta(days=args.days)).isoformat()
    else:
        default_since = args.since

    print(f"# 公众号增量检查 {today.isoformat()}\n")
    print(f"检查账号 {len(accounts)} 个，数据源 搜狗微信全量检索 + 分账号备用入口（今日看啥/RSS）\n")

    report, hits, misses = [], 0, 0

    for acc in accounts:
        since = default_since or acc["last_check"]
        names = {acc["name"]} | set(acc["aliases"])
        collected, reprints, errors = {}, {}, []

        for _round in range(args.rounds):
            for page in range(1, args.pages + 1):
                try:
                    for art in search(acc["name"], page):
                        if art["date"] <= since:
                            continue
                        art.setdefault("source", "sogou")
                        art.setdefault("date_approx", False)
                        # 账号名精确过滤：搜狗是关键词检索，会混入无关账号。
                        # 命中本账号 -> 计入增量；未命中但在窗口内 -> 记为他人转载线索。
                        if art["account"] in names:
                            art["already_archived"] = normalize_title(art["title"]) in archived
                            collected[normalize_title(art["title"])] = art
                        else:
                            reprints[art["title"]] = art
                except RuntimeError as exc:
                    errors.append(str(exc))
                    break
                time.sleep(1.5 + random.random() * 1.5)

        # 备用数据源：搜狗覆盖差的账号，叠加今日看啥/RSS 等入口，按标准化标题合并去重。
        extra = EXTRA_SOURCES.get(acc["name"])
        if extra:
            try:
                for art in EXTRA_FETCHERS[extra["type"]](extra["url"]):
                    if not art["date"] or art["date"] <= since:
                        continue
                    # 专栏页可能混入非本账号条目，账号名不符则跳过（RSS 无账号字段则不校验）。
                    if art.get("account") and art["account"] not in names:
                        continue
                    art["already_archived"] = normalize_title(art["title"]) in archived
                    collected.setdefault(normalize_title(art["title"]), art)
            except Exception as exc:
                errors.append(f"{extra['type']} 抓取失败: {exc}")

        all_found = sorted(collected.values(), key=lambda x: x["date"], reverse=True)
        found = [a for a in all_found if not a.get("already_archived")]
        dupes = [a for a in all_found if a.get("already_archived")]
        leads = sorted(reprints.values(), key=lambda x: x["date"], reverse=True)[:3]
        status = "检出" if found else ("请求失败" if errors else "未检出")
        if found:
            hits += len(found)
        else:
            misses += 1

        report.append({
            "account": acc["name"],
            "domain": acc["domain"],
            "level": acc["level"],
            "since": since,
            "status": status,
            "articles": found,
            "already_archived": dupes,
            "reprint_leads": leads,
            "errors": errors,
        })

        print(f"## {acc['name']}  [{acc['domain']} / {acc['level']}]")
        print(f"窗口 {since} 之后 · {status}")
        if found:
            for art in found:
                src = {"sogou": "搜狗", "rss": "RSS", "jintiankansha": "今日看啥"}.get(
                    art.get("source", "sogou"), art.get("source", "搜狗"))
                approx = "（日期为相对口径换算约值）" if art.get("date_approx") else ""
                print(f"- {art['date']} 「{art['title']}」 [{src}]{approx}")
                if art["summary"]:
                    print(f"  摘要: {art['summary']}")
                if art["url"]:
                    print(f"  链接: {art['url']}")
        if dupes:
            print(f"- 已归档过（标题命中现有条目，日期以搜狗为准不可靠，不计入增量）:")
            for art in dupes:
                print(f"  · {art['date']} {art['title']}")
        if leads:
            print(f"- 他人转载线索（非本账号发布，需人工核验归属）:")
            for art in leads:
                print(f"  · {art['date']} [{art['account']}] {art['title']}")
        if errors:
            print(f"- 错误: {errors[0]}")
        print()
        time.sleep(1)

    print("---\n")
    print(f"合计检出 {hits} 篇，{misses} 个账号未检出。")
    print("「未检出」仅代表搜狗当前公开索引拿不到，不等于该账号未发布。")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"date": today.isoformat(), "results": report}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\nJSON 已写入 {args.json}")


if __name__ == "__main__":
    main()
