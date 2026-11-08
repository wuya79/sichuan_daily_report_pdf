#!/usr/bin/env python3
"""分析框统一守卫（发电侧/售电侧共用）

设计（2026-11-08 用户拍板）：
- 200 字是硬下限，200-350 是目标；质量靠“XX研判 + 2个数据点 + 1条动作”。
- 解析按 div 深度匹配，支持带 title 的嵌套框（修售电侧 `(.*?)</div>` 截断）。
- 空框(<10) / 短框(<200)：逐框 Kimi 补写；只动分析框，不动数据/表格/图表。
- 补写失败：空框删除并告警，短框保留并告警；绝不让空框进 PDF。
- 顺序执行，不并行（9-10 框约 1 分钟，避免 429）。

用法：
    import analysis_guard as ag
    html, journal = ag.repair_boxes(html, report_text, call_llm, side="发电侧", label_map=LABELS)
    report = ag.audit(html)
"""

import html as _html
import re

MIN_CHARS = 200
EMPTY_CHARS = 10
MAX_CHARS = 350
MAX_RETRY = 1

BOX_CLASSES = ("analysis-box", "info-box", "warning-box")
_BOX_OPEN_RE = re.compile(r'<div class="(analysis-box|info-box|warning-box)"[^>]*>')
_H2_RE = re.compile(r'<h[12][^>]*>(.*?)</h[12]>', re.S)
_TITLE_RE = re.compile(
    r'<(p|div)[^>]*class="[^"]*(?:analysis-title|info-title|warning-title)[^"]*"[^>]*>(.*?)</\1>',
    re.S,
)
# 只剥离已知 HTML 标签。不要用 <[^>]+>：分析正文里的比较句
# “净缺口<0，价格>200元”会被当成标签整段吞掉（2026-11-09 审计）。
_TAG_RE = re.compile(
    r'</?(?:p|div|br|span|strong|b|i|em|u|ul|ol|li|table|thead|tbody|tr|td|th|'
    r'h[1-6]|a|img|hr|sup|sub|code|pre|blockquote|section|article|header|footer)'
    r'\b[^<>]*>',
    re.I,
)

DEFAULT_LABELS = {
    "一": "核心研判", "二": "水情研判", "三": "供给研判", "四": "出清研判",
    "五": "火电复盘", "六": "趋势研判", "七": "交易研判", "八": "竞争空间研判",
    "九": "检修研判", "十": "市场综合研判",
}


def _text(s):
    return re.sub(r'\s+', '', _html.unescape(_TAG_RE.sub('', s or '')))


def find_boxes(html):
    """按 div 深度返回 [{'kind','start','end','inner_start','inner_end','inner'}]。"""
    boxes = []
    for m in _BOX_OPEN_RE.finditer(html):
        i = m.end()
        depth = 1
        while i < len(html) and depth > 0:
            nxt_open = html.find('<div', i)
            nxt_close = html.find('</div', i)
            if nxt_close < 0:
                break
            if nxt_open != -1 and nxt_open < nxt_close:
                depth += 1
                i = nxt_open + 4
            else:
                depth -= 1
                i = nxt_close + 6
        if depth != 0:
            continue
        inner_end = html.rfind('</div', m.end(), i)
        if inner_end < 0:
            continue
        boxes.append({
            'kind': m.group(1),
            'start': m.start(),
            'end': i,
            'inner_start': m.end(),
            'inner_end': inner_end,
            'inner': html[m.end():inner_end],
        })
    return boxes


def _section(html, pos):
    h2s = list(_H2_RE.finditer(html[:pos]))
    if not h2s:
        return '', ''
    title = _text(h2s[-1].group(1))
    return title[:1], title


def _split_title_body(inner):
    """返回 (title, body_text, title_html)。"""
    m = _TITLE_RE.search(inner)
    if m:
        return _text(m.group(2)), _text(inner[m.end():]), m.group(0)
    # 没有 title 容器时，首段短文本可能是“分析”/“XX研判”
    p = re.search(r'<p[^>]*>(.*?)</p>', inner, re.S)
    if p:
        t = _text(p.group(1))
        if len(t) <= 8 and ('分析' in t or '研判' in t):
            return t, _text(inner[p.end():]), p.group(0)
    return '', _text(inner), ''


def parse_boxes(html):
    out = []
    for b in find_boxes(html):
        no, sec = _section(html, b['start'])
        title, body, title_html = _split_title_body(b['inner'])
        out.append({
            **b,
            'section_no': no,
            'section_title': sec,
            'title': title,
            'body': body,
            'title_html': title_html,
            'body_len': len(body),
        })
    return out


def classify(html, min_chars=MIN_CHARS, empty_chars=EMPTY_CHARS):
    empty, short, ok = [], [], []
    for b in parse_boxes(html):
        if b['body_len'] < empty_chars:
            empty.append(b)
        elif b['body_len'] < min_chars:
            short.append(b)
        else:
            ok.append(b)
    return {'empty': empty, 'short': short, 'ok': ok, 'boxes': empty + short + ok}


def extract_section_data(report_text, section_title, limit=4000):
    """从日报原文取该板块数据；找不到就回退前 limit 字符。"""
    if not report_text:
        return ''
    t = section_title or ''
    keys = [t, re.sub(r'^[一二三四五六七八九十]+[、.．]\s*', '', t)]
    pos = -1
    for k in keys:
        if k:
            i = report_text.find(k)
            if i >= 0:
                pos = i
                break
    if pos < 0:
        return report_text[:limit]
    return report_text[pos:pos + limit]


def _label_for(box, label_map=None):
    label = (box.get('title') or '').strip()
    if label and label not in ('分析', '分析内容'):
        return label
    mapping = label_map or DEFAULT_LABELS
    return mapping.get(box.get('section_no'), DEFAULT_LABELS.get(box.get('section_no'), '市场研判'))


def make_repair_prompt(box, data, side='', min_chars=MIN_CHARS, max_chars=MAX_CHARS):
    label = _label_for(box)
    sec = box.get('section_title') or label
    system = (
        f"你是四川电力{side}市场分析师。只输出分析正文，不要HTML代码块、不要标题、"
        "不要“根据/数据显示/综上所述”之类套话。"
    )
    user = f"""为“{sec}”写一段{min_chars}-{max_chars}字的分析。
要求：
1. 以“{label}：”开头；
2. 至少引用2个具体数据点（带数值），只能来自下面【数据】；
3. 给出1条明确判断或操作建议；
4. 不复述表格，不编造数据；数据里没有的就写“未取得”，不要猜。
【数据】
{data[:4000]}
【原分析（可能为空，仅供参考，不得沿用错误）】
{(box.get('body') or '')[:800]}
"""
    return system, user


def clean_analysis(text, label=''):
    if not text:
        return ''
    t = text.strip()
    if '```' in t:
        parts = t.split('```')
        t = parts[1].strip() if len(parts) > 1 else t
    t = _TAG_RE.sub('', t)
    t = _html.unescape(t).strip()
    t = re.sub(r'^\s*(分析|研判)[：:]\s*', '', t)
    if label and not t.startswith(label):
        t = f"{label}：{t}"
    return t


def repair_boxes(html, report_text, call_llm, side='', label_map=None,
                 min_chars=MIN_CHARS, max_chars=MAX_CHARS, max_retry=MAX_RETRY):
    """逐框补写空/短分析框。call_llm(system, user) -> str。

    Returns: (new_html, journal)
    journal: [{'section','before','after','status'}]，status ∈ repaired/removed_empty/kept_short
    """
    journal = []
    boxes = [b for b in parse_boxes(html) if b['body_len'] < min_chars]
    for b in reversed(boxes):
        label = _label_for(b, label_map)
        data = extract_section_data(report_text, b.get('section_title', ''))
        before = b['body_len']
        new = ''
        for _ in range(max_retry + 1):
            system, user = make_repair_prompt({**b, 'title': label}, data, side, min_chars, max_chars)
            try:
                raw = call_llm(system, user)
            except Exception:
                raw = ''
            new = clean_analysis(raw, label)
            if len(_text(new)) >= min_chars:
                break
        if len(_text(new)) >= min_chars:
            title = b.get('title') or ''
            if title in ('分析', '分析内容', ''):
                title_html = f'<div class="analysis-title">{label}</div>'
            else:
                title_html = b.get('title_html') or f'<div class="analysis-title">{label}</div>'
            new_inner = f"{title_html}\n<p>{_html.escape(new)}</p>"
            html = html[:b['inner_start']] + new_inner + html[b['inner_end']:]
            journal.append({'section': b.get('section_title') or label,
                            'before': before, 'after': len(_text(new)), 'status': 'repaired'})
        elif before < EMPTY_CHARS:
            html = html[:b['start']] + html[b['end']:]
            journal.append({'section': b.get('section_title') or label,
                            'before': before, 'after': 0, 'status': 'removed_empty'})
        else:
            journal.append({'section': b.get('section_title') or label,
                            'before': before, 'after': before, 'status': 'kept_short'})
    return html, journal


def audit(html, min_chars=MIN_CHARS, empty_chars=EMPTY_CHARS):
    c = classify(html, min_chars, empty_chars)
    return {
        'empty': [{'section': b['section_title'], 'len': b['body_len']} for b in c['empty']],
        'short': [{'section': b['section_title'], 'len': b['body_len']} for b in c['short']],
        'ok': len(c['ok']),
        'total': len(c['boxes']),
    }


def strip_empty(html, empty_chars=EMPTY_CHARS):
    for b in reversed(parse_boxes(html)):
        if b['body_len'] < empty_chars:
            html = html[:b['start']] + html[b['end']:]
    return html


def format_audit(a):
    parts = []
    if a['empty']:
        parts.append('空框' + str(a['empty']))
    if a['short']:
        parts.append('短框' + str(a['short']))
    return '；'.join(parts) if parts else f"{a['ok']}/{a['total']} 框≥{MIN_CHARS}字"
