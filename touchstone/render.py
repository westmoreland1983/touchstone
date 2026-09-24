#!/usr/bin/env python3
# ============================================================================
# touchstone/render.py —— 评审报告渲染层（七段版面填充）
# ----------------------------------------------------------------------------
# 从 orchestrator 拆出（模块职责单一化）：orchestrator 编排链路，本模块只负责把
# 结构化结果填进版面。版面由 templates/review_report.md 唯一定义——模板是设计资产，
# 代码只填充、不定义版面（修订设计 §3 意见 4）。
# 拆分同时根治一处运行期地雷：原 render_findings 函数内的 `from llm_budget import`
# 平铺导入在移除 sys.path hack 后必然 ModuleNotFoundError，且因该分支缺测试覆盖 +
# 个别测试文件污染 sys.path 而在全量测试中被掩盖（单跑文件才炸）。现改为顶层包导入。
# ============================================================================

import html
import json
import os
import re
import sys

from touchstone.llm_budget import MAX_FINDINGS_IN_SUMMARY
from touchstone.checklist import sig_of          # 清单签名构造（finding → sig，做 findings↔清单项 join）
from touchstone.checklist import MACHINE_DONE_NOTES   # 机器核销固定 note——呈现层静默（数据层保留审计）

_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "templates", "review_report.md")


def _load_template():
    """读七段版面模板（修订设计 §3 意见 4）。模板是设计资产：代码只填充，不定义版面。
    读取失败退回极简版面（防模板缺失把评审主链打断），并在 stderr 留痕。"""
    try:
        with open(_TEMPLATE_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        print(f"[warn] 版面模板读取失败（{e}），使用内置极简版面", file=sys.stderr)
        return "{{status}}\n\n{{alerts}}\n\n{{facts}}\n\n{{findings}}\n\n{{reference}}\n\n{{markers}}"


_TEMPLATE_SLOT_RE = re.compile(r"\{\{(\w+)\}\}")


def _fill_template(template, parts):
    """单遍填充版面 `{{占位符}}`（A2-F1 防注入）。

    此前用顺序 `for k,v: out = out.replace("{{k}}", v)` 累积替换——每次 replace 重扫整段 out，
    于是【已填入】的段落文本若含占位符（finding 的 rationale/banner 等内容里出现 `{{markers}}`、
    `{{checklist}}` 等——LLM 输出、对抗构造、或 legitimately 讨论模板），后续步骤会把它当模板
    占位符展开：把 markers/checklist 段内容注入 finding 文本（占位符注入 / 串段）。

    re.sub 单遍替换只扫模板一次、替换文本不再被重扫，故占位符出现在内容值里时保持字面。
    未知占位符保持原样（同旧 str.replace 对未匹配键的行为）；值统一 str() 以防非字符串。"""
    return _TEMPLATE_SLOT_RE.sub(lambda m: str(parts.get(m.group(1), m.group(0))), template)


def render_unreliable_callout(engine_status, ai_raw_count=0, added_lines=0, engine_detail=""):
    """本轮评审不可信时的置顶告警——[!CAUTION] 红框置顶，替代常规溯源/降级横幅。精简到两行：
    点明【失败环节】+ 后果 + 指向。具体可靠的原始错误在紧随其后的折叠块（本框不塞原始
    dump）。判定层（销项/收敛/放行）已由 review_reliable 挡住；本函数把同一信号接到呈现层。"""
    _WHERE = {
        "no_engine": "评审引擎未启动",
        "provider_failed": "取 PR 失败",
        "llm_failed": "LLM 调用失败",
        "skipped_large_diff": "diff 超预算被跳过",
    }
    where = _WHERE.get(engine_status) or f"疑似空收敛（约 {added_lines} 行改动却 {ai_raw_count} 建议）"
    tail = "；原始错误见下方折叠块" if (engine_status != "ok" and engine_detail) else ""
    return "\n".join([
        "> [!CAUTION]",
        f"> **本轮 AI 评审不可信**：{where}。",
        f"> 请人工评审{tail}。",
    ])


def _engine_detail_fold(engine_status, engine_detail):
    """降级原始错误折叠块（v3：「验证与日志」段移除后并入②告警区——诊断不丢，默认折叠不占屏）。

    入参须已经 orchestrator._redact_secrets 脱敏（呈现边界纪律，PR #74）；本层补齐 HTML 转义
    （_html_text——错误正文可含 <script>/反引号等）+ 1500 截断标记（不静默砍尾）+ <pre> 包裹
    （<details> 内是 type-6 HTML block，markdown 围栏不解析——用 <pre>，同申报指引段纪律）+
    空行折叠为单换行（空行会终止 HTML block，#168 同源教训）。纯函数、可测。"""
    if engine_status == "ok" or not engine_detail:
        return ""
    d = re.sub(r"\n\s*\n+", "\n", engine_detail.strip())
    # 截断指针按状态区分（PRA-REVIEW round-3）：仅 llm_failed 下 PR-Agent 子进程真跑过、
    # pr-agent-interaction.log artifact 才存在；no_engine/provider_failed 时引擎没起/取数
    # 失败，指过去是死链——误导人工排障。被截内容仍完整落 runner stderr（Actions 日志）。
    if len(d) > 1500:
        ptr = ("，完整内容见 pr-agent-interaction.log artifact"
               if engine_status == "llm_failed" else "，完整内容见本 job 运行日志")
        shown = d[:1500] + f"\n[…]（已截断：原始错误超 1500 字符{ptr}）"
    else:
        shown = d
    return ("<details><summary>评审引擎降级（" + _html_text(engine_status)
            + "）——原始错误</summary>\n"
            f"<pre>{_html_text(shown)}</pre>\n</details>")


# v2：_location（v1 位置串渲染）已移除——sig 兼作位置显示（checklist.sig_of 在构造时
# 防 `file:None`），不再单列位置行。位置精度兜底移至 sig_of 构造源（一处修两处一致）。


_REASONING_COLLAPSE_THRESHOLD = 200
_TEASER_MAX = 60                          # <details> summary 露首句/前 N 字（不展开也露关键点）
# 句末标点：CJK 全角（。！？）无条件算句末；ASCII 半角（.!?）仅在后接空白/串尾时算句末
# —— 否则版本号（0.2.3）、小数、缩写（e.g.）、域名里的 . 会被误判为句末，teaser 在
# 「版本号从 0.」处截断（实测）。CJK 文本无词间空格，全角句号后直接接下一句，故全角
# 不设边界条件。
#
# 已知局限（#168 round-4）：ASCII 句号直连 CJK 字符（无空格，如「fixed.版本号」）不
# 被识别为句末——lookahead 要求 . 后接空白/串尾。此类混合写法罕见（混排时通常用 。 或
# 加空格），此时 teaser overrun 到 max_len 硬截断，可接受；要识别需在 lookahead 加
# 「后接非 ASCII」分支，但会让 0.2.3 等边角更难推理，收益不抵复杂度。
_SENTENCE_END = re.compile(r"[。！？]|[.!?](?=\s|$)")


def _html_text(text):
    """LLM/清单来源的自由文本进报告前的中性化：html.escape(quote=False)——`&`/`<`/`>` 转实体。

    动机（PR #191 round-7 实录）：依据折叠的 <details> body 处在 CommonMark type-6 HTML
    block 内——块内 markdown（含反引号 code span）一律不生效，全是 raw HTML。LLM 在依据
    里引用 marker 语法写字面 `<!-- touchstone-checklist:` 时，浏览器把它当真注释开符，
    一路吞到下一个 `-->`（恰好是页尾 loop marker 的闭符），中间的 清单尾部/参考信息/
    如何申报销项/验证与日志/要点速览 在渲染层整体消失（GraphQL bodyHTML 证实：四段
    find=-1）——评论源码仍在、marker 存 reasoning 原文，API 取全文零丢失，仅 UI 不可见。
    反方向（#196 修的是 marker 载荷）：字面 `-->` 作孤儿闭符裸露成可见乱文。转义后两向
    皆中性：`&lt;!--`/`--&gt;` 渲染回原字符，纯文本视觉无损；quote=False 不动引号。

    站点纪律：凡 findings/清单项（LLM 产出或作者 ack 文本）来源的自由字段进报告必经此
    函数——direction/rationale/reasoning/note/guard/复核问题，以及折叠 body/summary teaser。
    机器字段（sig、rule_id、status 标签等）由本系统构造、不含 `<>`，不经此函数保持原样；
    自产 marker（<!-- touchstone-* -->）是机器可读结构，同样保持原样。"""
    return html.escape(text or "", quote=False)


# ---------------- 文档级不变量：报告正文的 HTML 注释配对必须封闭在自产 marker 上 ----------------
# 这是 L2 运行时闸门（L1=各嵌入点 _html_text 转义；L3=对抗性属性测试）。点修（#196 修
# marker 载荷、#197 修自由文本）锁的是已知洞；本层锁的是洞的【类别】——无论未来哪个新
# 嵌入点漏了转义，POST 前的单遍扫描都能发现并把坏 token 中性化，坏文档不再出门。
_COMMENT_OPEN = "<!--"
_COMMENT_CLOSE = "-->"
# 自产 marker 的已知种类：loop/checklist/result/finding（带载荷）+ collapsed 哨兵（无载荷）。
# 刻意穷举而非 `touchstone-\w+`：白名单外的 `<!-- touchstone-...` 本身就该被拦下（防伪造）。
_KNOWN_MARKER_RE = re.compile(
    r"<!-- touchstone-(?:loop|checklist|result|finding):|<!-- touchstone-collapsed -->")


def _marker_payload_ok(body, m, close_at):
    """marker 前缀（m.match）到闭符 close_at 之间的载荷必须是完整 JSON——只认前缀不够：
    LLM 常在正文里【原样引用】marker 语法（round-7 实录：`` `<!-- touchstone-checklist:` ``），
    前缀完全相同。真 marker 载荷是 html_comment_safe_json 产物，raw_decode 必然吃满；
    引用/伪造的载荷是自然语言，必失败 → 归 bad-open。collapsed 哨兵无载荷，单独放行。"""
    if m.group(0).endswith(_COMMENT_CLOSE):        # `<!-- touchstone-collapsed -->`
        return True
    seg = body[m.end():close_at].strip()
    try:
        _obj, end = json.JSONDecoder().raw_decode(seg)
    except ValueError:
        return False
    return end == len(seg)


def scan_report_body(body):
    """按浏览器 HTML 注释配对规则单遍扫描正文，返回事件列表（validate/sanitize 共用）：

    ("marker", i, j)        自产 marker：i 处开符到闭符 j（不含 j 后内容），合法
    ("bad-open", i)         非自产 `<!--`（含前缀对但载荷非 JSON 的引用/伪造 marker）：
                            会开真注释吞掉后文（#197 修的洞——渲染层丢段）
    ("orphan-close", i)     孤儿 `-->`：不在任何配对内，裸露成可见乱文（#196 修的洞）
    ("unclosed-marker", i)  自产 marker 开符后无闭符（载荷被截断/损坏）

    模型与浏览器一致：开符吞到【第一个】闭符。marker 载荷经 html_comment_safe_json 保证
    无内嵌 `-->`，故「第一个闭符」即 marker 闭符；坏开符同样吞到第一个闭符——扫描对
    坏开符之后的配对可能过度报告（ swallowed 区内事件仍逐个报），验证器只取首因即可。
    纯函数。"""
    events = []
    i, n = 0, len(body or "")
    while i < n:
        o = body.find(_COMMENT_OPEN, i)
        c = body.find(_COMMENT_CLOSE, i)
        if o == -1 and c == -1:
            break
        if o != -1 and (c == -1 or o < c):
            m = _KNOWN_MARKER_RE.match(body, o)
            j = body.find(_COMMENT_CLOSE, o) if m else -1
            if not m or j == -1 or not _marker_payload_ok(body, m, j):
                if m and j == -1:
                    events.append(("unclosed-marker", o))
                else:
                    events.append(("bad-open", o))
                i = o + len(_COMMENT_OPEN)
                continue
            events.append(("marker", o, j + len(_COMMENT_CLOSE)))
            i = j + len(_COMMENT_CLOSE)
        else:
            events.append(("orphan-close", c))
            i = c + len(_COMMENT_CLOSE)
    return events


def validate_report_body(body):
    """报告正文不变量校验：返回违例描述列表（空=通过）。
    不变量：每个 `<!--` 都是自产 marker 且在本文件内闭合；可见区无孤儿 `-->`。
    附带 `<details>/<summary>/<pre>` 开闭计数平衡检查（不平=有内容被意外折叠/吞掉，
    只报告不自愈——无法机械断定该转义哪一个）。计数只看【注释外】文本：marker 载荷
    JSON 里存的是 reasoning/note 原文，含 `<details>` 等标签属正常（浏览器不解析注释
    内部），计入则误报。"""
    events = scan_report_body(body)
    issues = []
    for ev in events:
        if ev[0] == "marker":
            continue
        pos = ev[1]
        issues.append(f"{ev[0]}@{pos}: …{body[pos:pos+40]}…")
    spans = [(ev[1], ev[2]) for ev in events if ev[0] == "marker"]

    def _outside_count(sub):
        cnt, k = 0, 0
        while True:
            k = body.find(sub, k)
            if k == -1:
                return cnt
            if not any(a <= k < b for a, b in spans):
                cnt += 1
            k += len(sub)

    for tag in ("details", "summary", "pre"):
        o, c = _outside_count(f"<{tag}>"), _outside_count(f"</{tag}>")
        if o != c:
            issues.append(f"tag-balance <{tag}>: open={o} close={c}")
    return issues


def sanitize_report_body(body):
    """L2 自愈：把违例 token 机械中性化（坏 `<!--`/未闭合 marker 开符→`&lt;!--`、孤儿
    `-->`→`--&gt;`），返回 (修复后正文, 修复次数)。marker 闭符永不改写；
    tag-balance 类只交 validate 报告（无法机械断定该转义哪一个）。
    迭代到不变量满足（每轮至少消掉一批坏 token，必然收敛；上限 5 轮防意外死循环）。
    测试纪律：新嵌入点漏转义时，本函数保证坏文档不出门——stderr 由调用方打点。"""
    fixes = 0
    for _ in range(5):
        repl = []
        for ev in scan_report_body(body):
            if ev[0] in ("bad-open", "unclosed-marker"):
                repl.append((ev[1], _COMMENT_OPEN, "&lt;!--"))
            elif ev[0] == "orphan-close":
                repl.append((ev[1], _COMMENT_CLOSE, "--&gt;"))
        if not repl:
            break
        for pos, old, new in sorted(repl, reverse=True):   # 右到左替换，免偏移失效
            body = body[:pos] + new + body[pos + len(old):]
        fixes += len(repl)
    return body, fixes


def _reasoning_teaser(reasoning, max_len=_TEASER_MAX):
    """取 reasoning 首句（或前 max_len 字）作 <details> summary 的关键信息预览。

    summary 不再是无信息标签「依据（N 字）」，而是露核心论断（如「版本号 0.2.3→0.2.5
    跳过了 0.2.4」）——author 扫清单时不展开也能判断这条依据是否值得细读（用户 #168 续：
    「把关键信息的 summary 展示出来」）。纯函数：截断处加 …；换行折叠为空格（summary 是
    单行内联文本，换行会破坏 CommonMark type-6 HTML block 的单块性）。"""
    s = reasoning.strip().replace("\n", " ").replace("\r", "")
    m = _SENTENCE_END.search(s)
    # 首句在预算内（m.end() 含末标点，≤ max_len）→ 取整句；首句超长 → 硬截断到 max_len。
    # 不留余量：严格保证 teaser ≤ max_len + 1（+1 是末尾 …），与 run-on 分支（s[:max_len]）
    # 一致——此前 max_len + 5 余量会让首句 teaser 长达 max_len+6，与 _TEASER_MAX 语义不符。
    first = s[:m.end()] if (m and m.end() <= max_len) else s[:max_len]
    first = first.rstrip()
    if len(first) < len(s):
        first += "…"
    return first


def _render_reasoning(reasoning, indent="   "):
    """渲染依据字段——长文折叠进 <details>（借鉴 pr-agent 上游 #2510 的 Agent Prompt 折叠）。

    pr-agent #2510 评审把详细 Issue description / Issue Context 放 <details> 折叠，默认只露
    标题 + 一句后果，降低视觉噪声。本系统同理：依据 ≤200 字符平铺（短依据是快速判读信号），
    超阈值折叠——summary 露首句/前若干字（关键信息预览，author 不展开也能判读），body 完整
    保留（author 需要细节时展开）。

    indent 参数：子列表项缩进空格数。编号列表（`1. `）用默认 3 空格；task list（`- [ ] `）
    传 2 空格。body 缩进 = indent + 2（`- ` 占 2 字符），使 body 留在子列表项内容区内
    （CommonMark type-6 HTML block 不脱出——见下方折叠分支详注）。

    约束（用户 #168 续——「不能是动态获取，不能影响通过 api 获取全量 review 意见信息」）：
    summary 与 body 均静态嵌入 markdown（非动态获取，点击展开无网络请求）；全文始终在
    details body 内——API 取评论原文即得全量 review 意见，折叠仅影响 GitHub UI 默认展开态、
    不丢一字；机器可读的 <!-- touchstone-checklist --> 结构化标记不在本函数，不受影响。

    纯函数：输入字符串，输出 markdown 片段（空输入返回空串）。"""
    if not reasoning:
        return ""
    body_indent = " " * (len(indent) + 2)   # indent + "- " 2 字符 → body 留在子列表项内容区
    if len(reasoning) <= _REASONING_COLLAPSE_THRESHOLD:
        return f"{indent}- 依据：{_html_text(reasoning)}"
    # 折叠：summary 露字数 + 首句预览（关键信息），body 完整保留（author 需细节时展开）。
    # 用 f-string 而非 .format()：reasoning 含 { 或 } 时（代码片段/JSON 示例），
    # .format(body=reasoning) 虽不解析值里的 {}（值不被二次扫描），但 .format() 调用
    # 形态易让评审/读者误判会炸——f-string 直接内联，无此视觉歧义（评审两轮均提此点）。
    # return 串开头不带 \n：调用方 `"\n" + _render_reasoning(...)` 已加换行，与短依据分支
    # （`f"{indent}- 依据：..."` 开头也无 \n）保持一致——避免折叠分支双换行（评审第三轮提）。
    #
    # <details> 是 CommonMark type-6 HTML block，遇到空行即终止。此前 summary 与 body 之间、
    # body 与 </details> 之间各有一空行 → <details> 在第一个空行处被截断成孤立开标签，
    # body 变成列表项里的松散段落（始终可见、不在折叠区内）、</details> 变孤立闭标签——
    # 表现为「点击展开」点了没反应（展开后空、正文跑到外面）。去空行让整段留在同一 HTML
    # block 内，<details> 才是完整可折叠元素（#167 review 实测回归）。
    #
    # body 同理须防 reasoning 自带空行（多段依据、含 \n\n 的代码片段）：原样嵌 {reasoning}
    # 时其内部空行同样会截断 HTML block（#168 round-2 PRA-POSSIBLE_ISSUE）。折叠 body 的
    # 空白（\s+→空格）成单行——内容一字不丢，仅丢多段排版（折叠区内的显示形态本就不重要）；
    # 机器可读的 <!-- touchstone-checklist --> marker 存的是 reasoning 原文，API 取全文不受影响。
    teaser = _reasoning_teaser(reasoning)
    # 中性化（round-7 实录，见 _html_text）：teaser 进 summary、body 进 details——两处都在
    # HTML block 语境，字面 `<!--`/`-->` 分别会开注释吞段/裸露乱文。转义在空白折叠之后做：
    # 转义只加长源码不改显示，先折叠保证 len(reasoning) 字数与 teaser 截断口径不受影响。
    teaser = _html_text(teaser)
    body = _html_text(re.sub(r"\s+", " ", reasoning).strip())
    # body 与 </details> 须缩进到与 <details> 同列（indent + 2：子列表项 "- " 占 2 字符）。
    # 此前 body 缩进不足会让 body 脱出子列表项的内容区，CommonMark 判其不属于该列表项 →
    # HTML block 在 body 行处截断 → <details> 变空壳、body 渲染成列表项外的松散段落（始终
    # 可见）—— GitHub 实测 body_html 证实。缩进到 body_indent 让 body 留在子列表项内、
    # HTML block 完整（#167/#168 回归修复，v2 参数化缩进以兼容编号列表与 task list）。
    return (f"{indent}- <details><summary>依据（{len(reasoning)} 字）：{teaser}</summary>\n"
            f"{body_indent}{body}\n"
            f"{body_indent}</details>")


# ---- v2 版面函数（2026-08 评审模板重设计：七段 → 六段，去冗余）-------------------------
# v2 移除了 v1 的 _finding_entry / render_facts（含修改范围+规则命中）/ render_findings
# （态势区+AI 评审）三函数——版面合并：态势区→render_status_line（①），AI 评审+清单→
# render_findings_checklist（④），静态检查→render_facts_v2（③，去修改范围+规则命中）。
# 状态标记/措辞从 checklist.py 迁入（呈现层常量归呈现层）。checklist.py 只保留数据层
# （reconcile/parse/marker），可见渲染统一由 render.py 负责。
_STATUS_MARK = {"open": "- [ ]", "done": "- [x]", "waived": "- [x]", "split": "- [x]"}
_STATUS_LABEL = {"open": "⬜ 待处理", "done": "✅ 已复核销项",
                 "waived": "🟡 待人核准（author 豁免）", "split": "🟡 待人核准（author 拆出）"}


def _render_done_criteria(dc):
    """达成判据行内容：deterministic=规则复检；review=人工复核问题。纯函数。"""
    _spec = (dc or {}).get("spec") or {}
    if (dc or {}).get("kind") == "deterministic":
        return f"规则 `{_spec.get('recheck', '?')}` 复检不再命中"
    if (dc or {}).get("kind") == "review":
        q = _spec.get("question", "")
        # q 非空=有具体复核问题；q 空=机器无逐条判据——唯一核验机制是 sig 级 reconcile，
        # 该机制已由要点速览②与条目 sig 锚点表达，逐条复读模板是纯 boilerplate → 不渲染行
        # （#159 诚实降级的呈现面收尾：不伪造具体问题，也不复读机制模板）。
        # q 是 LLM 自由文本（进 HTML block 语境，须中性化——见 render._html_text）
        return f"需人工复核：{_html_text(q)}" if q else ""
    return ""


def _render_finding_meta(f):
    """行尾元数据小字（rule/严重度/置信/来源）。task list 子项缩进 2 空格。
    用 .get() 兜底：清单项可能源自只含部分字段的 finding（如测试夹具缺 confidence）。"""
    conf = f.get("confidence")
    conf_str = f"{conf:.2f}" if conf is not None else "—"
    return (f"  - <sub>`{f.get('rule_id', '?')}` · {f.get('severity', '')} · "
            f"置信 {conf_str} · 来源 {f.get('agent', '')}</sub>")


def render_facts_v2(scope_facts, gate_line=""):
    """③ v2 静态检查区：仅确定性事实——敏感路径命中 + 门禁状态。
    修改范围与 PR UI 统计重复 → 删除（观测意见 7）。规则命中详情并入「评审发现与销项」段。
    无内容（简单 PR 无敏感路径/无门禁）时整段省略——去恒定噪声。"""
    if not scope_facts:
        return ""
    if not scope_facts.get("parse_ok", True):
        return f"### 静态检查\n\n- ⚠️ {scope_facts.get('parse_warning', 'diff 解析失败：范围事实未生效')}"
    lines = []
    hits = scope_facts.get("sensitive_hits", [])
    if hits:
        by_rule = {}
        for h in hits:
            by_rule.setdefault(h["rule"], []).append(h["path"])
        for rule, paths in sorted(by_rule.items()):
            shown = ", ".join(f"`{p}`" for p in paths[:5]) + ("…" if len(paths) > 5 else "")
            lines.append(f"- 敏感路径命中（{rule}）：{shown}")
    if gate_line:
        lines.append(f"- 门禁状态：{gate_line}")
    if not lines:
        return ""            # 只有修改范围（已删）→ 整段省略（观测意见 7：简单 PR 零噪声）
    return "### 静态检查\n\n" + "\n".join(lines)


def render_status_line(risk, loop_info=None, checklist=None, rounds_left=None,
                       review_reliable=True):
    """① v2 状态行（合并观测意见 1+6）：循环决策 + 轮次 + 销项率 + 风险等级 — 合成一行 blockquote，
    替代旧版「横幅反馈循环行 + 态势风险行」两行。escalate 的 reason 有诊断价值（为何升级），保留；
    continue/converged 的 reason 是对轮次/销项率的复述——已在状态行结构化呈现，不复读。"""
    _RISK = {"high": "高", "mid": "中", "low": "低"}
    _ACTION = {"read+arbitrate": "需人工评审后合入", "read": "建议人工过目", "skip": "无需人工介入"}
    _DECISION = {"continue": "🔁 继续", "converged": "✅ 收敛", "escalate": "⬆️ 升级到人"}
    _BLAST = {"cross_module_contract": "跨模块契约变更", "security_surface": "涉及安全面"}

    parts = []
    if loop_info:
        decision = loop_info[0]
        reason = loop_info[1] if len(loop_info) > 1 else ""
        parts.append(_DECISION.get(decision, decision))
        if decision == "escalate" and reason:
            parts.append(reason)         # escalate reason 有诊断价值（为何升级），保留
    cl = checklist or {}
    if cl.get("round"):
        round_part = f"第 {cl['round']} 轮"
        if rounds_left is not None:
            round_part += f" · 剩余 {rounds_left} 轮"
        parts.append(round_part)
    if cl.get("items") and cl.get("resolved_rate") is not None:
        # 真值检查（非 `is not None`）：空清单 items=[] 时 resolved_rate=1.0（_rate 空列表
        # 归一），若用 `is not None` 会漏过空列表显示「销项率 100%」——对零项清单是噪声，
        # 与 v2 去冗余目标矛盾（PRA-GENERAL round-1）。空清单无项可销，不显示销项率。
        rate = min(100, max(0, int(round(cl["resolved_rate"] * 100))))
        parts.append(f"销项率 {rate}%")

    band = _RISK.get(risk.get("risk_band"), "未定")
    action = _ACTION.get(risk.get("human_action"), "建议人工过目")
    if review_reliable:
        parts.append(f"风险等级：{band} — {action}")
    else:
        parts.append(f"风险等级：{band}（LLM 评审不可信）— 需人工评审")

    line = "> " + " · ".join(parts)
    factors = "、".join(_BLAST.get(b, b) for b in (risk.get("blast_radius") or []))
    if factors:                       # 无触发因子时不显「触发因子：无」——去冗余（观测意见 7 同纪律）
        line += f"\n> **触发因子：** {factors}"
    return line


def render_findings_checklist(findings, checklist, review_reliable=True):
    """④ v2 评审发现与销项（合并观测意见 2+3+4）：AI 评审 + 待解决问题清单合为一段。
    所有发现（确定性规则命中 + LLM 建议）作为 - [ ] task list，每条含完整详情 + 销项状态。
    保留 GitHub 原生 checkbox（用户明确要求）。sig 兼作位置与 ack 锚点（不再单列位置/锚点行）。
    样板「销项跟踪：…见上方」删除（详情已在同段，观测意见 3）。

    findings↔checklist 按 sig join：开放项有当前 finding（显示完整详情 + 最新 direction），
    已销项项可能无当前 finding（用清单存储的 direction/reasoning 历史快照，sparse 显示）。
    排序：开放项在前（按置信降序），已销项项在后（不再抢注意力）。"""
    cl = checklist or {"round": 0, "items": [], "resolved_rate": 1.0}
    items = cl.get("items", [])
    if not items:
        return ""    # 无清单项（干净 PR / 全销项）→ 整段省略（与 ③/⑤「无内容整段省略」纪律一致，
                     # PRA-REVIEW round-2）。防静默故障溯源在 ② alerts 段（已端到端运行/0 条建议），
                     # 不靠此处的恒定标题——此前「### 评审发现与销项\n本次无可自改发现。」是干净
                     # PR 上的恒定噪声，与 v2 去冗余目标矛盾。

    # findings↔清单项 join 用 sig 建索引。重复 sig（同 rule:file:line 的多条 finding）
    # keep-first——与 from_findings 的去重（seen 集，keep-first）一致：清单项已是首条，
    # 索引也取首条才不出现「清单用首条 direction、元数据却取末条」的错配（PRA-GENERAL round-5）。
    finding_by_sig = {}
    for f in (findings or []):
        s = sig_of(f)
        if s not in finding_by_sig:
            finding_by_sig[s] = f

    def _sort_key(it):
        f = finding_by_sig.get(it["sig"])
        conf = f.get("confidence", 0) if f else 0
        return (0 if it["status"] == "open" else 1, -conf)

    total = len(items)
    n_open = sum(1 for it in items if it["status"] == "open")
    capped = total > MAX_FINDINGS_IN_SUMMARY
    head = f"### 评审发现与销项（共 {total} 条"
    if n_open:
        head += f"，待销项 {n_open}"
    if capped:
        head += f"，仅列前 {MAX_FINDINGS_IN_SUMMARY} 条"
    head += "）"
    lines = [head, ""]

    # 封顶：大 PR 产出大量条目 → 列表封顶避免撑破 GitHub 65536 字符限。开放项优先（sort_key
    # 使 open 排前），已销项项按预算余量跟进。超出部分折叠到尾注（完整清单在 marker 里有）。
    shown = sorted(items, key=_sort_key)[:MAX_FINDINGS_IN_SUMMARY]
    for it in shown:
        f = finding_by_sig.get(it["sig"])
        mark = _STATUS_MARK.get(it["status"], "- [ ]")
        label = _STATUS_LABEL.get(it["status"], "")
        # 开放项用当前发现的 direction（最新评审）；已销项用清单存储的（历史快照）
        if f:
            direction = f.get("fix_direction") or f.get("suggested_fix") or ""
            rationale = f.get("rationale") or ""
            reasoning = f.get("fix_reasoning") or ""
            dc = f.get("done_criteria") or {}
        else:
            direction = it.get("direction") or ""
            rationale = ""
            reasoning = it.get("reasoning") or ""
            dc = it.get("done_criteria") or {}
        # 标题：方向作标题（加粗）。无方向时按状态区分占位——open 项「待补」提示 author 补方向；
        # 已销项项（done/waived/split）方向是历史快照、本就可能未留存，「待补」会误导（待补=待办，
        # 但已销项无需再补）→ 标「已销项」（PRA-REVIEW round-3 data-loss）。
        if direction:
            title = f"**{_html_text(direction)}**"
        elif it["status"] == "open":
            title = "（待补修复方向）"
        else:
            title = "（已销项）"
        lines.append(f"{mark} {title}" + (f" {label}" if label else "") + f" — `{it['sig']}`")
        # 守卫事实不再单列一行（v3 瘦身，用户 2026-09-10：人不怎么看）——折叠为行尾 <sub>
        # 小字：优先挂「依据」行尾，无依据挂「问题」行尾，两皆无才单列小字行。marker 的
        # item["guard"] 原样持久化，C 面核销注入与 waived 反证引用不受影响（数据层零改动）。
        # guard 可含字面 `<module>`（裸路径守卫的函数名占位）——不转义会被浏览器当
        # 未知内联标签吞掉（round-7 实测：`函数 <module>：无守卫` 渲染丢 `<module>`）
        guard = it.get("guard") or ""
        gsub = f" <sub>（守卫：{_html_text(guard)}）</sub>" if guard else ""
        # 挂载判定以「是否已挂上」为准、不以「走没走依据分支」为准（PRA-REVIEW round-3：
        # 旧 elif 挂在依据分支的条件上——若 _render_reasoning 对真值输入返回空（未来演化），
        # gsub 未消费、elif 又不评估，守卫会跳过「问题」行直落兜底行，违背优先级）。
        guard_attached = False
        # rationale（问题陈述）作首条子项；与 direction 同文则省（去冗余，同 _finding_entry 纪律）
        if rationale and rationale != direction:
            lines.append(f"  - {_html_text(rationale)}")
        # reasoning（依据）与 rationale 或 direction 同文则省——成对去冗余（PRA-REVIEW round-4：
        # 已销项项 rationale="" 时 `reasoning != ""` 恒真，若 reasoning==direction 会复读标题，
        # 补 `!= direction` 守卫使两分支（open 有 finding / resolved 无 finding）去冗余一致）。
        if reasoning and reasoning != rationale and reasoning != direction:
            r = _render_reasoning(reasoning, indent="  ")   # task list 子项缩进 2 空格
            if r:
                lines.append(r + gsub)
                guard_attached = True
        if gsub and not guard_attached and rationale and rationale != direction:
            lines[-1] += gsub                                # 无依据行 → 挂「问题」行尾
            guard_attached = True
        dc_line = _render_done_criteria(dc)
        if dc_line:
            lines.append(f"  - 达成判据：{dc_line}")
        # 说明行只在携带非机制信息时渲染：机器核销 done 的固定 note（MACHINE_DONE_NOTES）
        # 与「✅ 已复核销项」标签同义，逐条复读是 boilerplate → 静默（marker 仍留审计轨迹）；
        # author 内容（waived/split 反证）与受理失败原因照常显示。
        if it.get("note") and it["note"] not in MACHINE_DONE_NOTES:
            lines.append(f"  - 说明：{_html_text(it['note'])}")
        if gsub and not guard_attached:                      # 问题/依据行皆无 → 单列小字行兜底
            lines.append(f"  - <sub>守卫：{_html_text(guard)}</sub>")
        if f:
            lines.append(_render_finding_meta(f))
    if capped:
        lines.append("")
        lines.append(f"……另有 {total - MAX_FINDINGS_IN_SUMMARY} 条（超列表上限，完整清单见 marker）。")
    return "\n".join(lines)


# 销项规程正本的规范 URL（上游仓）。评审部署在【任意受评仓】运行——开发者代码仓里
# 没有 skills/ 目录，指针不能引用受评仓本地路径（不存在），也不能用 GITHUB_REPOSITORY
# 拼 URL（会指向受评仓自己的 404）；统一指上游正本。不贴正文（#192 二级折叠后
# 用户仍反馈混乱，拍板只留链接），渲染零文件依赖。
_ACK_SKILL_URL = "https://github.com/AKDI-SE/touchstone/blob/main/skills/touchstone-ack/SKILL.md"


def _ack_skill_ref():
    """给【提交代码的 agent】的销项规程指针（评审评论必经之地 = 最可靠的提醒触点）。

    恒出现——不依赖受评仓是否携带 skills/（开发者代码仓没有）：规范 URL + 内联
    要点速览（无网 agent 至少从评论本身拿到最易错的规则；与 SKILL.md §2/§3/§4/§7
    同源，改口径须两处同步——test_skill_doc_sync 有防漂移断言）。行间用 <br>：本段
    落在 HTML block 里，裸 \n 浏览器不换行（#192 教训）。

    【不贴正本正文】（#192 二级折叠后用户仍反馈混乱，拍板只留链接）：速览控制在
    6 行内——每行一个决策点，全文以链接为准；渲染零文件依赖。"""
    return (f"Agent 销项完整规程（可安装为 skill 或直接参考）：{_ACK_SKILL_URL}<br>\n"
            "要点速览：<br>\n"
            "① 销项只认 PR 级评论的围栏申报，行内评论线程一律不计数、不报错；<br>\n"
            "② done=已改码——下轮复检签名不再命中才落账；waived=驳回——须附可核查"
            "反证（文件行号/实测输出），归人核准；split=拆后续 PR——须附链接；<br>\n"
            "③ 时序：改码 → 提交 → 发 ack 评论（并空提交触发评审） → 推送（复检只读"
            "启动前已存在的申报，推送后补 ack 本轮不计；空提交与 ack 同点承载触发，"
            "无代码改动的纯 ack 轮同理）；<br>\n"
            "④ 修复须落进测试套件；同根因发现逐条列出并注明同源；<br>\n"
            "⑤ 销项是多轮交互：每轮复检可能新增 findings——推送申报后自行启动定时"
            "任务，每 1 分钟检查一次最新评审评论，直到 ✅ 收敛或 ⬆️ 升级到人；<br>\n"
            "⑥ 本地全量测试不必每轮跑：首次申报前与 findings 清零后各一次，"
            "中间轮跑定向测试即可。")


def render_reference(has_checklist_items=False):
    """⑤ 申报指引（v3 瘦身：「参考信息」段壳与「验证与日志」折叠块移除——健康轮它只承载一行
    运行链接且 check-run 页可达，降级轮原始错误并入②告警区折叠块；只剩「如何申报销项」一块，
    直接渲染、不加段标题）。无清单项时整段省略。<details> 是 CommonMark type-6 HTML block——
    summary/body/</details> 之间不得有空行（#168 回归）；行内 code 用 <code> 标签（HTML block
    不解析 markdown）。"""
    if not has_checklist_items:
        return ""
    body = ("发评论，内容为 <code>touchstone-ack</code> 代码块，每行 "
            "<code>&lt;签名&gt;: done|waived: 理由|split: 链接</code>。"
            "勾选/申报是输入信号，以评审方按达成判据复核后的本清单为准。<br>\n")
    body += _ack_skill_ref()                    # 恒出现（受评仓无 skills/ 也提醒）；
                                               # 只留链接不贴正文（#192 后用户拍板）
    return f"<details><summary>如何申报销项</summary>\n{body}\n</details>"


def render_report(risk, findings, alerts="", scope_facts=None, checklist=None,
                  rounds_left=None, loop_info=None,
                  markers="", gate_line="",
                  review_reliable=True, engine_status="ok", ai_raw_count=0, added_lines=0,
                  engine_detail=""):
    """v3 六段版面（模板唯一定义，代码只填充；v2 版面去冗余、v3 再瘦身）：
      ① 标题 + 状态行（循环 + 风险合一，观测意见 1+6）
      ② 告警（降级/CAUTION/溯源/同源提示，blockquote；降级原始错误以折叠块附于其后）
      ③ 静态检查（敏感路径/门禁，简单 PR 整段省略——观测意见 7）
      ④ 评审发现与销项（AI 评审 + 清单合一 - [ ] task list——观测意见 2+3+4）
      ⑤ 申报指引（如何申报销项 <details>；v3：参考信息壳与验证/日志折叠移除）
      ⑥ 机器 marker
    alerts 取代旧 banner 参数（不再含循环行——循环行归①状态行）。"""
    status = render_status_line(risk, loop_info, checklist, rounds_left, review_reliable)
    # ② 告警：不可信时 [!CAUTION] 置顶替代降级横幅；其余告警（det/llm/unverified/telemetry/
    # 溯源/同源提示）作为 blockquote 追加。可信时直接逐行包 blockquote。
    if not review_reliable:
        kept = []
        if alerts:
            for ln in alerts.split("\n"):
                if not ln.strip():
                    continue
                kept.append(ln)
        alerts_md = render_unreliable_callout(engine_status, ai_raw_count, added_lines, engine_detail)
        if kept:
            alerts_md += "\n\n" + "\n".join(("> " + ln if not ln.startswith(">") else ln) for ln in kept)
    elif alerts:
        alerts_md = "\n".join(("> " + ln if ln.strip() else ">") for ln in alerts.split("\n"))
    else:
        alerts_md = ""
    # 降级原始错误折叠块（v3：原「验证与日志」段的诊断部分并入②告警区，默认折叠不占屏）。
    # 不进 blockquote——它是 CAUTION 的附件而非告警本身（引用块内嵌 <details> 渲染易碎）。
    _ed_fold = _engine_detail_fold(engine_status, engine_detail)
    if _ed_fold:
        alerts_md = f"{alerts_md}\n\n{_ed_fold}" if alerts_md else _ed_fold
    has_items = bool((checklist or {}).get("items"))
    parts = {
        "status": status,
        "alerts": alerts_md,
        "facts": render_facts_v2(scope_facts, gate_line) if scope_facts else "",
        "findings": render_findings_checklist(findings, checklist, review_reliable),
        "reference": render_reference(has_items),
        "markers": markers or "",
    }
    out = _fill_template(_load_template(), parts)   # 单遍填充（A2-F1）：不重扫已填入内容，防占位符注入
    # 折叠空段落留下的多余空行；剥掉模板头部注释（HTML 注释会带进评论——只保留 marker 类注释）
    out = re.sub(r"<!-- =+\n.*?=+ -->\n?", "", out, flags=re.S)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def render_summary(risk, findings):
    label = {"high": "高", "mid": "中", "low": "低"}.get(risk["risk_band"], "未定")
    action = {"read+arbitrate": "需人工评审后合入", "read": "建议人工过目",
              "skip": "无需人工介入"}.get(risk["human_action"], "建议人工过目")
    lines = [
        "**Touchstone · ADVISORY**（不拦截合入，与人工审核并行）",
        "",
        f"风险等级：**{label}** — {action}",
    ]
    _blast = {"cross_module_contract": "跨模块契约变更", "security_surface": "涉及安全面"}
    if risk["blast_radius"]:
        lines.append("触发因子：" + "、".join(_blast.get(b, b) for b in risk["blast_radius"]))
    lines.append("")
    if not findings:
        lines.append("本次未发现规则范围内的问题。")
    else:
        lines.append(f"发现 {len(findings)} 条（按置信降序）：")
        for f in findings:
            lines.append(
                f"- `{f['rule_id']}` [{f.get('severity','')}] "
                f"conf={f['confidence']:.2f} · {f['agent']} · "
                f"`{f.get('file','?')}:{f.get('line','?')}`\n"
                f"  - {f.get('rationale','')}\n"
                f"  - 建议：{f.get('suggested_fix','')}"
            )
    return "\n".join(lines)

