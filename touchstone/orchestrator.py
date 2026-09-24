#!/usr/bin/env python3
# ============================================================================
# touchstone/orchestrator.py  ——  Touchstone 主编排（评审复用 PR-Agent）
# ----------------------------------------------------------------------------
# 形态：advisory。只产出评分与发现、回贴到 PR，**绝不拦截合入**，与人工审核并行。
# 链路：load_standards/contract → get_pr_diff → review_provider.fetch(PR-Agent) → normalize
#        → map_verdict(按 category 定风险等级，不做共识) + contract_check(确定性契约一致性)
#        → 回贴(摘要 + 尽力内联 + 中性 check run) → 写 touchstone-findings.json
# 评审引擎复用开源 PR-Agent（见 docs/touchstone-on-pr-agent.html）；自研委员会已退役。
# touchstone 不再直接调 LLM——PR-Agent 自带端点配置；touchstone 只做归一/裁决/门禁/回贴。
# 依赖：GitHub 走 requests(ghclient)、diff 解析用 unidiff、配置 pyyaml。
#   GITHUB_API_URL  缺省 https://api.github.com；用 GitHub Enterprise 在此改。
# ============================================================================

import html
from urllib.parse import quote          # 标签名 URL 编码（含 ":" 的名字进路径段）
import json
import os
import time
import re
import sys
import traceback

import requests

import yaml

from touchstone import ghclient             # GitHub HTTP 客户端(requests)
from touchstone.envutil import env_pr_event  # PR 类事件判定（pull_request_target 族，pr-agent 第三轮评审）
from touchstone import checks                # 可插拔检查框架 + 总闸
from touchstone import loop                  # 反馈循环控制器
from touchstone import contract_check        # 提交契约一致性核对（确定性）
from touchstone import review_provider       # 评审提供器(复用 PR-Agent) + 发现归一 + 裁决映射
from touchstone import autonomy              # 变更分类计算（供自治经验层/auto_merge 重建）
from touchstone import stack_rules           # §4.1 栈专项确定性规则（machine_checkable 的 SPR/JAVA/CTR）
from touchstone import checklist as checklist_mod   # 收敛清单（修订设计 §4.3，评审意见 1、3）
from touchstone import lineage               # 轮次台账与同源检测（修订设计 §4.4，评审意见 10）
from touchstone.atomicio import atomic_write_json   # 状态文件原子写（决策输入不留半文件）
from touchstone.artifacts import artifact_path      # 统一产物路径（默认 CWD，可经 OUTPUT_DIR 隔离）
# 渲染层已拆至 touchstone/render.py（v2 六段版面填充；模块职责单一化）。此处再导出以保持
# 既有引用路径 orchestrator.render_* 兼容（测试与外部调用无需改动）。
# v2：render_facts/render_findings（v1 七段版面的两段渲染函数）已随版面合并移除——态势区→
# render_status_line、AI 评审+清单→render_findings_checklist、静态检查→render_facts_v2。
# 再导出仅保留仍存在的 render_report / render_summary。
from touchstone.render import (_load_template,  # noqa: F401
                               render_report, render_summary,
                               sanitize_report_body)   # L2 文档级闸门（post_results POST 前）

# --- 配置 ---------------------------------------------------------------------
STANDARDS_PATH = os.environ.get("TOUCHSTONE_STANDARDS", ".touchstone/standards.yaml")
CONTRACT_PATH  = os.environ.get("TOUCHSTONE_CONTRACT",  ".touchstone/pr.yaml")

# --- GitHub API（stdlib） -----------------------------------------------------
def gh(method, path, token, data=None, accept="application/vnd.github+json"):
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    return ghclient.request(method, base + path, token, data=data, accept=accept)


# --- 输入加载 -----------------------------------------------------------------
def load_yaml(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _api_base():
    """统一的 GitCode/GitHub API base URL（get_pr_diff 与 escalate label 共用同一优先级）。"""
    return (os.environ.get("TOUCHSTONE_GITHUB_BASE_URL")
            or os.environ.get("GITHUB_API_URL", "https://api.github.com")).rstrip("/")


def _is_gitcode():
    """检测 API 端点是否 GitCode（Gitea 兼容）。优先看显式 TOUCHSTONE_PLATFORM=gitcode；
    否则子串匹配 GITHUB_API_URL 或 TOUCHSTONE_GITHUB_BASE_URL 含 "gitcode"。
    GitHub/GHE 默认不受影响。"""
    if os.environ.get("TOUCHSTONE_PLATFORM", "").lower() == "gitcode":
        return True
    _api = "gitcode" in os.environ.get("GITHUB_API_URL", "").lower()
    _base = "gitcode" in os.environ.get("TOUCHSTONE_GITHUB_BASE_URL", "").lower()
    return _api or _base


def _gitcode_files_to_diff(files):
    """GitCode /pulls/{n}/files 的 JSON 数组 → unified diff 文本（供 unidiff PatchSet 解析）。
    字段层级以 2026-09-24 探针 PR 实测为准（westmoreland/touchstone，add/modify/delete 全覆盖）：
    - 文件级仅 filename/status/additions/deletions/sha/blob_url/raw_url；
    - patch 是嵌套 dict：diff/old_path/new_path/a_mode/b_mode/new_file/deleted_file/
      renamed_file 全在 patch 【内】（GitLab 系 diff 结构）。旧实现按【文件级】读
      old_path/new_path/new_file——恒为 None → 每个文件都被当新文件拼 `--- /dev/null`、
      删除文件漏 `+++ /dev/null`（unidiff 的 is_removed_file 失真，与 GitHub 路径行为漂移）。
    - status 词汇：新增='added'、删除='deleted'（GitLab 风格）、修改=【键缺失】。
    GitHub 的 patch（纯字符串）路径不受影响（p 空时回落文件级字段）。
    已知信息损失（与 GitHub Accept:diff 路径的差异，调用方须知）：
    - 二进制文件 / 纯重命名（无 patch 字段）不入 diff → 不进 changed_files（GitHub 路径
      会以 "Binary files differ" / rename 头出现，但同样无 hunks 可扫）。
    - 必须配合分页取全量（见 get_pr_diff），否则确定性核对漏文件。"""
    parts = []
    for f in files or []:
        patch = f.get("patch")
        p = patch if isinstance(patch, dict) else {}
        new_fn = (p.get("new_path") or f.get("new_path") or f.get("filename")
                  or p.get("old_path") or f.get("old_path"))
        old_fn = p.get("old_path") or f.get("old_path") or new_fn
        hunks = patch.get("diff") if isinstance(patch, dict) else patch
        # 新/删判定：patch 内布尔优先；a_mode/b_mode=="0" 是同信息的模式位（实测：
        # 新文件 a_mode="0"、删文件 b_mode="0"），布尔缺失时兜底；文件级字段留作兼容。
        new_file = bool(p.get("new_file") or f.get("new_file")
                        or str(p.get("a_mode", "")) == "0")
        deleted_file = bool(p.get("deleted_file") or f.get("deleted_file")
                            or str(p.get("b_mode", "")) == "0")
        if not new_fn or not hunks:
            if new_fn and old_fn and old_fn != new_fn:
                parts.append(f"diff --git a/{old_fn} b/{new_fn}")
                parts.append(f"--- a/{old_fn}")
                parts.append(f"+++ b/{new_fn}")
                parts.append("")
            else:
                print(f"[warn] GitCode files: 跳过无 patch 的文件 {new_fn or '?'}（二进制/重命名/无 hunks）", file=sys.stderr)
            continue
        parts.append(f"diff --git a/{old_fn} b/{new_fn}")
        if new_file:
            # 对齐真实 git diff 形态（unidiff 解析 /dev/null 目标侧时要求模式行在场，
            # 否则 UnidiffParseError "Target without source"→parse_diff 整体失败=确定性核对全失效）
            _b_mode = p.get("b_mode") if str(p.get("b_mode", "")) != "0" else None
            parts.append(f"new file mode {_b_mode or '100644'}")
        elif deleted_file:
            _a_mode = p.get("a_mode") if str(p.get("a_mode", "")) != "0" else None
            parts.append(f"deleted file mode {_a_mode or '100644'}")
        if new_file:
            parts.append("--- /dev/null")
        else:
            parts.append(f"--- a/{old_fn}")
        if deleted_file or not new_fn:
            parts.append("+++ /dev/null")
        else:
            parts.append(f"+++ b/{new_fn}")
        parts.append(hunks if hunks.endswith("\n") else hunks + "\n")
    return "\n".join(parts)


def get_pr_diff(owner, repo, number, token):
    """取 PR 全文 diff——确定性核对（SEC-001 等）必须覆盖全文，安全保证不随体量打折扣。
    LLM 侧的上下文限制由 pr-agent 自己管理（它取全文 PR + 用 custom_model_max_tokens 做
    max_tokens）；touchstone 的确定性核对（密钥扫描/契约/栈规则）是纯正则/AST，不进 LLM，
    不受 diff 体量影响。超大体量 PR 默认走 SIZE-001 体量门禁拆分（TOUCHSTONE_MAX_DIFF_LINES
    默认 3000 行；设 0 关闭、或调高/调低阈值）。
    GitCode 适配：GitCode/Gitea 不支持 Accept: application/vnd.github.v3.diff（400），
    改走 /pulls/{n}/files 取每文件 patch 拼 unified diff；GitHub/GHE 路径不变。
    files 端点是分页的——必须 paginate 取全量（per_page=100 × 30 页 = 3000 文件，
    对齐 GitHub 自身 diff 上限），否则超过一页的文件会静默漏出确定性核对。"""
    if _is_gitcode():
        _api_base_url = _api_base()
        url = _api_base_url + f"/repos/{owner}/{repo}/pulls/{number}/files"
        files = ghclient.paginate(url, token, per_page=100, max_pages=30)
        if not isinstance(files, list):
            print(f"[warn] GitCode /pulls/{number}/files 返回非预期类型 {type(files).__name__}，"
                  f"确定性核对无 diff 输入", file=sys.stderr)
            return ""
        if len(files) >= 3000:
            print(f"[warn] GitCode /pulls/{number}/files 返回 {len(files)} 文件，可能因分页截断"
                  f"（max 3000），确定性核对可能漏文件——降级为空 diff 防止部分 diff 被误当完整"
                  f"（3000 文件 PR 极罕见，GitHub 自身 diff 也有上限；若需精确检测请探测第 31 页）", file=sys.stderr)
            return ""
        if not files:
            print(f"[warn] GitCode /pulls/{number}/files 返回空列表——可能是 fetch 失败"
                  f"（auth/404/限流）或确实无文件变更", file=sys.stderr)
            return ""
        return _gitcode_files_to_diff(files)
    return gh("GET", f"/repos/{owner}/{repo}/pulls/{number}", token,
              accept="application/vnd.github.v3.diff")


def sync_touchstone_config(owner, repo, number, token, repo_dir="."):
    """Sync .touchstone/ config from base branch if missing.

    Fork PRs may not include .touchstone/ config that exists on the target
    repo's base branch. This fetches missing yaml files from the base branch
    via the API, ensuring repo-level rules (seeds.yaml, pr.yaml, etc.) always
    apply. Never overwrites files the PR author included.

    2026-09-24 探针实测（GitCode westmoreland/touchstone）：GET /pulls/{n} 的
    base.sha 存在；contents 目录列表返回 [{type:"file", name}]、单文件
    {type:"file", encoding:"base64", content}——GitHub/GitCode 两形态一致，本函数
    对两平台通用。PR 侧删光 .touchstone/ 时会从 base 拉回（门禁规则不可被 PR 内
    删除绕过，见 docstring 上面的场景说明）。
    """
    import base64 as _b64
    ts_dir = os.path.join(repo_dir, ".touchstone")
    if os.path.isdir(ts_dir) and any(
        f.endswith((".yaml", ".yml")) for f in os.listdir(ts_dir)
    ):
        return
    try:
        api = _api_base()
        pr_info = ghclient.request(
            "GET", f"{api}/repos/{owner}/{repo}/pulls/{number}", token)
        base_sha = ((pr_info or {}).get("base") or {}).get("sha")
        if not base_sha:
            return
        items = ghclient.request(
            "GET",
            f"{api}/repos/{owner}/{repo}/contents/.touchstone?ref={base_sha}",
            token)
        if not isinstance(items, list):
            return
        os.makedirs(ts_dir, exist_ok=True)
        synced = 0
        for item in items:
            if item.get("type") != "file":
                continue
            fname = item.get("name", "")
            if not fname.endswith((".yaml", ".yml")):
                continue
            if "/" in fname or "\\" in fname or ".." in fname:
                continue          # contents 列表正常只有裸文件名；带路径段=逃逸企图，跳过
            local_path = os.path.join(ts_dir, fname)
            if os.path.exists(local_path):
                continue
            fdata = ghclient.request(
                "GET",
                f"{api}/repos/{owner}/{repo}/contents/.touchstone/{fname}"
                f"?ref={base_sha}",
                token)
            content_b64 = fdata.get("content") if isinstance(fdata, dict) else None
            if content_b64:
                with open(local_path, "wb") as f:
                    f.write(_b64.b64decode(content_b64))
                synced += 1
        if synced:
            print(f"[touchstone] synced {synced} .touchstone/ config file(s) "
                  f"from base branch", file=sys.stderr)
    except Exception as e:
        print(f"[warn] sync .touchstone/ from base branch failed: {e}",
              file=sys.stderr)


# --- 回贴 ---------------------------------------------------------------------
def anchor_inline(findings, diff):
    """把发现锚到 PR diff 的可评论行(RIGHT 侧新增行)。
    - 行恰在新增行上 → 直接锚。
    - 行不在新增行上(如指向被删代码/上下文外) → 就近锚到同文件最近新增行，注明原行。
    - 该文件无任何新增行(纯删除/重命名) → 不内联(靠摘要覆盖)。
    GitHub 要求内联评论落在 diff 内的可评论行，否则整条 review 被拒。"""
    _, added = contract_check.parse_diff(diff or "")

    def _fm(f):
        return ("<!-- touchstone-finding: "
                + json.dumps({"rule_id": f.get("rule_id"), "agent": f.get("agent")},
                             ensure_ascii=False) + " -->")
    out = []
    for f in findings:
        path, line = f.get("file"), f.get("line")
        if not path or not line:
            continue
        addl = sorted(n for n, _ in added.get(path, []))
        if not addl:                       # 文件无新增行 → 降级，只进摘要
            continue
        if line in addl:
            anchored, note = line, ""
        else:                              # 就近锚定，并注明原始行号
            anchored = min(addl, key=lambda n: abs(n - line))
            note = f"（原指 :{line}）"
        # 审计 #6：内联正文与主评论同一呈现边界——rationale 常引用 diff/LLM 原文，
        # 凭据形子串须同样过 _redact_secrets（此前只脱敏主评论 body）。
        _body = _redact_secrets(f.get('rationale', '')) or ""
        _dir = _redact_secrets(f.get('fix_direction') or f.get('suggested_fix', '') or "")
        out.append({"path": path, "line": anchored, "side": "RIGHT",
                    "body": f"`{f['rule_id']}`{note} {_body}"
                            f"\n方向：{_dir}\n{_fm(f)}"})
    return out


def ci_verdict(owner, repo, head_sha, token):
    """读 head 的 check-runs 总判定，供反馈循环判断 CI/verify 是否红。
    排除 touchstone 自身的 check（neutral·advisory，不参与）。
    返回 True=全绿/中性、False=有失败、None=仍有未完成或无数据（未知不强制 author 继续）。"""
    try:
        # 审计 #8：check-runs 翻页取全（默认单页仅 30 条，矩阵 CI 产 >30 个 check 的大仓
        # 会把 touchstone 自己的 check 截出首屏 → CI 判定基于残缺事实）。
        data = ghclient.paginate_check_runs(
            f"{_api_base()}/repos/{owner}/{repo}/commits/{head_sha}/check-runs", token)
    except requests.exceptions.RequestException:
        return None
    runs = [r for r in (data.get("check_runs") or [])
            if not str(r.get("name", "")).startswith("touchstone")]
    if not runs:
        return None
    if any(r.get("status") != "completed" for r in runs):
        return None                      # 还有未跑完 → 未知
    bad = {"failure", "timed_out", "cancelled", "action_required", "stale"}
    if any(r.get("conclusion") in bad for r in runs):
        return False
    return True


def _run_link():
    """构造本次 workflow run 的链接（Actions 自动注入的 env）。用于在评审评论里指向
    pr-agent-interaction artifact（完整 LLM 交互日志）。非 Actions 环境返回空。"""
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not run_id:
        return ""
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        return ""
    return f"{server}/{repo}/actions/runs/{run_id}"


def _engine_banner(engine_status):
    """评审引擎降级的人可见说明（防静默故障）。贴在评审评论顶部 + check-run 标题里。"""
    if engine_status == "no_engine":
        return ("⚠️ **AI 评审未运行**：PR-Agent 未安装或不可用，本次评审**只含确定性契约与栈规则核对**，"
                "不含 LLM 代码评审。请确认 workflow 安装了 pr-agent（见 README「GitHub 集成」）。")
    if engine_status == "provider_failed":
        return ("⚠️ **AI 评审取 PR 失败**：PR-Agent 已启动但无法获取该 PR（git provider/凭据/网络），"
                "本次**只含确定性核对**。请检查 pr-agent 的 GitHub token（`GITHUB_TOKEN`）与 "
                "`git_provider` 配置。")
    if engine_status == "llm_failed":
        return ("⚠️ **AI 评审的 LLM 调用失败**：PR-Agent 已运行但 LLM 端点未成功响应，本次**只含确定性核对**。"
                "请检查 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 配置与端点可达性。")
    return ""


def _clean_review_trace(engine_status, ai_raw_count, added_lines, n_changed, raw_excerpt=None):
    """0 条发现时的溯源（防静默故障，一行）：让人区分"LLM 真审了没问题"与"pr-agent 没真审/被过滤光"。
    仅在引擎正常（ok）且无降级时输出；降级由 _engine_banner 负责。

    v3 瘦身（用户 2026-09-10：三行横幅 + LLM 原始 review 段快照太罗嗦）——压成一行。
    "真审过"不再贴 raw_excerpt 全段（快照仍落 touchstone-findings.json 供排查）；
    🟢「已端到端运行」标记本身承载防静默信号，可疑空收敛另有 review_reliable→CAUTION 兜底。
    raw_excerpt/n_changed 不再进文本，保留入参以稳签名（调用方与测试按位置传参）。"""
    if engine_status != "ok":
        return ""
    suspicious = added_lines >= 20 and ai_raw_count == 0   # 改动不小却 0 原始建议
    head = "🟢 **AI 评审已端到端运行**（PR-Agent + LLM 已调用）"
    if suspicious:
        return f"{head}：**改动不小却 0 条原始建议——建议人工扫一眼**（LLM 可能未实质产出）。"
    if ai_raw_count == 0:
        return f"{head}：0 条原始建议，改动规模小、合理。"
    return (f"{head}：{ai_raw_count} 条原始建议归一后 0 条进入评审"
            "（确定性契约/栈核对 0 命中）。")


# 凭据脱敏：engine_detail（来自 ReviewEngineDegraded.reason / 过滤后 stderr）在降级场景被原样贴进
# 【公开 PR 评论】。litellm 详错 / verbose 轨迹可能夹带 Authorization 头或 api key（开
# TOUCHSTONE_LITELLM_VERBOSE 尤甚）；review_provider 仅抽取错误正文、不做脱敏。故在【呈现边界】
# 补这层防御——尽力而为，只抹凭据形子串，错误正文/traceback 保留可读（PRA-REVIEW 安全发现，PR #74）。
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.=]+"),          # Authorization: Bearer xxx
    re.compile(r"(?i)\bAuthorization\b['\"\s]*[:=]\s*['\"]?[A-Za-z0-9_\-\.=]+"),
    re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}"),                    # OpenAI / Anthropic 风格 key
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),                # GitHub PAT（ghp_/ghs_/gho_/ghu_/ghr_）
    re.compile(r"(?i)(api[_-]?key|access[_-]?token)\b['\"\s]*[:=]\s*['\"]?[A-Za-z0-9_\-\.=]{16,}"),
)


def _redact_secrets(text):
    """抹掉凭据形子串（Bearer / Authorization / sk- / gh_ / api_key=…）→ ***REDACTED***。纯函数、可测。"""
    out = text or ""
    for pat in _SECRET_PATTERNS:
        out = pat.sub("***REDACTED***", out)
    return out


# _render_engine_detail 已退役（v3：「验证与日志」段移除）——降级原始错误改由
# render._engine_detail_fold 折叠块呈现（并入②告警区）。脱敏仍在 orchestrator 呈现边界
# （post_results 传 render 前先 _redact_secrets，PR #74 纪律不变）；围栏/截断/转义归 render 层。


# --- 历史轮次折叠（视觉降噪；原文与 marker 全保留）----------------------------
# 动机：每轮评审各发一条全量报告评论，9 轮下来整屏重复信息。新评论发出后，把历史轮次
# 评论【就地编辑】为折叠形态：一行摘要 + <details> 包住转义原文——数据不删（GitHub 评论
# 编辑历史仍可查全量），屏幕只剩最新轮展开。
# 顺序铁律：只在新评论 POST 成功后折叠。旧 marker 随正文转义后不可再解析，若新评论没发出
# 去就折叠旧的，PR 上将无任何可解析 marker → 下一轮 round 归零、台账断链。
_COLLAPSED_SENTINEL = "<!-- touchstone-collapsed -->"
_REVIEW_BRAND = "## Touchstone · AI Committer 代码检视"
# marker 提取：非贪婪到字面终止符 -->（行内，不跨行——marker 是 json.dumps 单行产物）。
# 【不得用 [^>]*】（round-1 评审）：result/checklist marker 的 payload 是自由文本 JSON
# （LLM direction/reasoning 可含 "->" / ">="），[^>]* 遇 payload 内首个 '>' 即失配 →
# 整条 marker 匹配失败 → 随正文转义进 <pre> 永久不可解析，恰好毁掉折叠要保护的
# loop 状态/lineage 台账。残余风险（有意接受）：payload 字符串里出现字面 "-->" 会提前
# 截断——json.dumps 不转义 '>'，但 marker 消费方（parse_latest_state 等）本就容忍坏
# JSON 跳过，且发生频率远低于裸 '>'。
_MARKER_RE = re.compile(r"<!-- touchstone-(?:loop|checklist|result):[^\n]*?-->")


def _marker_payload_ok(marker):
    """整条 marker 的 payload 必须是合法 JSON（round-5 评审）：历史裸 dumps 评论的
    marker 含字面 '-->' 时，findall 截在 payload 内首个 '-->' 上——残片外置等于
    固化坏 marker，完整原文随转义进 <pre> 再无人可解析（round 归零风险）。"""
    m = re.match(r"^<!-- touchstone-(?:loop|checklist|result):\s*(.*?)\s*-->$", marker, re.DOTALL)
    if not m:
        return False
    try:
        json.loads(m.group(1))
        return True
    except (ValueError, TypeError):
        return False


def _repair_marker(orig, pos):
    """从原文 pos 起修复损坏 marker：raw_decode 从首个 '{' 取完整 JSON（字符串内的
    '-->' 不干扰——checklist.parse_latest 同技），重新序列化 + html_comment_safe_json
    转义。修复不了（真不是 JSON）返回 None——调用方放弃折叠该评论。

    round-7 销项（PRA-POSSIBLE_ISSUE）：'{' 的搜索必须限定在 marker 名前缀紧邻之后——
    此前无界 orig.find("{", pos) 在坏 marker 载荷没有 '{' 时会一路落到【后续】真 marker
    或正文的 '{' 上，raw_decode 成功即产出张冠李戴的"修复"（loop 名挂着 checklist 载荷）。
    前缀后跳过空白若非 '{'，直接判不可修复。"""
    name = re.match(r"<!-- touchstone-(loop|checklist|result):", orig[pos:])
    if not name:
        return None
    i = pos + name.end()                     # 名前缀（含冒号）之后
    while i < len(orig) and orig[i] in " \t":
        i += 1
    if i >= len(orig) or orig[i] != "{":
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(orig[i:])
    except ValueError:
        return None
    return (f"<!-- touchstone-{name.group(1)}: "
            + checklist_mod.html_comment_safe_json(obj) + " -->")


def _tail_marker_start(orig, matches):
    """尾部区（真 marker 的信任域）起点判定：真 marker 统一追加在报告最后（⑥ 段，
    位于最后一个 </details> 之后）。round-7 销项（PRA-GENERAL）：

    - 正文含 </details> → 尾部区 = 最后一个 </details> 之后——正文中引用/示例的假
      marker（LLM 转述 marker 语法）几乎都在 依据/reference 的 details 体内，天然
      落在信任域外；
    - 无 </details> 的退化正文（极简夹具/异常输入）→ 尾部区无从锚定，退回信任全域：
      宁可保守外置（round-6 的同类去重保最后仍是防线），不误杀真 marker——尤其别
      把「历史裸 dumps 损坏 marker」的修复路径堵死（损坏 marker 的正则残片截在载荷
      中段，任何「从末尾回扫连续 marker 段」的口径都会把它判到信任域外）。

    同类去重保最后（round-6）只在假 marker 之后还有同类真 marker 时兜得住；引用了
    本评论从未真实发射过的 kind（如只有 checklist 的评论里引用 loop round=99）时假货
    是同类唯一出现，keep-last 会原样外置污染 parse_latest_state——信任域把这类假货
    整类挡在门外（不外化 ≠ 丢失：原文仍在折叠 <pre> 里）。"""
    k = orig.rfind("</details>")
    if k != -1:
        return k + len("</details>")
    return 0


def _collapse_status_line(orig):
    """折叠摘要取状态行：品牌 H2 之后、**头部区内**的第一条 blockquote（v2 版面结构：
    ①标题+状态行 ②告警——告警也是 "> " 开头，如降级轮的 [!CAUTION]；头部区止于首个
    小节标题 ###）。取「全文第一条 > 」在模板演化或正文含前置引用时会拿错（round-1）；
    H2 后无界扫描会在头部缺状态行（旧模板/降级轮）时抓到正文深处的引用片段冒充摘要
    （round-4）——界到 ###，头部没有就用占位，绝不深挖正文。"""
    lines = orig.split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith(_REVIEW_BRAND):
            for l2 in lines[i + 1:]:
                if l2.startswith("###"):       # 头部区止于首个小节标题
                    break
                if l2.startswith("> "):
                    return l2
            return "> Touchstone 历史轮次"     # 头部无状态行 → 占位（不冒充、不深挖）
    # 无品牌 H2（异常输入）也直接占位（round-6）：受信评审评论必有品牌 H2
    # （_stale_review_comments 以其过滤），无 H2 = 模板已面目全非，头部区无从锚定，
    # 全文扫首条 '>' 是 round-4 刚消灭的无界扫描借尸还魂——占位不冒充。
    return "> Touchstone 历史轮次"


def _collapse_review_body(orig):
    """单条历史评审评论 → 折叠体。

    - 状态行（品牌 H2 后首条 blockquote）提到折叠体外做摘要——折叠后仍一眼可见每轮结论；
    - 原文逐行 html.escape 进 <pre>（#168 HTML block 约束：details 内不得有空行——空行
      以 &nbsp; 占位行保留，段落分隔在 <pre> 里仍可见，不再静默塌掉（round-8 销项）；
      尖括号防吞；<pre> 保留换行）；
    - 尾部区机器 marker（loop/checklist/result 的 HTML 注释）从原文抽出、**原样**附在
      折叠体末尾——HTML 注释不渲染（视觉零成本），但保持可解析：loop 状态派生、lineage
      台账从已关 PR 评论重建轮次预算都依赖它们。归档只剥离这些【已外化】的 marker；
      正文引用的 marker（信任域外、不外置）留在 <pre> 原位转义——「原文不删、只折叠」
      对正文片段同样成立（round-8 销项：此前 _MARKER_RE.sub 全删，归档静默缺内容）。
    """
    status = _collapse_status_line(orig)
    matches = list(_MARKER_RE.finditer(orig))
    # 只外化【尾部区】marker（round-7 销项 PRA-GENERAL，信任域判定见 _tail_marker_start）：
    # 正文引用的假 marker 不外化（原文仍在折叠 <pre> 里可见），kind 仅正文出现则整类消失
    # ——防引用伪造污染 parse_latest_state/lineage。尾部区的损坏 marker（历史裸 dumps
    # 截断）才走修复；修不好（真不是 JSON）放弃折叠该评论。
    tail_start = _tail_marker_start(orig, matches)
    markers = []
    for m in matches:
        if m.start() < tail_start:
            continue                             # 正文引用：不外化
        frag = m.group(0)
        if _marker_payload_ok(frag):
            markers.append(frag)
            continue
        rep = _repair_marker(orig, m.start())   # 损坏（历史裸 dumps 截断）→ 从原文
        if rep is None:                         # 完整区间修复；修不好才放弃折叠
            return None
        markers.append(rep)
    # 同类去重保最后一个（round-6）：尾部区同类多次出现（如损坏残片与其真身）取最后；
    # 定序 loop/checklist/result 保持稳定输出。
    by_kind = {}
    for mk in markers:
        k = re.match(r"<!-- touchstone-(loop|checklist|result):", mk).group(1)
        by_kind[k] = mk
    markers = [by_kind[k] for k in ("loop", "checklist", "result") if k in by_kind]
    # round-8 销项（PRA-REVIEW）：归档只剥离【已外化】的尾部区 marker——正文引用的
    # marker 留在 <pre> 原位（转义后可见）。此前 _MARKER_RE.sub 全删：引用片段从归档
    # 静默消失，「原文与 marker 全保留」的自述对它们不成立。
    keep = [(m.start(), m.end()) for m in matches if m.start() >= tail_start]
    parts, last = [], 0
    for a, b in keep:
        parts.append(orig[last:a])
        last = b
    parts.append(orig[last:])
    stripped = "".join(parts)
    # 空行 → &nbsp; 占位行：#168 禁空行是渲染约束，但内容版面不该静默丢——占位行非空
    # （HTML block 完整），<pre> 里渲染为一条空行，段落分隔保真。转义在行内做：&nbsp;
    # 必须在 escape 之后注入（否则 & 被 escape 成 &amp;nbsp;）。
    folded = "\n".join("&nbsp;" if not ln.strip() else html.escape(ln)
                       for ln in stripped.strip().split("\n"))
    out = (f"{_COLLAPSED_SENTINEL}\n"
           f"🔁 历史评审已折叠（最新轮次见最新评论）· {status.removeprefix('> ').strip()}\n"
           "<details><summary>展开本轮完整评审原文</summary>\n"
           f"<pre>{folded}</pre>\n"          # folded 已逐行转义（上方），此处不得二次 escape
           "</details>")
    if markers:
        out += "\n" + "\n".join(markers)
    return out


def _stale_review_comments(comments, bot_login):
    """待折叠的历史评论：受信 bot 发的【评审报告】（品牌 H2 识别），且尚未折叠过。"""
    out = []
    for c in comments or []:
        login = ((c.get("user") or {}).get("login"))
        if bot_login:
            if login != bot_login:
                continue
        elif not loop._is_bot_login(login):
            continue
        body = c.get("body", "") or ""
        if _REVIEW_BRAND not in body or _COLLAPSED_SENTINEL in body:
            continue
        if not c.get("id"):
            continue
        out.append(c)
    return out


def _collapse_stale_reviews(owner, repo, token, stale):
    """就地编辑（PATCH）历史评论为折叠体。逐条隔离：单条失败只告警，不阻塞评审主链。

    GitCode 适配：评论编辑走 PATCH /pulls/comments/{id}（与取评论的 /pulls/{n}/comments
    同一 id 命名空间，实测可改）。GitHub 走 PATCH /issues/comments/{id}（JSON）。
    请求体 JSON（官方文档声明 application/json；2026-09-24 探针实测 form 与 JSON 均
    200 且真正落库，取 JSON 与仓内其余 POST/PATCH 统一）。
    折叠是纯视觉功能：失败只告警。"""
    for c in stale:
        folded = _collapse_review_body(c.get("body", "") or "")
        if folded is None:
            print(f"[info] 历史评论含损坏 marker，跳过折叠保持原样(id={c.get('id')})",
                  file=sys.stderr)
            continue
        try:
            if _is_gitcode():
                # 不走 gh()：其 base 只认 GITHUB_API_URL，GitCode 部署可能只设
                # TOUCHSTONE_GITHUB_BASE_URL（与 get_pr_diff/label 同一约定，_api_base()）。
                _edit_url = _api_base() + f"/repos/{owner}/{repo}/pulls/comments/{c['id']}"
                _resp = requests.patch(_edit_url, headers={"Authorization": "Bearer " + token,
                                       "Accept": "application/json",
                                       "Content-Type": "application/json"},
                                       json={"body": folded}, timeout=30)
                _resp.raise_for_status()
            else:
                gh("PATCH", f"/repos/{owner}/{repo}/issues/comments/{c['id']}", token,
                   {"body": folded})
        except requests.exceptions.RequestException as e:
            print(f"[warn] 历史评论折叠失败(id={c.get('id')})，保持原样: {e}", file=sys.stderr)


def _label_names(labels_json):
    """PR JSON 的 labels 字段 → 标签名列表。GitCode 实测（2026-09-24 探针）返回
    dict(name=...) 列表；历史版本出现过纯字符串列表（gitcode-adaptation round-8），
    两种都收——丢掉任何一种形态都会让"标签是否已打上"的核验误报缺失。"""
    out = []
    for l in labels_json or []:
        if isinstance(l, dict):
            n = l.get("name")
            if n:
                out.append(str(n))
        elif isinstance(l, str) and l:
            out.append(l)
    return out


def _post_escalate_label(owner, repo, number, token):
    """打 touchstone:needs-human 标签（GitHub / GitCode 端点与请求体各异）。

    GitCode 适配（2026-09-24 探针实测，westmoreland/touchstone，勿凭"400 但已生效"
    的旧结论回退——那不成立）：
    - 正确调用：POST /pulls/{n}/labels + 【纯 JSON 数组】体 ["label"] → 201，追加语义，
      不覆盖既有标签（escalate 正需要追加，勿用 PUT /pulls/{n}/labels 的替换语义）。
    - POST /issues/{n}/labels 对 PR 不存在：数组体 404 "Issue Not Found"；
      {"labels": [...]} 对象体在两个端点都 400 body parsing error 且标签【并未】加上。
    gh() 对 data 走 json= 序列化，传 list 即发纯数组体，正合 GitCode 要求。"""
    _label = "touchstone:needs-human"
    if _is_gitcode():
        gh("POST", f"/repos/{owner}/{repo}/pulls/{number}/labels", token, [_label])
        # gh() 已 raise_for_status；追加语义再 GET 核验一次，防端点行为漂移把
        # "升级信号已传达"变成静默假阳性（标签形态 str/dict 两种都认，见 _label_names）。
        _pr_chk = gh("GET", f"/repos/{owner}/{repo}/pulls/{number}", token)
        _names = set(_label_names(_pr_chk.get("labels") if isinstance(_pr_chk, dict) else None))
        if _label not in _names:
            raise requests.exceptions.RequestException(
                "POST /pulls/{n}/labels 返回 2xx 但 GET 未见 needs-human 标签"
                "（GitCode 端点行为漂移？）")
        return
    gh("POST", f"/repos/{owner}/{repo}/issues/{number}/labels", token,
       {"labels": [_label]})


_TS_OPEN_LABELS = ("touchstone:open-findings",
                   "touchstone:open-1-3", "touchstone:open-4-10", "touchstone:open-11+")
_TS_BUCKETS = _TS_OPEN_LABELS[1:]    # 三个量级桶（对账清桶用）
_TS_LABEL_COLORS = {
    "touchstone:converged": ("0E8A16", "销项闭环：可机器验证发现已全销（waived/split 仍待人核准）"),
    "touchstone:open-findings": ("D93F0B", "销项进行中：仍有未销项发现（量级见 open-* 桶标签）"),
    "touchstone:open-1-3": ("FBCA04", "未销项发现 1–3 条"),
    "touchstone:open-4-10": ("FBCA04", "未销项发现 4–10 条"),
    "touchstone:open-11+": ("B60205", "未销项发现 11+ 条"),
}


def _ensure_label(owner, repo, token, name):
    """标签存在性保障（幂等 best-effort）：GET 404 → POST 建带色标签。已存在即返。
    失败向上抛由 _set_labels 统一 [warn]（加标签时 GitHub 对缺失名自动建灰色默认标签，
    故此处失败不致命——只是徽章不好看）。"""
    try:
        gh("GET", f"/repos/{owner}/{repo}/labels/{quote(name)}", token)
        return
    except requests.exceptions.RequestException:
        pass    # 静默豁免：GET 404/瞬断只说明"可能缺标签"，落到下方 POST 预建/加标签路径统一处置
    _color, _desc = _TS_LABEL_COLORS.get(name, ("CCCCCC", ""))
    gh("POST", f"/repos/{owner}/{repo}/labels", token,
       {"name": name, "color": _color, "description": _desc})


def _set_labels(owner, repo, number, token, add, remove=()):
    """GitHub PR 标签外科式增删：POST /issues/{n}/labels 增、DELETE 单条删——不用 PUT 整组
    覆盖（竞窗内会抹掉他人并发加的标签）。绝不抛：任何失败只 [warn]（标签是传达渠道，
    评论/check-run 才是契约本体）。GitCode 无此通路（POST labels 400，且列表页标签渲染
    未核实）——调用方负责平台分流，本函数不做 GitCode 适配。"""
    try:
        add = [a for a in add if a]
        remove = [r for r in remove if r]
        if not add and not remove:
            return
        for name in add:
            try:
                _ensure_label(owner, repo, token, name)
            except Exception as e:            # 建标签失败不阻断加标签（灰色默认也是信号）
                print(f"[warn] 标签预建失败（{name}）: {type(e).__name__}: {e}", file=sys.stderr)
        if add:
            gh("POST", f"/repos/{owner}/{repo}/issues/{number}/labels", token, {"labels": add})
        for name in remove:
            try:
                gh("DELETE", f"/repos/{owner}/{repo}/issues/{number}/labels/{quote(name)}", token)
            except Exception as e:            # 删旧失败只留双标签（下轮再清），不致命
                print(f"[warn] 旧状态标签移除失败（{name}）: {type(e).__name__}: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] 标签增删失败（add={add} remove={remove}）: "
              f"{type(e).__name__}: {e}", file=sys.stderr)


def _open_count(checklist):
    """未销项数——列表标签桶与 check-run 标题的**同一口径**（round-2 评审意见：两处
    各自数数迟早语义漂移，标签说 3 条标题说 4 条即互相说谎）。"""
    return sum(1 for i in (checklist or {}).get("items", [])
               if isinstance(i, dict) and i.get("status") not in checklist_mod.RESOLVED)


def _loop_state(decision, checklist):
    """决策 → 循环状态的**单一映射**（round-2 评审：标签与标题两份并行实现迟早漂移
    ——PRA-DRIFT；与 _open_count 统一计数口径同一理由）。返回三元组：

    - kind：converged / escalate / continue / unknown——标签侧选绿/红徽章用；
    - title_state：check-run 标题的状态段；"" = 未知态（loop_info 缺失 / 未来新增的
      决策名）——**显式匹配已知决策、不落 else 兜底**，未知值与 None 一样省略状态段，
      不谎称进行中；
    - bucket：未销项量级桶标签名；None = 不进桶（converged；未闭环但 0 未销项——
      open-1-3 谎称「1–3 条」，反向说谎的徽章比没有更糟）。escalate 也打桶：
      needs-human + 量级 = 「多少人时的活」一眼可见；escalate 标题不带计数是刻意的
      ——状态归标题、量级归标签桶。"""
    if decision == "converged":
        return "converged", "✅ 已闭环", None
    if decision not in ("escalate", "continue"):
        return "unknown", "", None
    n = _open_count(checklist)
    bucket = ("touchstone:open-1-3" if 1 <= n <= 3 else
              "touchstone:open-4-10" if 4 <= n <= 10 else
              "touchstone:open-11+" if n >= 11 else None)
    if decision == "escalate":
        return "escalate", "⬆️ 已升级到人", bucket
    # continue 且 0 未销项（清单全销、CI/verify 待绿）：报「未闭环」不带计数——
    # 「未销项 0 项」与红 open-findings 徽章是自相矛盾信号。
    return "continue", (f"🔁 未销项 {n} 项" if n else "🔁 未闭环"), bucket


def _sync_state_labels(owner, repo, number, token, decision, checklist):
    """PR 列表页零点击可见性（用户诉求：不想每个 PR 点进去拖到底才知道销项状态）：
    converged → touchstone:converged（绿徽章）；未闭环 → touchstone:open-findings +
    未销项量级桶（1–3 / 4–10 / 11+，桶选法与标题状态同走 _loop_state 单一映射）。
    每轮全量对账（先清旧状态/桶标签再打新）——桶随轮次变化，残留即说谎。未知决策值
    不谎称也不冒充——保持上轮标签 + [info] 留痕。GitCode 平台跳过（标签通路与渲染均
    未核实，同 #219 折叠守卫模式）：[info] 留痕，不做半吊子适配。escalate 的
    needs-human 由既有块负责（含其自有 GitCode 兜底），本函数不碰。"""
    if _is_gitcode():
        print("[info] GitCode 平台暂不同步销项状态标签（标签通路未核实，GitHub 生效）",
              file=sys.stderr)
        return
    kind, _state, bucket = _loop_state(decision, checklist)
    if kind == "unknown":
        print(f"[info] 未知循环决策 {decision!r}，跳过本轮销项状态标签同步（保持上轮）",
              file=sys.stderr)
        return
    if kind == "converged":
        _set_labels(owner, repo, number, token, add=["touchstone:converged"],
                    remove=_TS_OPEN_LABELS)
        return
    _set_labels(owner, repo, number, token,
                add=["touchstone:open-findings"] + ([bucket] if bucket else []),
                remove=("touchstone:converged",) + tuple(
                    b for b in _TS_BUCKETS if b != bucket))


def post_results(owner, repo, number, head_sha, token, risk, findings, loop_info=None,
                 change_class=None, diff=None, injected_types=None, injected_experience_ids=None,
                 shadow_types=None, shadow_experience_ids=None,
                 engine_status="ok", det_warning="", ai_raw_count=0, added_lines=0, n_changed=0,
                 scope_facts=None, checklist=None, rounds_left=None, ledger=None,
                 review_reliable=True,
                 llm_notes=None, raw_excerpt=None, unverified_claims=0, telemetry_status="disabled",
                 engine_detail="", stale_comments=None):
    # (1) 摘要评论——本轮评审的契约载体；发布失败=大声失败（issue #211：无评论+绿灯
    #     =评审结果静默丢失，机器销项循环永远等不到评论）。按 v2 六段版面模板组装：
    #     ①标题+状态行 ②告警 ③静态检查 ④评审发现与销项 ⑤参考信息 ⑥机器 marker
    # 评审不可信时，降级说明/0-发现溯源统一并入 render 层的 [!CAUTION] 置顶告警
    # （见 render.render_unreliable_callout；判定层的 review_reliable 信号在此接到呈现层）；
    # 可信时保持原逻辑。det_warning（确定性侧警告）与可信度无关，两种情形都保留。
    # v2：循环状态行不再并入 alerts——归 render_status_line（① 状态行），alerts 只载告警。
    alerts = "" if not review_reliable else _engine_banner(engine_status)
    if det_warning:
        alerts = (alerts + "\n\n" if alerts else "") + f"⚠️ **{det_warning}**"
    if review_reliable and not alerts and not findings:
        # 引擎正常且可信的 0 发现：附溯源，让人区分"LLM 真审了没问题"与"没真审"
        alerts = _clean_review_trace(engine_status, ai_raw_count, added_lines, n_changed,
                                     raw_excerpt=raw_excerpt)
    for note in (llm_notes or []):
        alerts = (alerts + "\n\n" if alerts else "") + note
    if unverified_claims:
        # author 自证销项点名——advisory 下提示人核准，autonomy 下已独立拦（no_unverified_claims 闸）
        alerts = (alerts + "\n\n" if alerts else "") + (
            f"🟡 **{unverified_claims} 条 waived/split 系 author 自证、机器未验证**："
            "这些豁免/拆分需人核准，不计入机器可验证收敛，也不触发自动放行。")
    # 遥测上报状态进告警（防静默故障：可观测性子系统自身状态对运维可见，不只进 stderr）。
    # 仅在启用遥测时显示——默认关（disabled）时不加行，免噪声。failed 时附原因，让人能定位。
    if telemetry_status and telemetry_status != "disabled":
        if telemetry_status == "ok":
            _tel_line = "📡 **遥测**：已上报本轮指标"
        else:
            _reason = (telemetry_status[len("failed:"):].strip()
                       if telemetry_status.startswith("failed:") else telemetry_status)
            _tel_line = f"📡 **遥测**：上报失败（不阻塞评审）— {_reason}"
        alerts = (alerts + "\n\n" if alerts else "") + _tel_line
    # 同源提示（轮次台账）：v2 统一到告警段（旧版在静态检查+清单两处重复呈现）。
    if ledger and ledger.get("lineage"):
        _entries = [e for e in ledger.get("lineage", []) if isinstance(e, dict) and "number" in e]
        if _entries:
            hist = "、".join(f"#{e['number']}（{e.get('rounds', '?')} 轮）" for e in _entries)
            _lin = (f"⚠️ 与已关闭的 {hist} 内容同源：历史已消耗 {ledger.get('rounds_spent', 0)} 轮，"
                    f"未销项 {len(ledger.get('inherited_open_items', []))} 条已并入本清单，"
                    f"剩余轮次按台账计。人工重置请打 `rounds-reset` label。")
            alerts = (alerts + "\n\n" if alerts else "") + _lin
    # checklist 兜底（影响所有平台，run.py 独立入口受益）：run.py 调 post_results 不传
    # checklist → 此前 checklist=None 时 render_findings_checklist 整段省略，评论只有
    # 标题+状态行、findings 不进清单。仅 checklist=None 且有 findings 时从 findings 现
    # 建清单；CI（orchestrator main）恒传 cur_cl，不触发。
    if not checklist and findings:
        checklist = checklist_mod.from_findings(findings)
    # 机器 marker 段：loop 状态 marker + checklist 权威状态 marker（机读，永远存在）。
    markers = []
    if loop_info:
        markers.append(loop_info[2])           # loop state marker（render_status_line 用 loop_info[0/1]）
    if checklist:
        markers.append(checklist_mod.render_marker(checklist))
    # v3：「验证与日志」段移除——健康轮它只承载一行运行链接（check-run 页可达，无需评论内贴）；
    # 降级轮的原始错误并入②告警区折叠块（render._engine_detail_fold）。engine_detail 在呈现
    # 边界先脱敏（凭据形子串不出公开评论，PR #74 纪律不变），再交 render 层转义/截断/折叠。
    _ed_safe = _redact_secrets(engine_detail) if engine_detail else ""
    body = render_report(risk, findings, alerts=alerts, scope_facts=scope_facts,
                         checklist=checklist, rounds_left=rounds_left, loop_info=loop_info,
                         markers="\n".join(markers), gate_line="",
                         review_reliable=review_reliable, engine_status=engine_status,
                         ai_raw_count=ai_raw_count, added_lines=added_lines, engine_detail=_ed_safe)
    # 机读 result marker（隐藏）——校准/自治经验从 API 重建数据的入口
    result_marker = "<!-- touchstone-result: " + checklist_mod.html_comment_safe_json({
        "risk_band": risk["risk_band"],
        "verification_decision": risk["verification_decision"],
        "change_class": change_class,
        "loop_decision": (loop_info[0] if loop_info else None),
        "injected_types": injected_types,          # 本轮注入的经验类型（供 shadow A/B 分臂采集）
        "injected_experience_ids": injected_experience_ids,   # 本轮注入的经验【id】（单条归因/回退，见数据采集设计 取舍2）
        "shadow_types": shadow_types or [],              # 本轮 shadow 注入的 candidate 类型（破冷启动死锁，with 臂归因；SHADOW_INJECTION 开时非空；None→[] 稳定 list 类型）
        "shadow_experience_ids": shadow_experience_ids or [],   # 本轮 shadow 注入的 candidate【id】（单条归因/回退；None→[]）
        "findings": [{"rule_id": f.get("rule_id"), "agent": f.get("agent"),
                      "severity": f.get("severity")} for f in findings],
        "unverified_claims": unverified_claims,
    }) + " -->"
    body = body + "\n\n" + result_marker
    # L2 文档级闸门（POST 前最后一道）：不变量=每个 `<!--` 都是自产 marker 且闭合、可见区
    # 无孤儿 `-->`。L1（各嵌入点 _html_text）漏转义时在此兜底自愈——坏文档不出门。
    # 实录：#191 round-7 LLM 依据引用 marker 语法的字面 `<!--` 吞掉整段参考信息（#197 L1
    # 修复）；本闸门保证同类洞（未来新增嵌入点/新文本源）不再进 UI。修复打 stderr 留痕
    # （run log 可查），正文不再静默带病出门。
    body, _n_fixes = sanitize_report_body(body)
    if _n_fixes:
        print(f"[review_pr] 报告正文 HTML 注释配对违例，已中性化 {_n_fixes} 处（L1 转义漏点，"
              f"详见 render.sanitize_report_body）", file=sys.stderr)
    posted = False
    _post_err = None
    try:
        # GitCode 适配：PR 评论端点是 /pulls/{n}/comments（GitHub 是 /issues/{n}/comments）。
        _cmt = (f"/repos/{owner}/{repo}/pulls/{number}/comments" if _is_gitcode()
                else f"/repos/{owner}/{repo}/issues/{number}/comments")
        gh("POST", _cmt, token, {"body": body})
        posted = True
    except requests.exceptions.RequestException as e:
        # issue #211：摘要评论发不出去时若继续走完主流程 = 「绿灯 job + 无评论」——评审
        # 结果静默丢失。此处先 stderr 留痕（[error] 而非 [warn]：这不是可忽略的旁路），
        # 内联评论/check-run 仍尽力而为（有限诊断面），函数末尾统一转大声失败。
        _post_err = e
        print(f"[error] 摘要评论失败: {e}", file=sys.stderr)
    # 新评论已落地 → 历史轮次评论折叠（视觉降噪，原文与 marker 全保留）。仅在 posted 后做：
    # 若新评论没发出去就折叠旧的，旧 marker（转义后不再可解析）会丢状态——round 归零。
    if posted and stale_comments is not None:
        _collapse_stale_reviews(owner, repo, token, stale_comments)
    # (2) 尽力内联评论（event=COMMENT，绝不 REQUEST_CHANGES）
    #     锚定到 diff 可评论行（删除行/超界行就近锚或降级）；每条附自识别隐藏标记
    if diff is not None:
        inline = anchor_inline(findings, diff)
    else:
        def _finding_marker(f):
            return ("<!-- touchstone-finding: "
                    + json.dumps({"rule_id": f.get("rule_id"), "agent": f.get("agent")},
                                 ensure_ascii=False) + " -->")
        inline = [{"path": f["file"], "line": f["line"], "side": "RIGHT",
                   "body": f"`{f['rule_id']}` {_redact_secrets(f.get('rationale',''))}"
                           f"\n方向：{_redact_secrets(f.get('fix_direction') or f.get('suggested_fix',''))}"
                           f"\n{_finding_marker(f)}"}
                  for f in findings if f.get("file") and f.get("line")]
    # GitCode 适配：无 /pulls/{n}/reviews 与 /check-runs 端点（404）——内联与 check run
    # 双双跳过（内联信息由摘要评论覆盖；中性 check run 本就是 advisory，缺失不影响裁决）。
    if inline and not _is_gitcode():
        try:
            gh("POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews", token,
               {"event": "COMMENT", "comments": inline})
        except requests.exceptions.RequestException as e:
            print(f"[info] 内联评论降级(行不在 diff 内属正常): {e}", file=sys.stderr)
    # (3) 中性 check run（advisory，永不 failure）。标题带销项状态：PR 列表页悬停
    #     checks 图标即可见，不必点进 PR 拖到底（与 _sync_state_labels 标签互补：
    #     标签零点击常驻、标题带精确未销项数）。
    if head_sha and not _is_gitcode():
        flag = "⚠️ 评审降级 · " if (engine_status != "ok" or det_warning) else ""
        _dec = loop_info[0] if loop_info else None
        # 状态段与标签桶同走 _loop_state 单一映射（round-2 评审：两份并行实现迟早
        # 漂移；未知决策值与 None 同归未知态、省略状态段，不落 else 谎称进行中）。
        _kind, _state, _bucket = _loop_state(_dec, checklist)
        _suffix = f" · {_state}" if _state else ""
        try:
            gh("POST", f"/repos/{owner}/{repo}/check-runs", token, {
                "name": "touchstone", "head_sha": head_sha, "status": "completed",
                "conclusion": "neutral",
                "output": {"title": f"{flag}风险等级 {risk['risk_band']} · {len(findings)} 条发现{_suffix}",
                           "summary": body[:600]},
            })
        except requests.exceptions.RequestException as e:
            print(f"[info] check run 跳过: {e}", file=sys.stderr)
    # issue #211 契约收口：摘要评论没落地 → 本轮评审结果无法送达 PR（机器 marker/销项
    # 循环都读它）。宁可 job 红（触发重跑/人工介入），绝不静默成功。gh() 对 POST 有意
    # 不重试（防重复评论），单发失败即此路径——secondary rate limit / 权限收紧是常见
    # 触发。raise 穿透 main() → 顶层看门狗（_run_with_watchdog）兜 exit 1。
    if not posted:
        raise RuntimeError(
            f"摘要评论未发布——评审结果无法送达 PR（issue #211：无评论+绿灯=静默丢评审，"
            f"改判大声失败；run 日志与产物仍有本轮数据，re-run 可重试）。原始异常：{_post_err!r}")


# --- main ---------------------------------------------------------------------
def _stack_from_diff(diff):
    """审计 #51：diff → 技术栈粗判（供 seeds.yaml 按栈过滤）。与 ground_truth._stack_of
    同口径（.java→java / .py→python / .go→go / ts|tsx|js|jsx→typescript，不确定→""=通用），
    复用同一实现避免两套判定漂移。"""
    from touchstone.ground_truth import _stack_of
    try:
        changed, _added = contract_check.parse_diff(diff or "")
        return _stack_of(list(changed.keys()))
    except Exception:
        return ""


def _collect_injection():
    """取本轮要写入 result marker 的经验注入类型：active（生产路径）+ shadow（实验路径，env 开时）。
    与 review_provider._experience_injection 同源（只读经验库、失败即空）。
    active 是生产路径、shadow 是实验路径——shadow 取值用【独立内层】try/except 隔离：shadow 抛异常
    只丢弃 shadow、不 wipe 已成功取到的 active（pr-agent review #117 指出的失败隔离点）。四者皆
    失败即空（marker 写空）。shadow 仅 TOUCHSTONE_SHADOW_INJECTION 开时才取（默认关=字节级不变，
    需 step4 review_provider include_shadow 透传后才不归因失真——见 _shadow_injection_enabled）。"""
    injected_types, injected_experience_ids = [], []
    shadow_types, shadow_experience_ids = [], []
    # 审计 #44：与 review_provider._experience_injection 同门控（同源同闸）——
    # ① TOUCHSTONE_EXPERIENCE_ENABLED=false：评审侧整体不注入，marker 也不得记 active，
    #   否则归因数据声称注入了从未注入的经验，A/B 采纳率分臂失真；
    # ② PR 事件未配 TOUCHSTONE_EXPERIENCE_REF：引擎库被防投毒闸整段跳过（load_store
    #   会读到 PR 可篡改的工作树），marker 同样必须空。
    if os.environ.get("TOUCHSTONE_EXPERIENCE_ENABLED", "true").lower() not in ("1", "true", "yes", "on"):
        return [], [], [], []
    # pr-agent 评审（第三轮）：事件匹配改前缀族——本仓 workflow 触发器是 pull_request_target，
    # 精确 == "pull_request" 时闸②在生产路径上永不生效（pull_request_target ≠ pull_request）。
    if env_pr_event() and not os.environ.get("TOUCHSTONE_EXPERIENCE_REF"):
        return [], [], [], []
    try:
        from touchstone import learning_loop as _ll
        _store = _ll.load_store()
        injected_types = _ll.active_types(_store)
        injected_experience_ids = _ll.active_ids(_store)
        if _ll._shadow_injection_enabled():
            try:
                shadow_types = _ll.shadow_types(_store)
                shadow_experience_ids = _ll.shadow_ids(_store)
            except Exception:
                shadow_types, shadow_experience_ids = [], []
    except Exception:
        injected_types, injected_experience_ids = [], []
    return injected_types, injected_experience_ids, shadow_types, shadow_experience_ids


def _max_diff_lines():
    """SIZE-001 体量门禁阈值。空串（vars 未创建时 `${{ vars.X }}` 透传的常态）回落默认 3000（128K 上下文窗口实测可容），
    只有显式 "0" 才关闭——上游报告问题三：此前空串经 `or 0` 静默关闭门禁，超大 PR 直送 LLM 且无提示。"""
    raw = (os.environ.get("TOUCHSTONE_MAX_DIFF_LINES") or "").strip()
    if not raw:
        return 3000
    try:
        return int(raw)
    except ValueError:
        print(f"[warn] TOUCHSTONE_MAX_DIFF_LINES={raw!r} 非数字，回落默认 3000（SIZE-001 门禁保持生效）",
              file=sys.stderr)
        return 3000



def _rule_index(rules, src="standards.yaml"):
    """rules 列表 → {id: rule}。缺 id 的条目跳过并告警（审计 #7）。

    旧实现 ``{r["id"]: r for r in rules}`` 对缺 id 的规则（YAML 手写漏键很常见）直接
    KeyError——整条评审链以无诊断信息的 traceback 崩掉。现：跳过坏条目 + stderr 指名，
    其余规则照常生效（fail-degraded 而非 fail-crash）。"""
    out = {}
    for r in rules or []:
        rid = (r or {}).get("id") if isinstance(r, dict) else None
        if not rid:
            print(f"[warn] {src} 规则缺 id（跳过该条）: {r!r}"[:300], file=sys.stderr)
            continue
        out[rid] = r
    return out

def review_pr(pr, contract, standards, provider=None):
    """§4.1 主入口：复用 PR-Agent 评审 → 发现归一 → 提交契约核对 + 栈专项确定性规则 → 裁决映射。
    等价于 map_verdict( normalize(fetch(pr)) + check_contract_consistency(...) + check_stack_rules(...) )。
    pr：上下文 dict（owner/repo/number/sha/token/diff/standards 等）；返回 {findings, risk}。
    评审层只产建议与风险分流，不产准入（准入只由质量门禁/总闸决定）。"""
    nmap = review_provider.load_nmap(os.environ.get("REPO_DIR", "."))
    rules = standards.get("rules", []) if isinstance(standards, dict) else (standards or [])
    rule_index = _rule_index(rules)
    diff = pr.get("diff", "")
    changed_files, added = contract_check.parse_diff(diff)
    added_lines = sum(len(v) for v in added.values())
    ai_raw_count = 0
    engaged = False         # glm 是否给出实质性多段评审（runner 经 _LAST_META 透出，见 review_provider）
    raw_excerpt = {}        # LLM 原始 review 段快照（0 原始建议时贴横幅，打消"是否真审过"疑虑）
    llm_notes = []          # LLM 侧非致命注记（部分降级/截断修复），进报告横幅
    unreviewed = []         # 覆盖面清单：仅 engine ok 路径有值（降级=没评审，无覆盖面可言）
    unreviewed_total = 0    # 覆盖面真·总数（列表截 100 后的保真口径）
    engine_detail = ""      # 降级/失败时的具体原始错误（PR#68 做准的 reason）——留给渲染层
    max_lines = _max_diff_lines()
    size_findings = []
    if max_lines > 0 and added_lines > max_lines:
        engine_status = "skipped_large_diff"
        size_findings = [{
            "rule_id": "SIZE-001", "file": "", "line": 0,
            "category": "contract", "severity": "block_candidate",
            "confidence": 1.0,
            "rationale": f"PR 改动约 {added_lines} 行，超过单 PR 上限 {max_lines} 行。",
            "fix_direction": "请拆分为多个 PR，每个聚焦一个变更。",
            "fix_reasoning": "一次性提交大量代码增加评审难度与出错风险。",
            "done_criteria": {"kind": "deterministic", "spec": {"recheck": "SIZE-001"}},
            "suggested_fix": "请拆分为多个 PR，每个聚焦一个变更。",
            "agent": "contract-check",
        }]
        review_findings = []
    else:
        engine_status = "ok"
        try:
            raw_items = review_provider.fetch(pr, provider)
            ai_raw_count = len(raw_items)
            review_findings = review_provider.normalize(raw_items, nmap)
            _meta = review_provider.invoke_meta()
            engaged = _meta.get("review_engaged", False)   # review_reliable 据此区分"审完无问题"与"裁空/吞没"
            raw_excerpt = _meta.get("raw_review_excerpt") or {}  # 0 原始建议时贴横幅的 LLM 原始 review 段
            unreviewed = _meta.get("unreviewed_files") or []     # token 预算裁掉、未进 LLM 评审的文件（0.44）
            # 真·总数：列表在 runner 侧截 100 防畸形巨表，横幅/产物必须按真值报（截断保真）
            unreviewed_total = _meta.get("unreviewed_total")
            if not (isinstance(unreviewed_total, int) and unreviewed_total >= 0):
                unreviewed_total = len(unreviewed)                # 老协议回退：len(列表)
            # 部分降级/修复解析：整轮仍可信（另一侧有真实产出/条目仍在），不触发降级，
            # 但必须在报告可见——improve 连挂数日而 review 正常时，建议侧信号长期缺失
            # 却无人察觉；截断修复则意味着条目可能被静默修丢（本次静默故障排查 S1/S3）。
            if _meta.get("partial_tool_failure") == "improve":
                llm_notes.append("⚠️ **本轮 improve 工具失败**：建议侧（code_suggestions）信号缺失，"
                                 "review 侧发现仍有效——非整轮不可信，真实错误见交互日志。")
            elif _meta.get("partial_tool_failure") == "review":
                llm_notes.append("⚠️ **本轮 review 工具失败**：key_issues 侧信号缺失，"
                                 "improve 侧建议仍有效——非整轮不可信，真实错误见交互日志。")
            if _meta.get("repaired_parses"):
                llm_notes.append(f"ℹ️ 本轮有 {_meta['repaired_parses']} 次 LLM 预测经修复解析"
                                 "（输出截断/畸形的弱信号，条目可能被修复丢弃），原文见交互日志。")
            if unreviewed_total:
                # 评审覆盖面（pr-agent 0.44）：绿灯结论的可信边界。总数按真值（截断保真），
                # 点名前 5 个，与上游 coverage footer 同款克制（MAX_REVIEW_COVERAGE_FILES）——
                # 横幅是提示不是文件浏览器；截断后的清单在 touchstone-findings.json 产物里。
                _shown = "、".join(f"`{f}`" for f in unreviewed[:5])
                llm_notes.append(f"⚠️ **评审覆盖面**：{unreviewed_total} 个文件因 diff 超 token 预算"
                                 f"未进入 LLM 评审（{_shown}{' 等' if unreviewed_total > 5 else ''}）"
                                 "——本轮绿灯结论不含这些文件。")
        except review_provider.ReviewEngineDegraded as e:
            engine_status = e.degraded
            engine_detail = e.reason or ""      # PR#68 做准的具体原因（工具 + litellm 真实异常 + 过滤后 stderr）
            print(f"[review_pr] 评审引擎降级（{e.degraded}）：{e.reason}", file=sys.stderr)
            review_findings = []
        except RuntimeError as e:
            engine_status = "no_engine"
            engine_detail = str(e)
            print(f"[review_pr] PR-Agent 不可用：{e}", file=sys.stderr)
            review_findings = []
    contract_findings = contract_check.check_contract_consistency(diff, contract or {}, rule_index)
    stack_findings = stack_rules.check_stack_rules(diff, rule_index)
    det_warning = contract_check._PARSE_WARNING or ""
    # 范围事实（修订设计 §4.1，评审意见 7）：确定性修改范围 + 仓级路径规则命中 + 内容指纹
    sf = contract_check.scope_facts(
        diff, contract_check.load_scope_rules(os.environ.get("REPO_DIR", ".")))
    findings, risk = review_provider.map_verdict(
        size_findings + review_findings + contract_findings + stack_findings, nmap,
        changed_files=changed_files, scope_facts=sf)
    return {"findings": findings, "risk": risk, "engine_status": engine_status,
            "det_warning": det_warning, "ai_raw_count": ai_raw_count,
            "added_lines": added_lines, "changed_files": changed_files,
            "scope_facts": sf, "llm_notes": llm_notes, "engaged": engaged,
            "raw_excerpt": raw_excerpt, "engine_detail": engine_detail,
            "unreviewed_files": unreviewed, "unreviewed_total": unreviewed_total}


def main():
    _t_main0 = time.monotonic()
    token = os.environ["GITHUB_TOKEN"]

    event = load_yaml(os.environ["GITHUB_EVENT_PATH"]) or {}
    pr = event.get("pull_request", {})
    number = pr.get("number")
    head_sha = pr.get("head", {}).get("sha")
    owner, repo = os.environ["GITHUB_REPOSITORY"].split("/", 1)
    if not number:
        sys.exit("非 PR 事件，跳过。")

    sync_touchstone_config(owner, repo, number, token,
                           os.environ.get("REPO_DIR", "."))

    standards = load_yaml(STANDARDS_PATH)
    if not standards:
        sys.exit(f"未找到规范 {STANDARDS_PATH}")
    contract = load_yaml(CONTRACT_PATH)
    rule_index = _rule_index(standards.get("rules", []))

    diff = get_pr_diff(owner, repo, number, token)
    changed_files, _ = contract_check.parse_diff(diff)

    # 评审主链（§4.1）：PR-Agent 评审归一 + 契约核对 + 栈专项确定性规则 → 裁决映射
    # 守卫核销预取（issue #139 方案 C）：上轮权威清单的 open 项守卫事实须在评审【前】
    # 注入，故此处轻量早取一次评论解析 marker（与下方反馈循环的正式取用相互独立；
    # 双取代价一次 GET，换取不重排 main 流程）。失败即空——绝不阻塞评审链路。
    guard_adjudication = ""
    try:
        # import 同在 try 内（PR#140 R3 意见 3）：模块导入失败也走"失败即空"降级，
        # 不允许守卫增强层的 ImportError 崩掉 main()——与 attach 面的包裹口径一致。
        from touchstone import guard_context as _gc0
        if _gc0.enabled():
            _pre_cmt_path = (f"/repos/{owner}/{repo}/pulls/{number}/comments" if _is_gitcode()
                             else f"/repos/{owner}/{repo}/issues/{number}/comments")
            # 审计 #5：翻页取全（默认单页 30 条，>30 评论的 PR 最早的 marker 会被截掉）
            _pre_comments = ghclient.paginate(_api_base() + _pre_cmt_path, token)
            _pre_bodies = loop.trusted_bodies(
                _pre_comments if isinstance(_pre_comments, list) else [], None)
            _pre_cl = checklist_mod.parse_latest(_pre_bodies)
            if _pre_cl:
                guard_adjudication = _gc0.render_adjudication(
                    _pre_cl.get("items", []), os.environ.get("REPO_DIR", "."))
    except Exception:
        guard_adjudication = ""
    # repo_dir 显式透传（PR#140 R2 意见 1）：guard_adjudication 用 REPO_DIR 解析，而
    # review_provider 侧 digest 此前回退 pr_ctx.get("repo_dir",".")——REPO_DIR 非默认时
    # 两个守卫面解析目录不一致，digest 静默扫错目录返回空。单一来源，两面同目录。
    pr_ctx_review = {"owner": owner, "repo": repo, "number": number, "sha": head_sha,
                     "token": token, "diff": diff, "standards": standards,
                     "repo_dir": os.environ.get("REPO_DIR", "."),
                     "guard_adjudication": guard_adjudication,
                     # 审计 #51：改动文件后缀粗判的技术栈 → review_provider 据此对 seeds.yaml
                     # 按栈过滤（load_seed_injection(stack=...)，原本"已知 gap"里永不生效的
                     # stack 字段由此接通；不确定=空串=通用，不过滤）。
                     "stack": _stack_from_diff(diff)}
    _t_rev0 = time.monotonic()
    _out = review_pr(pr_ctx_review, contract, standards)
    _t_review = round(time.monotonic() - _t_rev0, 2)
    findings, risk = _out["findings"], _out["risk"]
    engine_status = _out.get("engine_status", "ok")
    det_warning = _out.get("det_warning", "")
    ai_raw_count = _out.get("ai_raw_count", 0)
    llm_notes = _out.get("llm_notes") or []
    engine_detail = _out.get("engine_detail", "")   # 降级原始错误 → CAUTION 精简指向 + 验证与日志详列
    added_lines = _out.get("added_lines", 0)
    n_changed = len(_out.get("changed_files") or [])
    engaged = _out.get("engaged", False)
    raw_excerpt = _out.get("raw_excerpt") or {}
    unreviewed_files = _out.get("unreviewed_files") or []   # 覆盖面清单：进 findings 产物（横幅已由 llm_notes 承担）
    unreviewed_total = _out.get("unreviewed_total") or len(unreviewed_files)  # 真·总数（截断保真）
    # 本轮 LLM 评审是否可靠（engine_status + 可疑空收敛判据 + engaged 逃生口）。不可靠时
    # checklist 不予自动销项、loop 不收敛、autonomy 不自动放行--防"diff 被裁空/LLM 随机性"
    # 假收敛放行未评审代码。engaged 让"glm 审完无问题"的干净 PR 不再被误判可疑（PR #51）。
    reliable = review_provider.review_reliable(engine_status, ai_raw_count, added_lines, engaged=engaged)
    # 引擎级故障（LLM 调用失败等，见 loop.INFRA_FAILURE_STATUSES）：检视未发生，本轮不消耗
    # 作者轮次预算——与 reliable 正交：reliable=False 只是「不收敛」（轮照烧），engine_failed
    # 是「没评审过」（轮不烧）。PR #183 实录：glm 429 连续 4 轮 llm_failed，白烧 4 轮预算。
    engine_failed = engine_status in loop.INFRA_FAILURE_STATUSES

    # 反馈循环：从历史评论 marker 取状态 → 决策 → 回贴附状态与新 marker。
    # 只信机器人自己发的评论（按发帖人过滤）——否则 author 可自己发伪造 marker 洗掉抗博弈闸。
    all_bodies = []          # 全量评论正文（含 author）——只用于解析 ack 申报（申报是输入信号）
    stale_reviews = []       # 历史评审评论（bot 发、未折叠）——新评论落地后折叠（视觉降噪）
    try:
        # GitCode 适配：PR 评论读取同走 /pulls/{n}/comments（与回贴端点一致）。
        _cmt_path = (f"/repos/{owner}/{repo}/pulls/{number}/comments" if _is_gitcode()
                     else f"/repos/{owner}/{repo}/issues/{number}/comments")
        # 审计 #5：翻页取全（默认单页 30 条）——loop marker/checklist marker/ack 申报常在
        # 最早的评论里，单页截断会让轮次预算、震荡/无推进闸、销项全部失明。
        comments = ghclient.paginate(_api_base() + _cmt_path, token)
        comments = comments if isinstance(comments, list) else []
        all_bodies = [c.get("body", "") for c in comments]
        try:
            bot_login = (gh("GET", "/user", token) or {}).get("login")
        except requests.exceptions.RequestException:
            bot_login = None
        if not bot_login:
            # GET /user 未返回身份（默认 GITHUB_TOKEN 常见）——不降级：trusted_bodies 改按
            # [bot] 后缀过滤（github-actions[bot]），防伪造仍生效（人无法注册 [bot] 后缀）。
            print("[info] GET /user 未返回身份：loop marker 改按 [bot] 后缀过滤（防伪造仍生效）",
                  file=sys.stderr)
        # 此刻列表不含即将发出的本轮新评论——正是「历史」的定义；外层 except 路径保持 []。
        stale_reviews = _stale_review_comments(comments, bot_login)
        bodies = loop.trusted_bodies(comments, bot_login)
    except requests.exceptions.RequestException:
        bodies = []
    state = loop.parse_latest_state(bodies)

    # 轮次台账（修订设计 §4.4，评审意见 10）：同源检测 + 历史继承。台账是增强，失败不阻塞。
    scope_facts = _out.get("scope_facts") or {}
    pr_labels = [l.get("name") for l in (pr.get("labels") or []) if isinstance(l, dict)]
    ledger = lineage.detect_lineage(
        scope_facts.get("fingerprint"), lambda m, p: gh(m, p, token),
        owner, repo, number, current_labels=pr_labels)

    # 收敛清单（修订设计 §4.3，评审意见 1、3）：上一轮权威清单（受信 marker）+ author 申报（ack，
    # 全量评论）→ 按达成判据复核销项 → 新一轮权威清单。首轮并入台账继承的历史未销项。
    prev_cl = checklist_mod.parse_latest(bodies)
    if prev_cl is None and ledger.get("inherited_open_items"):
        prev_cl = {"round": 0, "items": ledger["inherited_open_items"]}
    acks = checklist_mod.parse_acks(all_bodies)
    # 引擎故障轮 round_no 冻结在 state.round（清单不显示「新的一轮」——检视没成功就没有新轮；
    # 首轮即故障时 round=0，render 的 `if cl.get("round")` 自然隐藏轮次行）。确定性发现（规则/
    # 契约核对不依赖 LLM）仍照常吸收进清单——故障只是 AI 部分缺席，不废掉确定性检查的结果。
    cur_cl = checklist_mod.reconcile(prev_cl, acks, findings,
                                     round_no=state.round if engine_failed else state.round + 1,
                                     review_reliable=reliable)
    try:                                       # 守卫事实附着（issue #139 方案 C）：只附着不判断，
        from touchstone import guard_context as _gc2   # 供人 waived 佐证 + 下一轮核销注入复用
        _gc2.attach_guard_facts(cur_cl, os.environ.get("REPO_DIR", "."))
    except Exception:
        pass    # 静默豁免：守卫附着是纯增强，失败即无守卫行；抛出会阻塞收敛清单主链
    checklist_mod.snapshot(cur_cl)          # 本轮快照写入文件（供可视化与校准回放）
    n_unverified = len(checklist_mod.unverified_claims(cur_cl))   # author 自证未核准销项数

    ci_pass = ci_verdict(owner, repo, head_sha, token)   # 供闭环：CI/verify 红则不收敛
    decision, reason, new_state = loop.loop_step(
        findings, rule_index, state, ci_passed=ci_pass,
        checklist_pair=(prev_cl, cur_cl), ledger=ledger, review_reliable=reliable,
        engine_failed=engine_failed)
    loop_info = (decision, reason, loop.render_marker(new_state))
    # v2：可见清单渲染由 render_report 内 render_findings_checklist 统一负责（合并 AI 评审 +
    # 清单）；此处只算 rounds_left 供状态行/台账用，不再预算 checklist_md。
    _rounds_left = loop.remaining_rounds(
        cur_cl.get("round", 0), ledger.get("rounds_left", loop.MAX_ROUNDS))

    # 变更分类（供自治经验层/auto_merge）：touchstone 侧此时已知 risk/findings/changed_files
    cls = autonomy.change_class(risk, findings, sorted(changed_files), rule_index)
    contract_clean = not any(f.get("agent") == "contract-check" for f in findings)

    # 本轮注入的经验类型（active 生产 + shadow 实验）由 _collect_injection 统一取：shadow 取值独立
    # try 隔离，失败不 wipe active（pr-agent review #117 隔离点）。详见 _collect_injection。
    injected_types, injected_experience_ids, shadow_types, shadow_experience_ids = _collect_injection()

    rd_path = os.environ.get("TOUCHSTONE_RDJSON_PATH")
    if rd_path:                       # 可选 reviewdog 后端：导出 RDFormat，行内投递交 reviewdog
        try:
            with open(rd_path, "w", encoding="utf-8") as _rf:
                json.dump(review_provider.to_rdjson(findings), _rf, ensure_ascii=False)
        except OSError as e:
            print(f"[warn] RDJSON 写出失败: {e}", file=sys.stderr)

    # 可插拔检查 → 对外发【一个】总闸状态（策略全在 .touchstone/checks.yaml）。
    # CI 中由独立 gate job 在(可选)verify 之后聚合并发布，此处置 TOUCHSTONE_SKIP_GATE 跳过自发、
    # 避免重复发；本地/dry-run（未设该环境变量）则就地计算并发布，行为不变。
    # 【顺序】gate 上移到 post_results 之前：可观测性块（metrics/告警/遥测）需 gate（_metrics.build
    # 的 gate= 参数），而遥测上报结果 _tel_res 要进评审报告横幅——故 gate + 可观测性须先于回贴评论
    # 跑完。gate 仍只在非 SKIP 时计算并外发，行为不变。
    gate = None
    if os.environ.get("TOUCHSTONE_SKIP_GATE", "").lower() not in ("1", "true", "yes", "on"):
        try:
            chk_cfg = checks.load_config(os.environ.get("REPO_DIR", "."))
            # 确定性发现 = contract-check（scope/test/dup/untested/sec）+ touchstone-rules（CTR/SPR/JAVA）。
            # 注意：之前这里误引了未定义的 contract_findings（NameError，仅因 gate 路径少被走到而隐藏）。
            det_findings = [f for f in findings
                            if f.get("agent") in ("contract-check", "touchstone-rules")]
            pr_ctx = {"owner": owner, "repo": repo, "sha": head_sha, "token": token,
                      "files": sorted(changed_files), "contract_findings": det_findings}
            gate, _ = checks.post_gate(pr_ctx, chk_cfg, checks.run_checks(chk_cfg, pr_ctx))
        except Exception as e:
            # 总闸是旁路增强（独立 check-run 状态），不是评审交付本身。catch 宽到 Exception：
            # gate 块上移到 post_results 之前（本 PR），若只 catch RequestException，则 checks.*
            # 抛任意非网络异常（如 checks.py 未来改动引入的编程错误、插件聚合异常）会向上冒泡、
            # post_results 永不执行——评审评论被静默吞掉。原本 gate 在 post_results 之后，崩溃也只是
            # 评论已发之后再炸 job；顺序上移后必须拓宽 except 才能守住「总闸崩溃不阻断评审交付」
            # 的既有契约（gate 维持 None，真因进 stderr，防静默故障）。
            print(f"[info] 总闸跳过（不阻断评审）: {type(e).__name__}: {e}", file=sys.stderr)

    # 运行指标（运维可观测性）：每轮追加一条扁平指标到事件流，供 CI 聚合成 dashboard/告警。
    # 与 findings.json（autonomy 决策用的完整状态）分开——本条只含可累加的健康数值。失败不阻塞。
    # 【顺序】整块上移到 post_results 之前：遥测 forward 的结果 _tel_res（ok/disabled/failed）要进
    # 评审报告横幅（防静默故障：可观测性子系统自身状态对运维可见，不只进 stderr），须在回贴评论前
    # 算出。附带好处——回贴评论失败时指标/遥测仍落盘（可观测性不依赖评论投递成功，契合防静默约定）。
    _tel_res = "disabled"   # 默认：未配端点 / 指标产出整体失败时保持（报告里不显示遥测行，免噪声）
    try:
        from touchstone import metrics as _metrics
        _meta = None
        try:
            _meta = review_provider.invoke_meta()
        except Exception as e:
            # meta 是 best-effort（partial_tool_failure/repaired_parses 计数）；取不到按 None，
            # 不阻断指标产出——但留痕，不让降级静默（防静默故障约定）。
            print(f"[info] metrics invoke_meta 取数失败（按 None 继续）: {e}", file=sys.stderr)
        _rec = _metrics.build(
            number, head_sha, risk, findings,
            engine_status=engine_status, review_reliable=reliable,
            ai_raw_count=ai_raw_count, loop_decision=decision, gate=gate,
            unverified_claims=n_unverified, change_class=cls,
            added_lines=added_lines, round_no=new_state.round, invoke_meta=_meta,
            durations={"t_review": _t_review,
                       "t_total": round(time.monotonic() - _t_main0, 2)})
        _metrics.emit(_rec)
        # 告警钩子（可观测性投递）：按 env 选通道，判定并投递到客户自己配置的渠道。
        # 总开关不开 → 无操作（只保留上面的 metrics artifact）。失败绝不冒泡——不拖垮评审 job；
        # 但留痕（防静默故障约定）：告警子系统自身故障不许静默（同 ironic-for-observability）。
        try:
            from touchstone import alert as _alert
            # 聚合取数单独兜底：load/summarize 挂掉（损坏/权限/未来改动）时按 None 继续，
            # 不能让聚合失败连带吞掉本轮单轮告警（silent_failure 等）——它们只依赖 _rec。
            try:
                _agg = _metrics.summarize(_metrics.load())
            except Exception as e:
                print(f"[info] alert 聚合取数失败（按 None 继续，单轮告警仍发）: {e}", file=sys.stderr)
                _agg = None
            _alert.run(_rec, _agg, dict(os.environ),
                       {"owner": owner, "repo": repo, "number": number,
                        "token": token, "run_url": _run_link()})
        except Exception as e:
            print(f"[warn] 告警投递失败（不阻塞评审）: {e}", file=sys.stderr)
        # 使用遥测（可选，默认关）：把本轮 metrics 记录上报到【配置指定】的中心汇聚点。
        # 未配 TOUCHSTONE_TELEMETRY_ENDPOINT → 无操作（不外发）。失败绝不冒泡——同 alert，
        # 可观测性子系统自身故障留痕（防静默故障约定），不拖垮评审 job。
        try:
            from touchstone import telemetry as _tel
            # forward 失败时【返回】"failed: ..."（内部已 catch、不抛）——下面的 except 只兜底
            # forward 自身抛异常的罕见情形。故须显式看返回值：返回失败串时留痕，否则 forward 的
            # 常见失败路径被静默吞（防静默故障：可观测性子系统自身故障不许静默，同 alert 约定）。
            _tel_res = _tel.forward([_rec], dict(os.environ), version=_rec.get("version", ""))
            if isinstance(_tel_res, str) and _tel_res.startswith("failed:"):
                print(f"[warn] 遥测上报失败（不阻塞评审）: {_tel_res}", file=sys.stderr)
        except Exception as e:
            # forward 自身抛异常（罕见）→ 显式置 failed: 串，让报告横幅也显示失败（防静默故障：
            # 异常路径同样可见），并 stderr 留痕。_tel_res 此前未赋值，此处兜底。
            _tel_res = f"failed: {e}"
            print(f"[warn] 遥测上报失败（不阻塞评审）: {e}", file=sys.stderr)
        # 评审健康度看板（可选，默认关）：每轮把健康度写成一个"活"的本仓 issue（重写 body，
        # 静默刷新——GitHub 不为 issue body 编辑发通知），仅显著事件追加评论。未配
        # TOUCHSTONE_METRICS_ISSUE=true → 无操作。失败绝不冒泡——同 alert/telemetry，
        # 可观测性子系统故障只留痕、不拖垮评审 job（run 内部已 catch，此处 except 仅兜底罕见抛出）。
        try:
            from touchstone import metrics_issue as _mi
            _mi_res = _mi.run(_rec, dict(os.environ),
                              {"owner": owner, "repo": repo, "number": number,
                               "token": token, "run_url": _run_link()})
            if isinstance(_mi_res, str) and _mi_res.startswith("failed:"):
                print(f"[warn] metrics-issue 看板更新失败（不阻塞评审）: {_mi_res}", file=sys.stderr)
        except Exception as e:
            print(f"[warn] metrics-issue 钩子异常（不阻塞评审）: {type(e).__name__}: {e}", file=sys.stderr)
    except Exception as e:
        # 指标产出失败不阻塞评审主链——但绝不静默：可观测性子系统自身故障必须留痕
        # （同 learning_loop 2026-07-04 的防静默约定，ironic-for-observability 反模式）。
        # _tel_res 保持 "disabled"：指标整体失败时报告不显示遥测行（避免误导），真因在 stderr。
        print(f"[warn] 运行指标产出失败: {e}", file=sys.stderr)

    post_results(owner, repo, number, head_sha, token, risk, findings, loop_info, cls, diff,
                 injected_types=injected_types, injected_experience_ids=injected_experience_ids,
                 shadow_types=shadow_types, shadow_experience_ids=shadow_experience_ids,
                 engine_status=engine_status, det_warning=det_warning,
                 ai_raw_count=ai_raw_count, added_lines=added_lines, n_changed=n_changed,
                 scope_facts=scope_facts, checklist=cur_cl, rounds_left=_rounds_left,
                 ledger=ledger, stale_comments=stale_reviews,
                 review_reliable=reliable, llm_notes=llm_notes,
                 raw_excerpt=raw_excerpt, unverified_claims=n_unverified,
                 telemetry_status=_tel_res, engine_detail=engine_detail)

    # PR 列表页零点击可见性：销项状态 + 未销项量级标签（best-effort，绝不阻塞主链；
    # GitCode 守卫跳过——通路未核实，同 #219 折叠守卫模式）
    _sync_state_labels(owner, repo, number, token, decision, cur_cl)

    # 升级到人：打标签（best-effort）
    if decision == "escalate":
        try:
            _post_escalate_label(owner, repo, number, token)
        except requests.exceptions.RequestException as e:
            # needs-human 标签打不上 = 人工升级信号丢失——escalate 本身已定，标签只是
            # 传达渠道，失败必须可见（否则升级悄悄变没人接）。
            print(f"[warn] escalate 标签添加失败（人工升级信号未传达）: {e}", file=sys.stderr)

    # 校准 + 自治决策入口：落盘供下游 join / auto_merge 组装
    # 原子：这份 findings 是 verify join 与 autonomy auto_merge 的组装依据，进程被杀
    # 留下的半文件会让下游把残缺状态当真（见 atomicio 模块头注）。
    _findings_doc = {"pr": number, "sha": head_sha, "risk": risk, "findings": findings,
                     "changed_files": sorted(changed_files), "loop_decision": decision,
                     "contract_clean": contract_clean, "change_class": cls,
                     "gate": gate,
                     # 引擎健康度（供 autonomy 决策）：engine_status/ai_raw_count/added_lines +
                     # 预算 review_reliable。review_reliable=False 时 autonomy 不自动放行
                     # （防假收敛放行未评审代码，见 review_provider.review_reliable）。
                     "engine_status": engine_status, "ai_raw_count": ai_raw_count,
                     "added_lines": added_lines, "review_reliable": reliable,
                     "review_engaged": engaged,
                     # LLM 原始 review 段快照（0 原始建议时的"真审过"证据，见 _clean_review_trace）
                     "raw_review_excerpt": raw_excerpt,
                     # 评审覆盖面（pr-agent 0.44 remaining_files_list 透传）：token 预算裁掉、
                     # 未进 LLM 评审的文件。绿灯结论的可信边界，供 autonomy/下游对账。
                     "unreviewed_files": unreviewed_files,
                     "unreviewed_total": unreviewed_total,
                     # author 自证但未经人核准的销项数（waived/split）——autonomy 独立闸据此
                     # 拒放行（多层：即便 loop_decision 被虚报，本计数由 touchstone 侧写入）。
                     "unverified_claims": n_unverified,
                     # PR 作者出处（供 autonomy 作者信任闸）：login + GitHub author_association。
                     # 事件 payload 由 Actions 平台生成、作者不可伪造；产物缺/空此字段时
                     # autonomy 侧 fail-closed 不放行。`or {}` 同时兜底 key 缺失与 null 两种
                     # 情形（.get("user", {}) 只挡缺失、挡不住 "user": null 的 None.get() 崩溃）。
                     "author": {"login": (pr.get("user") or {}).get("login"),
                                "association": pr.get("author_association")}}
    atomic_write_json(artifact_path("touchstone-findings.json"), _findings_doc)

    # 风险分流的 job 输出：供下游 verify job 决定是否触发验证
    gho = os.environ.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a", encoding="utf-8") as f:
            f.write(f"verification_decision={risk['verification_decision']}\n")
            f.write(f"risk_band={risk['risk_band']}\n")
            f.write(f"loop_decision={decision}\n")

    print(f"[touchstone] 风险={risk['risk_band']} 发现={len(findings)} 条")


def _emergency_comment(reason):
    """看门狗最后通牒（issue #211）：main() 未正常完成时把「本轮评审未完成」贴到 PR。
    此刻 main() 局部变量已不可用 → 从环境自重导上下文（GITHUB_REPOSITORY /
    GITHUB_EVENT_PATH 里的 pull_request.number / GITHUB_TOKEN）。看门狗自身绝不抛
    （否则压过原始退出语义）——任何失败只 stderr 留痕，job 仍以非零码收场，守住
    「绝不静默」下限。评论体刻意**不携带**正常轮的 touchstone-result marker（销项
    循环会把残缺状态当完整轮记账），改用独立的 touchstone-incomplete marker 供机器分诊。"""
    try:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GITHUB_TOKEN") or ""
        number = None
        _evp = os.environ.get("GITHUB_EVENT_PATH", "")
        if _evp and os.path.exists(_evp):
            with open(_evp, encoding="utf-8") as f:
                number = ((json.load(f) or {}).get("pull_request") or {}).get("number")
        if not (repo and token and number):
            print(f"[watchdog] PR 上下文不完整，跳过降级评论（job 仍失败）: "
                  f"repo={repo!r} number={number!r} token={'有' if token else '缺'}", file=sys.stderr)
            return
        _cmt = (f"/repos/{repo}/pulls/{number}/comments" if _is_gitcode()
                else f"/repos/{repo}/issues/{number}/comments")
        body = ("⚠️ **本轮评审未完成**：touchstone 编排器异常终止，本轮没有评审结论。\n\n"
                f"原因：{_redact_secrets(str(reason))[:800]}\n\n"
                "job 已按失败收场；请结合 run 日志排查后 re-run。"
                "<!-- touchstone-incomplete -->")
        gh("POST", _cmt, token, {"body": body})
        print(f"[watchdog] 已在 {repo}#{number} 贴出降级评论", file=sys.stderr)
    except Exception as e:
        print(f"[watchdog] 降级评论发布失败（job 仍将失败，守住『绝不静默』下限）: "
              f"{type(e).__name__}: {e}", file=sys.stderr)


def _run_with_watchdog():
    """issue #211：编排器顶层看门狗——「崩溃 + 绿灯」组合自此不可能。三类结局归一：
      ① main() 正常完成（尾部最终 print 是唯一完成宣言）→ 自然退出 0；
      ② SystemExit(0/None)——Python 语义下**无 traceback 的静默成功退出**（库层裸
         sys.exit()/未来的早退路径都落这；SystemExit 非 Exception 子类，会穿透 main()
         里所有 except Exception 块）→ 贴降级评论 + 转 exit 1；
      ③ 其余未捕获异常（含穿透性 BaseException）→ traceback 留档 + 贴降级评论 + exit 1。
    例外：KeyboardInterrupt（concurrency cancel-in-progress 的平台取消）原样放行——
    新轮自会接管该 SHA，贴「未完成」只是噪音；取消语义由平台标记，不归看门狗管。"""
    try:
        main()
    except KeyboardInterrupt:
        raise
    except SystemExit as e:
        if e.code is None or e.code == 0:
            _emergency_comment(f"编排器异常退出 SystemExit({e.code!r})——无 traceback 的静默成功路径")
            sys.exit(1)
        raise    # 带信息/非零码的主动退出（非 PR 跳过 / 规范缺失）：本就大声，保持原样
    except BaseException as e:
        traceback.print_exc()
        _emergency_comment(f"未捕获异常 {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    _run_with_watchdog()
