"""按需本地端到端：真 LLM（复用 Claude Code 的 key + 同一端点）→ 真 touchstone 下游链。

目的（/goal）：构建足够多的极端场景，在本地用真 LLM 跑通 touchstone 评审主链，确保
「0意见→卡死」「裁空/吞没假收敛」「大 diff 崩」「内容过滤静默」等异常不再回归。
此类测试【按需本地运行，不在 CI 跑】——默认全跳过，保持仓库"离线可跑"（CLAUDE.md DoD）。

跑法：
    TOUCHSTONE_LLM_E2E=1 python -m pytest tests/test_e2e_llm.py -q -s
LLM 配置（皆可 env 覆盖；key 默认复用 Claude Code 的 ANTHROPIC_AUTH_TOKEN，绝不写入仓库/记忆）：
    LLM_API_KEY   默认 $ANTHROPIC_AUTH_TOKEN（Claude Code 当前用的 key）
    LLM_BASE_URL  默认 https://open.bigmodel.cn/api/coding/paas/v4（coding 端点，key 可用）
    LLM_MODEL     默认 glm-5.3（与 Claude Code 同款）；设 glm-4.5-air = 快稳基线（迭代时省时）

实现说明（两种模式）：
  • 【真子进程模式】（`test_llm_e2e_subproc_*`）：本地装 pr-agent（`python -m venv .pragent-venv &&
    .pragent-venv/bin/pip install pr-agent`，与 CI 同），用 LocalGitProvider（TOUCHSTONE_GIT_PROVIDER=local）
    跑【未改动的生产 runner 子进程】审本地分支 diff —— 全程真：真 pr-agent 提示词/工具 + 真 LLM +
    真 _engaged（runner 内 compute_engaged）。这是最忠实的端到端（仅 GitHub fetch 换成本地 diff）。
    local 模式 pr-agent 只支持 review（不支持 improve），故 code_suggestions 恒空、只验 key_issues 侧。
  • 【注入模式】（`test_llm_e2e_*` 非 subproc）：真 LLM 现产评审经 pr_ctx['pr_agent_output'] 喂入真
    orchestrator.review_pr。覆盖 review + code_suggestions 两侧 + 各类极端场景，不需 pr-agent venv。
  • 断言只锁【不变式】（干净 PR 不卡死、不崩、风险分流合法），不强断 LLM 是否抓到某具体缺陷
    （LLM 漏报是固有的，强断会 flaky）；实际 LLM 行为经 -s 打印供人肉眼诊断。
"""
import os
import re
import shlex
import subprocess
import textwrap

import pytest
import yaml

from touchstone import orchestrator
from touchstone import review_provider as RP

# ---- 按需开关：默认全跳过 ---------------------------------------------------
_LLM_E2E = os.environ.get("TOUCHSTONE_LLM_E2E") == "1"
_skip = pytest.mark.skipif(
    not _LLM_E2E,
    reason="按需本地跑：设 TOUCHSTONE_LLM_E2E=1（并确保 LLM_API_KEY/ANTHROPIC_AUTH_TOKEN 可用）")

_STANDARDS = yaml.safe_load(open(os.path.join(
    os.path.dirname(orchestrator.__file__), "..", ".touchstone", "standards.yaml")))


# ---- 真 LLM 调用 ------------------------------------------------------------
def _llm_conf():
    """读 LLM 配置（key 默认复用 Claude Code 的 ANTHROPIC_AUTH_TOKEN）。缺 key → skip。"""
    key = os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not key:
        pytest.skip("未提供 LLM_API_KEY / ANTHROPIC_AUTH_TOKEN——真 LLM 端到端需 key")
    base = os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/coding/paas/v4")
    model = os.environ.get("LLM_MODEL", "glm-5.3")
    return key, base, model


_REVIEW_SCHEMA = textwrap.dedent("""\
    严格按此 YAML schema 返回（只返回一个 YAML 代码块，不加任何解释文字）：
    ```yaml
    review:
      estimated_effort_to_review: <一句话工作量评估>
      security_concerns: <有安全顾虑则简述，无则给空字符串 "">
      relevant_tests: <有相关测试则述，无则给空字符串 "">
      key_issues_to_review:
        - relevant_file: <文件路径>
          start_line: <起始行>
          end_line: <结束行>
          issue_header: <一句话标题>
          issue_content: <详述>
          label: <security|critical bug|possible bug|possible issue|performance|enhancement|maintainability|typo|general>
    code_suggestions:
      - relevant_file: <文件路径>
        relevant_lines_start: <起始行>
        relevant_lines_end: <结束行>
        one_sentence_summary: <一句话>
        improved_code: <改进后的代码>
        label: <同上 label 集合>
    ```
    若确无问题：key_issues_to_review 与 code_suggestions 都给空列表 []，其余段照填。
""")


def _parse_review_yaml(text):
    """从 LLM 回复里取 YAML（去 ```yaml 围栏），safe_load 成 pr_agent_output 形状的 dict。
    不刻意写 _engaged——交由下游真 compute_engaged 现算（这正是被测的链）。解析失败抛错带原文。"""
    m = re.search(r"```(?:yaml|yml)?\s*\n(.*?)```", text, re.S)
    body = m.group(1) if m else text
    try:
        data = yaml.safe_load(body) or {}
    except yaml.YAMLError as e:
        raise RuntimeError(f"LLM 输出非合法 YAML：{e}\n---- 原文 ----\n{text[:1500]}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"LLM 输出顶层非 dict（{type(data).__name__}）\n---- 原文 ----\n{text[:1500]}")
    if not isinstance(data.get("review"), dict):
        data["review"] = {}
    if not isinstance(data.get("code_suggestions"), list):
        data["code_suggestions"] = []
    return data


def _llm_review(diff, focus):
    """真调 LLM 审 diff，返回 pr_agent_output 形状的 dict（不含 _engaged）。
    内容过滤/端点错误 → RuntimeError（带类型+原因），调用方可据此 skip/fail。"""
    import openai
    key, base, model = _llm_conf()
    prompt = (
        f"你是资深代码评审。审下列 unified diff，{focus}。{_REVIEW_SCHEMA}"
        f"---- DIFF ----\n{diff}\n")
    client = openai.OpenAI(base_url=base, api_key=key, timeout=120)
    try:
        r = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}],
            max_tokens=2000, temperature=0.2)
    except Exception as e:
        raise RuntimeError(
            f"LLM 调用失败（{type(e).__name__}: {str(e)[:200]}）"
            "——可能 glm 内容过滤/端点余额/超时，见 memory glm-empty-response-pragent") from e
    return _parse_review_yaml((r.choices[0].message.content or "").strip())


# ---- 本地 git diff 构造 -----------------------------------------------------
def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


def _make_diff(tmp_path, pr_files):
    """pr_files: {path: content}，全为【新增】文件。建本地 git 仓 → 返回该"PR"的 unified diff。"""
    repo = str(tmp_path)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    for path, content in pr_files.items():
        full = os.path.join(repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
    _git(repo, "add", "-A")
    return subprocess.run(
        ["git", "-C", repo, "diff", "--cached", "HEAD"], capture_output=True, text=True).stdout


def _run_pipeline(diff, raw_review, contract=None):
    """真 orchestrator.review_pr（注入真 LLM 评审），返回其完整信号 dict + 算好的 reliable。"""
    pr_ctx = {"owner": "o", "repo": "r", "number": 1, "sha": "deadbeef",
              "diff": diff, "standards": _STANDARDS, "pr_agent_output": raw_review}
    out = orchestrator.review_pr(pr_ctx, contract or {}, _STANDARDS)
    out["reliable"] = RP.review_reliable(
        out["engine_status"], out["ai_raw_count"], out["added_lines"], engaged=out["engaged"])
    return out


def _dump(label, out):
    """统一打印本轮真 LLM 评审的信号 + 发现，供 -s 下肉眼诊断（LLM 行为不硬断，靠人看）。"""
    print(f"\n[{label}] engine={out['engine_status']} ai_raw={out['ai_raw_count']} "
          f"added={out['added_lines']} engaged={out['engaged']} reliable={out['reliable']} "
          f"risk={out['risk']['risk_band']} findings={len(out['findings'])}")
    for f in out["findings"]:
        print(f"  - {f.get('rule_id')} {f.get('category')} {f.get('agent')}: "
              f"{(f.get('rationale') or f.get('fix_direction') or '')[:80]}")


# ===========================================================================
# 场景
# ===========================================================================
@_skip
def test_llm_e2e_connectivity_and_yaml(tmp_path):
    """冒烟：LLM 可达且回复可解析成合法 review 结构——后续场景的前置。失败即时报环境问题。"""
    diff = _make_diff(tmp_path, {"a.py": "def f(x):\n    return x + 1\n"})
    raw = _llm_review(diff, "重点：是否有 bug")
    assert isinstance(raw, dict)
    assert isinstance(raw.get("review"), dict)          # review 段在
    assert isinstance(raw.get("code_suggestions"), list)


@_skip
def test_llm_e2e_clean_substantive_pr_not_stuck(tmp_path, capsys):
    """【核心】干净实质 PR（正确实现 + 测试）→ 真 LLM 审 → 必须 reliable，不得卡在 reliable=False。
    这正是 PR#52/engaged 修的「0意见→卡死」：干净 PR 的 0意见是「审完无问题」，靠 engaged 证明。"""
    diff = _make_diff(tmp_path, {
        "src/add.py": "def add(a, b):\n    return a + b\n",
        "tests/test_add.py": (
            "from src.add import add\n\n"
            "def test_add_positive():\n    assert add(2, 3) == 5\n\n"
            "def test_add_zero():\n    assert add(0, 0) == 0\n\n"
            "def test_add_negative():\n    assert add(-1, 1) == 0\n"),
    })
    raw = _llm_review(diff, "重点：是否有 bug、安全、明显坏味道（这应是个干净的加法工具）")
    out = _run_pipeline(diff, raw, {"intent": "add helper", "scope": ["src/**"],
                                    "acceptance_criteria": ["加法正确"]})
    _dump("clean", out)
    assert out["engine_status"] == "ok", "引擎应正常（注入路径不降级）"
    assert out["reliable"] is True, "干净 PR 被判不可靠——「0意见→卡死」回归！"


@_skip
def test_llm_e2e_obvious_defect_reviewed(tmp_path, capsys):
    """明显缺陷（除零未护 + 重复 helper）→ 真 LLM 审。不变式：不崩、reliable=True。
    是否抓到具体缺陷不强断（LLM 漏报固有），但打印 findings 供人诊断。"""
    diff = _make_diff(tmp_path, {
        "src/calc.py": (
            "def divide(a, b):\n    return a / b\n\n"      # 未护 b==0
            "def add_two(x, y):\n    return x + y\n"),      # 与内置 + 重复（坏味道）
        "lib/util.py": "def shared_add(x, y):\n    return x + y\n",
    })
    raw = _llm_review(diff, "重点：除零风险、是否有重复逻辑可复用")
    out = _run_pipeline(diff, raw)
    _dump("defect", out)
    assert out["engine_status"] == "ok"
    assert out["reliable"] is True


@_skip
def test_llm_e2e_tiny_diff_reliable_via_size(tmp_path, capsys):
    """极小改动（< SUSPICIOUS_EMPTY_LINES=20 行）→ 即便 LLM 无建议也 reliable（小改动 0 建议合理）。
    锁 review_reliable 的另一条路径（与 engaged 无关），防「小 PR 也被卡」。"""
    diff = _make_diff(tmp_path, {"a.py": "PI = 3.14159\n"})   # 1 行
    raw = _llm_review(diff, "重点：是否有问题")
    out = _run_pipeline(diff, raw)
    _dump("tiny", out)
    assert out["added_lines"] < 20, "本场景应为小改动"
    assert out["reliable"] is True


@_skip
def test_llm_e2e_large_diff_no_crash(tmp_path, capsys):
    """大 diff（~200 行）→ 下游不得崩：parse_diff / map_verdict / loop 全程合法。
    （diff 裁剪是 pr-agent 的活、本注入路径无；这里验 touchstone 下游对大改动的健壮性。）"""
    big = "\n".join(f"def func_{i}(x):  # variant {i}\n"
                    f"    acc = 0\n"
                    f"    for j in range(x):\n"
                    f"        acc += j * {i}\n"
                    f"    return acc\n" for i in range(40))   # ~200 行
    diff = _make_diff(tmp_path, {"src/generated.py": big})
    assert diff.count("\n+") > 150
    raw = _llm_review(diff, "重点：是否有明显 bug 或性能问题")
    out = _run_pipeline(diff, raw)
    _dump("large", out)
    assert out["risk"]["risk_band"] in ("low", "mid", "high")   # 不崩 + 合法分流
    assert isinstance(out["findings"], list)


@_skip
def test_llm_e2e_security_secret_surface(tmp_path, capsys):
    """硬编码凭据（安全味重）→ 真 LLM 审。不变式：不崩、reliable=True；打印是否被抓。
    已知 glm 对 security-heavy diff 可能内容过滤（见 memory glm-empty-response-pragent）——
    若被过滤，skip 并注明（已知端点限制，非 touchstone bug），绝不静默当通过。"""
    diff = _make_diff(tmp_path, {
        "src/config.py": (
            "AWS_ACCESS_KEY_ID = 'AKIAIOSFODNN7EXAMPLE'\n"
            "AWS_SECRET_ACCESS_KEY = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'\n"
            "DATABASE_URL = 'postgres://admin:s3cret-pwd@db:5432/prod'\n"),
    })
    try:
        raw = _llm_review(diff, "重点：是否有硬编码凭据/密钥泄露")
    except RuntimeError as e:
        msg = str(e)
        if "内容过滤" in msg or "BadRequest" in msg or "filter" in msg.lower():
            pytest.skip(f"glm 内容过滤了 security-heavy diff（已知端点限制，见 memory）：{msg[:120]}")
        raise
    out = _run_pipeline(diff, raw)
    _dump("security", out)
    sec = [f for f in out["findings"] if f.get("category") == "security"]
    print(f"  security_findings={len(sec)}")
    assert out["engine_status"] == "ok"
    assert out["reliable"] is True


@_skip
def test_llm_e2e_eval_bypass_empirical(tmp_path, capsys):
    """【经验探针·对应 test_adversarial 的 B2 绕过】非常规坏代码（.py 的 eval(user_input)）+ 空壳测试
    → 确定性侧完全漏（见 test_adversarial.test_CHARACTERIZES_nonjava_badeval_with_dummy_test_bypasses_to_low_risk）。
    本探针用真 LLM 经验性回答：glm 能否抓到这个 eval？——即该绕过在真实评审下有多可 exploit。
    不强断 glm 是否抓到（LLM 漏报固有、会 flaky）；只锁不变式（不崩、engine ok），打印 raw key_issues
    与 security findings 供人诊断真实严重度。若 glm 多数轮次空回 → 该绕过在真实链路中确可放行，
    需走联审加确定性兜底（见 B2 测试标注）。"""
    diff = _make_diff(tmp_path, {
        "src/utils/eval_helper.py": "def run(s):\n    return eval(s)  # 反射执行用户串\n",
        "tests/test_eval.py": "def test_noop():\n    pass\n",
    })
    try:
        raw = _llm_review(diff, "重点：是否有安全漏洞（代码注入风险）")
    except RuntimeError as e:
        msg = str(e)
        if "内容过滤" in msg or "BadRequest" in msg or "filter" in msg.lower():
            pytest.skip(f"glm 内容过滤（已知端点限制）：{msg[:120]}")
        raise
    out = _run_pipeline(diff, raw)
    _dump("eval-bypass", out)
    ki = (raw.get("review") or {}).get("key_issues_to_review") or []
    sec = [f for f in out["findings"] if f.get("category") == "security"]
    print(f"  raw_key_issues={len(ki)}: {[k.get('issue_header', '')[:50] for k in ki[:5]]}")
    print(f"  security_findings={len(sec)} risk={out['risk']['risk_band']} "
          f"reliable={out['reliable']}（reliable=True 且 0 security → 该绕过本轮确被放行）")
    assert out["engine_status"] == "ok"           # 不崩
    assert isinstance(out["findings"], list)


# ===========================================================================
# 真子进程模式（真 pr-agent + 真 LLM；需本地 .pragent-venv）
# ===========================================================================
def _touchstone_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(orchestrator.__file__)))


def _runner_cmd_prefix():
    """生产 runner 子进程命令前缀（[python, -m, touchstone.pr_agent_runner]）。
    优先 TOUCHSTONE_PRAGENT_CMD（与 review_provider 同约定），否则本地 .pragent-venv/bin/python。"""
    env_cmd = os.environ.get("TOUCHSTONE_PRAGENT_CMD", "").strip()
    if env_cmd:
        return shlex.split(env_cmd)
    return [os.path.join(_touchstone_root(), ".pragent-venv", "bin", "python"),
            "-m", "touchstone.pr_agent_runner"]


def _pragent_available():
    """能否在子进程 python 里 import pr_agent（venv 已装）。失败/超时 → False。"""
    prefix = _runner_cmd_prefix()
    if not prefix:
        return False
    try:
        return subprocess.run([prefix[0], "-c", "import pr_agent"],
                              capture_output=True, timeout=15).returncode == 0
    except Exception:
        return False


_skip_subproc = pytest.mark.skipif(
    not (_LLM_E2E and _pragent_available()),
    reason="需 TOUCHSTONE_LLM_E2E=1 且本地 pr-agent venv 可用"
           "（python -m venv .pragent-venv && .pragent-venv/bin/pip install pr-agent）")


def _write_file(repo, path, content):
    full = os.path.join(repo, path)
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(full, "w") as f:
        f.write(content)


def _run_local_subprocess(tmp_path, feature_files, base_files=None, mode="review"):
    """真 pr-agent 子进程 + 真 LLM：在 tmp_path 建本地仓（main 基线 + feature 含 constructed 改动，
    均已提交），用 LocalGitProvider（TOUCHSTONE_GIT_PROVIDER=local）跑【未改动的生产 runner】，
    返回 (raw_review, diff)。raw_review 含真 _engaged（runner 内 compute_engaged 算）；diff = main..feature
    供下游 orchestrator.review_pr。降级/超时/非零退出 → 抛错（loud，不静默）。"""
    repo = str(tmp_path)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    for path, content in (base_files or {}).items():
        _write_file(repo, path, content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-q", "-b", "feature")
    for path, content in feature_files.items():
        _write_file(repo, path, content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feature")
    diff = subprocess.run(["git", "-C", repo, "diff", "main...feature"],
                          capture_output=True, text=True).stdout
    key, base, model = _llm_conf()
    timeout = int(os.environ.get("TOUCHSTONE_PRAGENT_TIMEOUT", "600"))
    env = dict(os.environ)
    env.update({"LLM_API_KEY": key, "LLM_BASE_URL": base, "LLM_MODEL": model,
                "TOUCHSTONE_GIT_PROVIDER": "local",
                "PYTHONPATH": _touchstone_root(),
                "TOUCHSTONE_PRAGENT_TIMEOUT": str(timeout)})
    cmd = _runner_cmd_prefix() + ["--pr-url", "main", "--mode", mode]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=repo, env=env,
                              timeout=timeout + 30)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"runner 子进程超时（{e.timeout}s）——glm-5.2 单次 360s+，可调 TOUCHSTONE_PRAGENT_TIMEOUT") from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"runner 子进程非零退出({proc.returncode}) stderr:\n{(proc.stderr or '')[-800:]}")
    data = RP._extract_json(proc.stdout)    # 真 _extract_json：哨兵/raw_decode 提取
    if isinstance(data, dict) and data.get("_degraded"):
        raise RuntimeError(f"runner 降级（{data['_degraded']}）：{(data.get('reason') or '')[:200]}")
    return data, diff


@_skip_subproc
def test_llm_e2e_subproc_clean_engaged(tmp_path, capsys):
    """【真子进程·核心】干净 PR → 真 pr-agent review → key_issues 空 + engaged → reliable。
    全程真：真 pr-agent 工具/提示词 + 真 LocalGitProvider diff + 真 LLM + 真 compute_engaged（runner 内算）。
    这是「0意见→卡死」最忠实的端到端回归锁（仅 GitHub fetch 换成本地 diff）。"""
    raw, diff = _run_local_subprocess(tmp_path, {
        "src/add.py": "def add(a, b):\n    return a + b\n",
        "tests/test_add.py": "from src.add import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    })
    out = _run_pipeline(diff, raw, {"intent": "add helper", "scope": ["src/**"],
                                    "acceptance_criteria": ["加法正确"]})
    _dump("subproc-clean", out)
    ki = (raw.get("review") or {}).get("key_issues_to_review") or []
    print(f"  raw_key_issues={len(ki)} raw_engaged={raw.get('review', {}).get('_engaged')}")
    assert out["engine_status"] == "ok"
    assert out["reliable"] is True, "干净 PR 真子进程评审被判不可靠——0意见→卡死 回归！"


@_skip_subproc
def test_llm_e2e_subproc_defect_reviewed(tmp_path, capsys):
    """【真子进程】明显缺陷（除零未护）→ 真 pr-agent review 应抓到（glm 实验证实抓到 Division by Zero）。
    不变式：不崩、reliable=True。是否抓到不强断（防 LLM 漏报 flaky），打印 raw key_issues 供诊断。"""
    raw, diff = _run_local_subprocess(tmp_path, {
        "src/calc.py": "def divide(a, b):\n    return a / b\n",
    })
    out = _run_pipeline(diff, raw)
    _dump("subproc-defect", out)
    ki = (raw.get("review") or {}).get("key_issues_to_review") or []
    print(f"  raw_key_issues={len(ki)}: {[k.get('issue_header', '')[:40] for k in ki[:5]]}")
    assert out["engine_status"] == "ok"
    assert out["reliable"] is True


@_skip_subproc
def test_llm_e2e_subproc_large_no_crash(tmp_path, capsys):
    """【真子进程】大 diff（~200 行）→ 真 pr-agent review 不崩。注入模式覆盖不到的点：pr-agent 的
    diff token 裁剪（get_pr_diff）在子进程内真跑——本场景验它在真实大 diff 上不裁空/不崩。"""
    big = "\n".join(f"def func_{i}(x):\n    acc = 0\n    for j in range(x):\n"
                    f"        acc += j * {i}\n    return acc\n" for i in range(40))
    raw, diff = _run_local_subprocess(tmp_path, {"src/generated.py": big})
    out = _run_pipeline(diff, raw)
    _dump("subproc-large", out)
    assert out["risk"]["risk_band"] in ("low", "mid", "high")
    assert isinstance(out["findings"], list)
