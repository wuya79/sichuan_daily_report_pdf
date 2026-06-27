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

DAILY_URL = f"{NGINX_BASE}/daily_latest.txt"

KIMI_BASE_URL = "https://api.moonshot.cn/v1"
KIMI_MODEL = "moonshot-v1-128k"

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

def kimi_call(api_key: str, system_prompt: str, user_message: str,
              timeout: int = 600, max_tokens: int = 100000) -> str:
    """调用Kimi API，带超时保护。使用httpx超时控制。"""
    import openai
    import httpx

    http_client = httpx.Client(timeout=httpx.Timeout(timeout, connect=10, read=timeout, write=10))
    client = openai.OpenAI(api_key=api_key, base_url=KIMI_BASE_URL, http_client=http_client, max_retries=0)

    resp = client.chat.completions.create(
        model=KIMI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.2,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content


def extract_html(raw: str) -> str:
    """从Kimi返回内容中提取HTML。带fallback，永不raise。"""
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


def fetch_report_text(url: str, timeout: int = 15) -> str:
    """从URL拉取报告文本。"""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "PDF-Generator/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        for enc in ["utf-8", "gbk", "gb2312"]:
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")


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
3. 总表格数必须达到{table_count}张，每张表必须有caption(data-label="表X")
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

def fix_short_analysis_boxes(html: str, report_text: str, api_key: str) -> str:
    """
    检测HTML中所有analysis-box/info-box/warning-box的内容长度，
    对不足200字的框，单独调用Kimi补写成200-400字的详细分析。
    直接在HTML中替换原框内容。
    返回补写后的完整HTML。
    """
    import openai
    import httpx

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
        new_analysis = _call_kimi_for_analysis(api_key, report_text, section_hint, old_text)
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


def _call_kimi_for_analysis(api_key: str, report_text: str, section_hint: str, old_text: str) -> str:
    """调用Kimi补写单个分析框。短调用，timeout=60s，max_tokens=2000。"""
    import openai
    import httpx

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

    try:
        http_client = httpx.Client(timeout=httpx.Timeout(60, connect=10, read=60, write=10))
        client = openai.OpenAI(api_key=api_key, base_url=KIMI_BASE_URL, http_client=http_client, max_retries=0)
        resp = client.chat.completions.create(
            model=KIMI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
            max_tokens=2000,
        )
        text = resp.choices[0].message.content.strip()
        # 如果返回了markdown代码块，提取
        if "```" in text:
            text = text.split("```")[1].split("```")[0].strip() if "```" in text else text
        return text
    except Exception as e:
        log.warning(f"    补写失败（跳过）: {e}")
        return ""



# ─── 生成HTML（单轮） ─────────────────────────────────────────────

def generate_html_single_round(report_text: str, charts_dir: str, api_key: str) -> str:
    """单轮调用Kimi生成完整HTML。"""
    chart_files = sorted(Path(charts_dir).glob("*.png")) if os.path.exists(charts_dir) else []
    chart_refs = "\n".join([f'<img src="charts/{f.name}">' for f in chart_files]) if chart_files else ""

    system_prompt = build_system_prompt()

    user_message = f"""请根据以下四川电力交易日报数据，生成完整的HTML报告。

【硬性要求 - 必须遵守】
1. 输出必须是完整HTML（从<!DOCTYPE html>到</html>），不要Markdown代码块包裹
2. 必须包含封面页（.cover）、目录页（.toc-page）、全部10个section
3. 每个section必须包含2-4张数据表格，每张表格必须有6-12行数据
4. 总表格数量必须达到25张以上
5. 每张表格必须有caption(data-label="表X")编号
6. 每个section结尾必须有.analysis-box或.info-box进行分析
7. 趋势仪表盘section必须引用图表：<img src="charts/xxx.png">
8. 所有单元格必须填入实际数据，不能留空或写"--"
9. 每个分析框内容不少于200字（约5-8句），结合表格数据展开分析趋势原因、数据变化原因和市场影响，遵循四川电力市场规则（水期划分、边际定价机制、供需关系），不得臆想

【已生成的图表文件 - 在HTML中引用】
{chart_refs}

【报告原始数据 - 请解析所有内容生成表格】
{report_text[:12000]}

请直接输出完整HTML代码。"""

    log.info("调用Kimi API生成HTML...")
    raw = kimi_call(api_key, system_prompt, user_message, timeout=600)
    html = extract_html(raw)
    log.info(f"HTML生成完成: {len(html)}字符")
    return html


# ─── 主流程 ─────────────────────────────────────────────────────

def generate_pdf(api_key: str) -> dict:
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
        log.info("Step 1: 拉取日报报告...")
        report_text = fetch_report_text(DAILY_URL)
        report_file = job_dir / "daily_report.txt"
        write_file(str(report_file), report_text)
        log.info(f"  拉取完成: {len(report_text)}字符")

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
            html = generate_html_single_round(report_text, str(charts_dir), api_key)
        except Exception as e:
            raise RuntimeError(f"HTML生成失败: {e}") from e

        # 兜底注入CSS
        css = CSS_PATH.read_text(encoding="utf-8") if CSS_PATH.exists() else ""
        if css and "<style>" not in html:
            html = html.replace("</head>", f"<style>{css}</style>\n</head>")

        html_file = job_dir / "daily_report.html"
        write_file(str(html_file), html)
        log.info(f"  HTML: {html_file} ({len(html)}字符)")

        # 3.5 后处理：补写短分析框（不超过200字的补成200-400字）
        log.info("Step 3.5: 补写短分析框...")
        try:
            html = fix_short_analysis_boxes(html, report_text, api_key)
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
            "pdf_url": f"{NGINX_BASE}/pdf/{latest_name}",
            "html_path": str(html_file),
            "date": date_str,
            "tables": html.count("<table>"),
            "charts": len(chart_files),
        })

        # 清理临时job目录
        if job_dir and job_dir.exists():
            import shutil as sh
            sh.rmtree(str(job_dir))
            log.info(f"  清理临时目录: {job_dir.name}")
            job_dir = None

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

    # 读取kimi api key：优先KIMI_API_KEY环境变量，若没有则从.key文件读取
    api_key = os.environ.get("KIMI_API_KEY")
    if not api_key:
        key_file = SCRIPT_DIR / ".kimi_key"
        if key_file.exists():
            api_key = key_file.read_text().strip().strip("'\"")

    if not api_key:
        log.error("错误: 未找到KIMI_API_KEY环境变量。请在 ~/.hermes/.env 中设置 KIMI_API_KEY=sk-...")
        sys.exit(1)

    log.info("=== 开始生成日报PDF ===")
    start = time.time()

    result = generate_pdf(api_key)

    elapsed = time.time() - start
    if result["success"]:
        log.info(f"=== ✅ 完成! 耗时{elapsed:.0f}秒 ===")
        log.info(f"  文件: {result['pdf_path']}")
        log.info(f"  下载: {result['pdf_url']}")
        log.info(f"  表格: {result['tables']}张, 图表: {result['charts']}张")
    else:
        log.info(f"=== ❌ 失败! 耗时{elapsed:.0f}秒 ===")
        log.info(f"  错误: {result['error']}")

    # JSON输出用于cron
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
