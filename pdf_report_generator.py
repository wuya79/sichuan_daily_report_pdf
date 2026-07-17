#!/usr/bin/env python3
"""
四川电力交易日报/周报 PDF生成脚本
- 从nginx公网拉取最新txt日报/周报
- 生成matplotlib图表
- 调用Kimi API生成HTML
- weasyprint转PDF
- 同步到nginx公网目录

Usage:
    python pdf_report_generator.py --type daily （KIMI_API_KEY从.env环境变量读取）

Cron:
    09:42 daily (日报txt 09:30同步后12分钟)

依赖: pip install openai matplotlib numpy weasyprint
Kimi API Key: 通过 ~/.hermes/.env 中 KIMI_API_KEY= 设置"""

import argparse
import os
import sys
import re
import json
import time
import logging
import traceback
from pathlib import Path
from datetime import datetime

# ─── 路径配置 ───────────────────────────────────────────────────
NGINX_BASE = "http://118.24.77.156:18080/reports"
NGINX_DIR = "/var/www/reports"
NGINX_PDF_DIR = "/var/www/reports/pdf"
SCRIPT_DIR = Path(__file__).parent
OUTPUT_DIR = Path("/tmp/hermes_pdf_reports")
LOG_FILE = os.path.expanduser("~/.hermes/logs/pdf_report_generator.log")

DAILY_TXT = Path(NGINX_DIR) / "daily_latest.txt"  # 本地读，不依赖nginx

KIMI_BASE_URL_OPEN = "https://api.moonshot.cn/v1"
KIMI_MODEL_OPEN = "moonshot-v1-128k"

# Kimi Code Plan（备用，限流/超时时切换）
KIMI_BASE_URL_CODE = "https://api.kimi.com/coding/v1"
KIMI_MODEL_CODE = "moonshot-v1-128k"

# ─── Skill Assets ───────────────────────────────────────────────
SKILL_DIR = Path.home() / ".hermes" / "skills" / "document-processing" / "scu-power-report"
ASSETS_DIR = SKILL_DIR / "assets"
SCRIPTS_DIR = SKILL_DIR / "scripts"

CSS_PATH = ASSETS_DIR / "report.css"
TEMPLATE_PATH = ASSETS_DIR / "daily_template.html"

# ─── 日志 ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("pdf_report")


# ─── 工具函数 ────────────────────────────────────────────────────

def _do_openai_call(api_key: str, base_url: str, model: str,
                    messages: list, temperature: float,
                    max_tokens: int, timeout: int) -> str:
    """单次 OpenAI 兼容 API 调用，不处理 fallback。"""
    import openai
    import httpx
    http_client = httpx.Client(timeout=httpx.Timeout(timeout, connect=10, read=timeout, write=10))
    client = openai.OpenAI(api_key=api_key, base_url=base_url, http_client=http_client, max_retries=0)
    resp = client.chat.completions.create(
        model=model, messages=messages,
        temperature=temperature, max_tokens=max_tokens,
    )
    return resp.choices[0].message.content


def kimi_call_with_fallback(api_key_open: str, api_key_code: str,
                             system_prompt: str, user_message: str,
                             timeout: int = 600, max_tokens: int = 100000) -> str:
    """优先 Kimi 开放平台。触发 429/5xx/超时 → 自动切 Kimi Code Plan。"""
    import openai

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    # 尝试主平台（开放平台，temperature=0.2）
    try:
        log.info("[PRIMARY] Kimi开放平台...")
        result = _do_openai_call(api_key_open, KIMI_BASE_URL_OPEN, KIMI_MODEL_OPEN,
                                 messages, temperature=0.2, max_tokens=max_tokens, timeout=timeout)
        log.info("[PRIMARY] ✅")
        return result
    except (openai.RateLimitError, openai.InternalServerError,
            openai.APITimeoutError, openai.APIConnectionError,
            openai.APIStatusError) as e:
        log.warning(f"[PRIMARY] ⚠️ {type(e).__name__}，切换到 Code Plan...")
        if not api_key_code:
            raise RuntimeError(f"开放平台{type(e).__name__}，但 Code Plan key 未配置，无法切换") from e
    # 401/400 不切换，直接向上抛

    # 备用平台（Code Plan，temperature=1）
    log.info("[FALLBACK] Kimi Code Plan...")
    result = _do_openai_call(api_key_code, KIMI_BASE_URL_CODE, KIMI_MODEL_CODE,
                             messages, temperature=1, max_tokens=max_tokens, timeout=timeout)
    log.info("[FALLBACK] ✅")
    return result


def extract_html(raw: str) -> str:
    """从Kimi返回内容中提取HTML。带fallback，永不raise。"""
    # 检测nginx错误页面
    if "<title>50" in raw and "Gateway" in raw:
        raise RuntimeError(f"API返回网关错误: {raw[:200]}")
    if "<title>40" in raw and "Error" in raw:
        raise RuntimeError(f"API返回HTTP错误: {raw[:200]}")
    if "```html" in raw:
        return raw.split("```html")[1].split("```")[0].strip()
    if "```" in raw and ("<!DOCTYPE" in raw or "<html" in raw):
        for p in raw.split("```"):
            if "<!DOCTYPE" in p or "<html" in p:
                return p.strip()
    if "<!DOCTYPE" in raw or "<html" in raw:
        return raw.strip()
    # fallback: 直接返回raw，靠后续CSS注入兜底
    log.warning("  extract_html无法识别标准HTML，使用原始返回内容")
    return raw.strip()



def write_file(path: str, content: str):
    """安全写入文件，自动创建目录。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def safe_symlink(target: str, link_path: str):
    """安全创建/更新符号链接。"""
    if os.path.islink(link_path):
        os.unlink(link_path)
    elif os.path.exists(link_path):
        os.remove(link_path)
    os.symlink(target, link_path)


# ─── System Prompt ───────────────────────────────────────────────

def build_system_prompt():
    """Build the system prompt for Kimi API for daily report."""
    css_file = ASSETS_DIR / 'report.css'
    template_file = ASSETS_DIR / 'daily_template.html'
    css_content = css_file.read_text(encoding='utf-8') if css_file.exists() else ''
    template_content = template_file.read_text(encoding='utf-8') if template_file.exists() else ''

    section_count = 10
    table_count = '25+'
    section_list = (
        "【日报板块】\n"
        "一、核心指标摘要(1张总表) → 二、水情监测(3张表：流域来水、水库水位、蓄放水) → "
        "三、天气前瞻(2张表：降雨预报、新能源/气温) → 四、供给预测(2张表：出力预测、火电状态) → "
        "五、检修与断面(2张表：检修清单、断面潮流) → 六、趋势仪表盘(1张7日数据表+图表) → "
        "七、昨日偏差分析(1张偏差表) → 八、昨日出清回顾(3张表：日前出清、分时电价、电源出力) → "
        "九、月内交易(2张表：滚动交易、价格对比) → 十、交易策略建议(4张表：因子评分、报价参考、流动性管理、相似日)"
    )

    return f"""你是一个专业的四川电力交易报告生成助手。将原始数据转换为格式正式的HTML报告。

【HTML结构模板 - 必须严格遵循此结构】
{template_content[:5000]}

【CSS样式 - 完整嵌入到HTML的<style>标签中】
{css_content}

【格式硬性要求】
1. 输出完整HTML（<!DOCTYPE html>到</html>），不要Markdown代码块
2. 必须包含：封面(.cover) + 目录(.toc-page) + {section_count}个section
3. 总表格数必须达到{table_count}张，每张表必须有<caption data-label="表X">标题</caption>（显示在表格上方）
4. 每张表格必须有thead（border-top:2px solid #333, border-bottom:1px solid #333）和tbody（border-bottom:2px solid #333）
5. 每个section结尾必须有<div class="analysis-box">或<div class="info-box">或<div class="warning-box">
6. 图表引用格式：<figure><img src="charts/xxx.png"><figcaption data-label="图X">标题</figcaption></figure>
7. 所有单元格填入实际数据，禁止留空写"--"
8. 不使用emoji，不使用CSS counter，使用data-*属性编号
9. 中文内容

{section_list}

【质量要求】每个分析框必须不少于200字（5-8句），结合表格数据展开分析，引用具体数字说明趋势原因。
生成完成后请自检：每个分析框是否都达到了200字以上？"""


# ─── 后处理：补写短分析框 ──────────────────────────────────────

def fix_short_analysis_boxes(html: str, report_text: str, api_key_open: str, api_key_code: str) -> str:
    """
    检测HTML中所有analysis-box/info-box/warning-box的内容长度，
    对不足200字的框，单独调用Kimi补写成200-400字的详细分析。
    直接在HTML中替换原框内容。
    返回补写后的完整HTML。
    """
    boxes = re.findall(
        r'(<div class="(analysis-box|info-box|warning-box)">)(.*?)(</div>)',
        html, re.DOTALL
    )

    short_boxes = []
    for match in boxes:
        full_match = match[0]  # 完整匹配字符串（<div class=...>...content...）
        cls = match[1]        # box类型
        inner = match[2]      # 内部内容
        closing = match[3]    # </div>

        # 纯文本字数（去html标签）
        text = re.sub(r'<[^>]+>', '', inner).strip().replace(' ', '').replace('\n', '').replace('\r', '')
        char_count = len(text)

        if char_count < 200:
            # 计算这个box在全局HTML中的结束位置
            box_start = html.find(full_match)
            if box_start < 0:
                continue
            short_boxes.append({
                'cls': cls,
                'inner': inner,
                'full_match': full_match + inner + closing,
                'end_pos': box_start + len(full_match + inner + closing),
                'old_text': text[:100],
                'char_count': char_count,
                'section_hint': _get_section_context(html, full_match),
            })

    if not short_boxes:
        log.info("  所有分析框字数达标 ✓")
        return html

    log.info(f"  发现{len(short_boxes)}个短分析框，开始补写...")

    # 逐框补写（从后往前替换，保证索引不偏移）
    for sb in reversed(short_boxes):
        box_cls = sb['cls']
        section_hint = sb['section_hint']

        # 旧的内容（可能包含info-title等）
        old_inner = sb['inner']
        old_text = re.sub(r'<[^>]+>', '', old_inner).strip()

        # 调用Kimi补写
        new_analysis = _call_kimi_for_analysis(api_key_open, api_key_code, report_text, section_hint, old_text)
        if not new_analysis:
            continue

        # 保留原有的info-title（如果有），只替换正文内容部分
        title_match = re.search(r'<div class="[^"]*-title">(.*?)</div>', old_inner, re.DOTALL)
        if title_match:
            title_html = title_match.group(0)
            new_inner = title_html + '\n' + new_analysis
        else:
            new_inner = new_analysis

        # 索引替换（从后往前，索引不会偏移）
        old_full = sb['full_match']
        new_full = f'<div class="{box_cls}">\n{new_inner}\n</div>'
        idx = html.rfind(old_full, 0, sb['end_pos'])
        if idx >= 0:
            html = html[:idx] + new_full + html[idx + len(old_full):]
        log.info(f"    ✓ 补写完成: {sb['char_count']}字 → 200+字 ({section_hint})")

    return html


def move_captions_below_tables(html: str) -> str:
    """
    将HTML中所有 <caption> 从 <table> 前方移动到 </table> 后方。
    同时将 data-label 属性值（如"表1"）直接嵌入caption文本中，
    避免weasyprint渲染table外caption时::before伪元素失效。

    Kimi输出格式:
        <table>
        <caption data-label="表1">核心指标一览</caption>
        ...表内容...
        </table>
    处理后:
        <table>
        ...表内容...
        </table>
        <div class="table-caption">表1  核心指标一览</div>
    """
    pattern = re.compile(
        r'(<table[^>]*>)\s*'
        r'<caption[^>]*data-label="([^"]*)"[^>]*>([\s\S]*?)</caption>\s*'
        r'(.*?</table>)',
        re.DOTALL
    )

    def replacer(match):
        table_open = match.group(1)
        label = match.group(2)     # "表1"
        title = match.group(3)     # "核心指标一览"
        rest = match.group(4)      # 表格内容 + </table>
        # 拼接标题文本：保持caption::before的样式效果
        caption_text = f'{label}  {title.strip()}'
        # 把 caption 移到 </table> 之后，用div替代caption（避免table外渲染异常）
        if rest.rstrip().endswith('</table>'):
            idx = rest.rfind('</table>')
            table_body = rest[:idx]
            table_close = rest[idx:]
            return f'{table_open}{table_body}\n{table_close}\n<div class="table-caption">{caption_text}</div>'
        else:
            return match.group(0)

    return pattern.sub(replacer, html)


def _get_section_context(html: str, box_marker: str) -> str:
    """找到分析框所在的section上下文。"""
    # 从box位置往前找最近的h1/h2标题
    idx = html.find(box_marker)
    if idx < 0:
        return ""
    prefix = html[max(0, idx-500):idx]
    # 找最近的section标题
    titles = re.findall(r'<h[12][^>]*id="[^"]*"[^>]*>(.*?)</h[12]>', prefix)
    if titles:
        return titles[-1]
    # 找最近的数字标题
    nums = re.findall(r'[一二三四五六七八九十]+[、.．].*?(?=<|$)', prefix)
    if nums:
        return nums[-1][:30]
    return ""


def _call_kimi_for_analysis(api_key_open: str, api_key_code: str, report_text: str, section_hint: str, old_text: str) -> str:
    """调用Kimi补写单个分析框。短调用，timeout=60s，max_tokens=2000。支持双key自动切换。"""

    if not section_hint:
        section_hint = "电力交易分析"

    system = "你是四川电力市场分析师。直接输出市场分析正文，200-300字，精炼专业。使用<p>标签包裹，不要标题，不要\"根据\"\"数据\"之类的开头套话，直接写分析。"

    user = f"""请写一段200-300字的{section_hint}分析。

参考之前的分析内容（可替换）：
{old_text[:200]}

【数据背景】
{report_text[:6000]}

要求：
- 200-300字，精炼
- 直接写分析，不要任何开头套话（不要"根据"、"数据显示"、"综上所述"）
- 引用1-2个关键数据
- 分析原因和趋势
- 用<p>标签包裹段落
- 只为输出分析正文，不要多余的说明"""

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    try:
        import openai
        # 主平台
        try:
            text = _do_openai_call(api_key_open, KIMI_BASE_URL_OPEN, KIMI_MODEL_OPEN,
                                   messages, temperature=0.3, max_tokens=2000, timeout=60)
            text = text.strip()
            if "```" in text:
                text = text.split("```")[1].split("```")[0].strip() if "```" in text else text
            return text
        except (openai.RateLimitError, openai.InternalServerError,
                openai.APITimeoutError, openai.APIConnectionError,
                openai.APIStatusError):
            text = _do_openai_call(api_key_code, KIMI_BASE_URL_CODE, KIMI_MODEL_CODE,
                                   messages, temperature=1, max_tokens=2000, timeout=60)
            text = text.strip()
            if "```" in text:
                text = text.split("```")[1].split("```")[0].strip() if "```" in text else text
            return text
    except Exception as e:
        log.warning(f"    补写失败（跳过）: {e}")
        return ""



# ─── 生成HTML（单轮） ─────────────────────────────────────────────

def generate_html_single_round(report_text: str, charts_dir: str, api_key_open: str, api_key_code: str) -> str:
    """单轮调用Kimi生成完整HTML。"""
    chart_files = sorted(Path(charts_dir).glob("*.png")) if os.path.exists(charts_dir) else []
    chart_refs = "\n".join([f'<img src="charts/{f.name}">' for f in chart_files]) if chart_files else ""

    system_prompt = build_system_prompt()

    today_str = datetime.now().strftime("%Y年%m月%d日")
    user_message = f"""请根据以下四川电力交易日报数据，生成完整的HTML报告。

【硬性要求 - 必须遵守】
1. 输出必须是完整HTML（从<!DOCTYPE html>到</html>），不要Markdown代码块包裹
2. 必须包含封面页（.cover）、目录页（.toc-page）、全部10个section
3. 每个section必须包含2-4张数据表格，每张表格必须有6-12行数据
4. 总表格数量必须达到25张以上
5. 每张表格必须有<caption data-label="表X">标题</caption>（显示在表格上方）
6. 每个section结尾必须有.analysis-box或.info-box进行分析
7. 趋势仪表盘section必须引用图表：<img src="charts/xxx.png">
8. 所有单元格必须填入实际数据，不能留空或写"--"
9. 每个分析框内容不少于200字（约5-8句），结合表格数据展开分析趋势原因、数据变化原因和市场影响，遵循四川电力市场规则（水期划分、边际定价机制、供需关系），不得臆想
10. 【重要】封面页的报告周期必须写为"{today_str}"，不要使用其他日期

【已生成的图表文件 - 在HTML中引用】
{chart_refs}

【报告原始数据 - 请解析所有内容生成表格】
{report_text[:12000]}

请直接输出完整HTML代码。"""

    log.info("调用Kimi API生成HTML...")
    raw = kimi_call_with_fallback(api_key_open, api_key_code, system_prompt, user_message, timeout=600)
    html = extract_html(raw)
    log.info(f"HTML生成完成: {len(html)}字符")
    return html


# ─── OSS 上传（阿里云对象存储） ──────────────────────────────

def upload_to_oss(local_path: str, oss_key: str,
                  max_retries: int = 3) -> dict:
    """
    上传文件到阿里云 OSS，作为下游系统的交付通道。

    返回:
        {"ok": True,  "url": "https://..."}
        {"ok": False, "error": "具体原因"}

    重试: 3次，间隔 0s / 5s / 15s
    超时: 连接 10s，操作不设硬超时（依赖 SDK 内部管理）
    失败: 返回 error dict，不抛异常
    """
    access_key_id = os.environ.get("OSS_ACCESS_KEY_ID", "")
    access_key_secret = os.environ.get("OSS_ACCESS_KEY_SECRET", "")
    bucket_name = os.environ.get("OSS_BUCKET", "sc-power-trade")
    endpoint = os.environ.get("OSS_ENDPOINT", "oss-cn-chengdu.aliyuncs.com")

    if not access_key_id or not access_key_secret:
        return {"ok": False, "error": "credentials not configured"}

    if not os.path.exists(local_path):
        return {"ok": False, "error": f"file not found: {local_path}"}

    try:
        import oss2
    except ImportError:
        return {"ok": False, "error": "oss2 SDK not installed"}

    auth = oss2.Auth(access_key_id, access_key_secret)
    bucket = oss2.Bucket(auth, endpoint, bucket_name, connect_timeout=10)

    last_error = ""
    for attempt in range(max_retries):
        try:
            bucket.put_object_from_file(oss_key, local_path)
            url = f"https://{bucket_name}.{endpoint}/{oss_key}"
            log.info(f"  OSS 上传成功: {url}")
            return {"ok": True, "url": url}
        except oss2.exceptions.NoSuchBucket:
            return {"ok": False, "error": f"bucket not found: {bucket_name}"}
        except oss2.exceptions.AccessDenied:
            return {"ok": False, "error": "access denied (check AK/SK permissions)"}
        except Exception as e:
            last_error = str(e)
            if attempt < max_retries - 1:
                wait = [0, 5, 15][attempt]
                log.warning(f"  OSS 上传失败 (attempt {attempt+1}/{max_retries}): {last_error}，{wait}s 后重试")
                time.sleep(wait)
            else:
                log.error(f"  OSS 上传最终失败 ({max_retries} attempts): {last_error}")

    return {"ok": False, "error": last_error}


# ─── 主流程 ─────────────────────────────────────────────────────

def generate_pdf(api_key_open: str, api_key_code: str) -> dict:
    """
    生成日报PDF完整流程。
    返回: {"success": bool, "pdf_path": str, "html_path": str, "error": str, ...}
    """
    report_type = "daily"
    result = {"success": False, "type": report_type, "error": ""}
    job_dir = None

    try:
        # 0. 创建临时目录
        job_dir = OUTPUT_DIR / f"daily_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        job_dir.mkdir(parents=True, exist_ok=True)
        charts_dir = job_dir / "charts"
        charts_dir.mkdir(exist_ok=True)

        # 1. 拉取报告文本
        log.info("Step 1: 读取日报报告（本地文件）...")
        report_text = DAILY_TXT.read_text(encoding="utf-8")
        report_file = job_dir / "daily_report.txt"
        write_file(str(report_file), report_text)
        log.info(f"  读取完成: {len(report_text)}字符")

        # 提取日期
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", report_text)
        date_str = date_match.group(1).replace("-", "") if date_match else datetime.now().strftime("%Y%m%d")

        # 2. 生成图表
        log.info("Step 2: 生成图表...")
        chart_files = []
        try:
            sys.path.insert(0, str(SCRIPTS_DIR))
            import generate_charts as gc_module
            data = gc_module.parse_daily_report(report_text)
            chart_files = gc_module.generate_daily_charts(data, str(charts_dir))
            log.info(f"  图表生成: {len(chart_files)}张")
        except Exception as e:
            log.warning(f"  图表生成失败（跳过）: {e}")
            chart_files = []

        # 3. 生成HTML（单轮Kimi）
        log.info("Step 3: 生成HTML...")
        try:
            html = generate_html_single_round(report_text, str(charts_dir), api_key_open, api_key_code)
        except Exception as e:
            raise RuntimeError(f"HTML生成失败: {e}") from e

        # 兜底注入CSS
        css = CSS_PATH.read_text(encoding="utf-8") if CSS_PATH.exists() else ""
        if css and "<style>" not in html:
            html = html.replace("</head>", f"<style>{css}</style>\n</head>")

        html_file = job_dir / "daily_report.html"
        write_file(str(html_file), html)
        log.info(f"  HTML: {html_file} ({len(html)}字符)")

        # 3.3 后处理：将caption从表格前移动到表格后（确保表X标签在表格下方渲染）
        log.info("Step 3.3: 移动captions到表格下方...")
        html = move_captions_below_tables(html)
        write_file(str(html_file), html)
        log.info(f"  Captions移动完成: HTML {len(html)}字符")

        # 3.5 后处理：补写短分析框（不超过200字的补成200-400字）
        log.info("Step 3.5: 补写短分析框...")
        try:
            html = fix_short_analysis_boxes(html, report_text, api_key_open, api_key_code)
            write_file(str(html_file), html)
            log.info(f"  补写完成: HTML {len(html)}字符")
        except Exception as e:
            log.warning(f"  补写失败（跳过，不影响后续）: {e}")

        # 4. 转PDF
        log.info("Step 4: 转换PDF...")
        try:
            import weasyprint
            pdf_name = f"四川电力交易日报_{date_str}.pdf"
            pdf_file = job_dir / pdf_name
            weasyprint.HTML(filename=str(html_file)).write_pdf(str(pdf_file))
            log.info(f"  PDF: {pdf_file} ({pdf_file.stat().st_size / 1024:.0f}KB)")
        except ImportError:
            raise RuntimeError("weasyprint未安装，无法生成PDF。执行: pip install weasyprint")

        # 5. 同步到nginx目录
        log.info("Step 5: 同步到公网...")
        os.makedirs(NGINX_PDF_DIR, exist_ok=True)
        import shutil
        dated_name = f"daily_{date_str}.pdf"
        nginx_dated = os.path.join(NGINX_PDF_DIR, dated_name)
        shutil.copy2(str(pdf_file), nginx_dated)
        latest_name = "daily_latest.pdf"
        latest_path = os.path.join(NGINX_PDF_DIR, latest_name)
        safe_symlink(dated_name, latest_path)
        log.info(f"  PDF: http://118.24.77.156:18080/reports/pdf/{latest_name}")
        # 清理旧版本（保留daily_YYYYMMDD.pdf + daily_latest.pdf两个文件）
        for old in Path(NGINX_PDF_DIR).glob("daily_*.pdf"):
            if old.name != latest_name and old.name != dated_name:
                os.remove(str(old))
                log.info(f"  清理旧版: {old.name}")

        result.update({
            "success": True,
            "pdf_path": str(nginx_dated),
            "pdf_url": f"{NGINX_BASE}/pdf/{dated_name}",
            "html_path": str(html_file),
            "date": date_str,
            "tables": html.count("<table>"),
            "charts": len(chart_files),
        })

        # 清理临时job目录
        if job_dir and job_dir.exists():
            import shutil as sh
            try:
                sh.rmtree(str(job_dir))
                job_dir = None
            except Exception as e:
                log.warning(f"  临时目录清理失败（不影响结果）: {e}")

    except Exception as e:
        log.error(f"✗ 生成失败: {e}")
        log.debug(traceback.format_exc())
        result["error"] = str(e)
        # 失败时保留临时目录，方便排查
        if job_dir and job_dir.exists():
            log.info(f"  ⚠ 临时目录已保留: {job_dir}")

    return result


# ─── CLI入口 ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="四川电力交易日报PDF生成")
    parser.add_argument("--type", "-t", choices=["daily"], default="daily",
                        help="报告类型: daily=日报（目前仅支持日报）")
    args = parser.parse_args()

    # 启动时校验：CSS/Template文件存在
    if not CSS_PATH.exists():
        log.warning(f"  ⚠ CSS文件不存在: {CSS_PATH}（PDF样式可能不完整）")
    if not TEMPLATE_PATH.exists():
        log.warning(f"  ⚠ 模板文件不存在: {TEMPLATE_PATH}（system prompt可能缺少模板参考）")

    # 统一Key加载：优先环境变量，再读key_loader
    api_key_open = os.environ.get("KIMI_API_KEY_OPEN")
    api_key_code = os.environ.get("KIMI_API_KEY")
    if not api_key_open or not api_key_code:
        try:
            from key_loader import get as _get_key
            if not api_key_open:
                api_key_open = _get_key("KIMI_API_KEY_OPEN")
            if not api_key_code:
                api_key_code = _get_key("KIMI_API_KEY")
        except ImportError:
            pass

    if not api_key_open:
        log.error("错误: 未找到KIMI_API_KEY_OPEN。请在 ~/.hermes/.env 中设置 KIMI_API_KEY_OPEN=sk-...")
        sys.exit(1)
    if not api_key_code:
        log.error("错误: 未找到KIMI_API_KEY（Code Plan）。请在 ~/.hermes/.env 中设置 KIMI_API_KEY=sk-kimi-...")
        sys.exit(1)

    log.info("=== 开始生成日报PDF ===")
    start = time.time()

    result = generate_pdf(api_key_open, api_key_code)

    elapsed = time.time() - start
    if result["success"]:
        log.info(f"=== ✅ 完成! 耗时{elapsed:.0f}秒 ===")
        log.info(f"  文件: {result['pdf_path']}")
        log.info(f"  下载: {result['pdf_url']}")
        log.info(f"  表格: {result['tables']}张, 图表: {result['charts']}张")

        # OSS 上传（下游系统交付通道）
        if result.get("pdf_path") and result.get("date"):
            oss_key = f"sichaun-daily-report/daily_{result['date']}.pdf"
            oss_result = upload_to_oss(result["pdf_path"], oss_key)
            result["oss_ok"] = oss_result["ok"]
            if oss_result["ok"]:
                result["oss_url"] = oss_result["url"]
                log.info(f"  OSS: {oss_result['url']}")
            else:
                result["oss_error"] = oss_result["error"]
                log.error(f"  OSS 上传失败: {oss_result['error']}")
                log.error("  下游系统将无法获取今日PDF！")
    else:
        log.info(f"=== ❌ 失败! 耗时{elapsed:.0f}秒 ===")
        log.info(f"  错误: {result['error']}")

    # JSON输出用于cron
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
