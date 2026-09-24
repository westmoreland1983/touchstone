"""#防静默故障：LLM 出错时，review 不得伪装成"干净 / 可信 / 可收敛"。

本套端到端注入各 LLM 故障模式（子进程超时、坏 JSON、非零退出、`_degraded` 自报、
吞没失败、内容过滤、可疑空收敛），断言可靠性链
  engine_status → review_reliable → loop 收敛
不假收敛、不放行未评审代码。这是 Touchstone 最大风险类——LLM 随机性 / 内容过滤 / 子进程
边界出错时返回看似合法的空评审，被误判成"审完无问题"。

全部离线 mock（注入 seam `pr_agent_output` + `subprocess.run` 打桩），不触网、不真调 LLM、
进 CI。真 LLM 故障场景见 test_e2e_llm.py（按需跑）。
"""
import json
import os
import subprocess as _sp
import sys

import pytest

from touchstone import orchestrator as orc
from touchstone import review_provider as RP
from touchstone import loop as _lp
from helpers import build_diff


def _standards():
    import yaml
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return yaml.safe_load(open(os.path.join(root, ".touchstone", "standards.yaml"), encoding="utf-8"))


def _pr(diff_pairs, pr_agent_output="__omit__"):
    """最小 PR 上下文。diff_pairs: [(path, [lines], is_added), ...]；直接喂 build_diff。
    pr_agent_output="__omit__" 时不放该键（走子进程路径），否则走注入 seam。"""
    diff = build_diff(diff_pairs)
    pr = {"owner": "o", "repo": "r", "number": 1, "sha": "s", "token": "t", "diff": diff}
    if pr_agent_output != "__omit__":
        pr["pr_agent_output"] = pr_agent_output
    return pr


class _Proc:
    """subprocess.run 打桩返回体。"""
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


@pytest.fixture(autouse=True)
def _no_diff_limit(monkeypatch):
    """显式关闭 SIZE-001 体量门禁（防 skipped_large_diff 干扰引擎降级断言）。
    默认值已是 1000（size-gate-default-1000），这里 setenv 0 强制关，与默认值解耦、稳定隔离。"""
    monkeypatch.setenv("TOUCHSTONE_MAX_DIFF_LINES", "0")


# ============================================================================
# 性质：engaged 不救引擎降级（防假收敛的基石）
# ============================================================================
def test_reliable_engaged_does_not_rescue_engine_degradation():
    """引擎降级时，即便 engaged=True，review_reliable 仍 False。
    engaged 只放宽"可疑空收敛"，不救"引擎没真跑 / LLM 调用失败"——
    LLM 挂了 → 0 建议是缺审，非审完无问题。这是防静默故障的基石。"""
    for degraded in ("no_engine", "llm_failed", "provider_failed", "skipped_large_diff"):
        assert RP.review_reliable(degraded, 0, 200, engaged=True) is False
        assert RP.review_reliable(degraded, 5, 200, engaged=True) is False   # 有建议也救不回降级


def test_reliable_ok_engaged_clean_is_trusted():
    """对照：engine ok + 大改动 + 0 建议 + engaged（glm 真多段评审）= 审完无问题，可信。
    （PR #57 ed7e57b 真场景：effort/relevant_tests/security_concerns 三段、0 issues/suggestions。）"""
    assert RP.review_reliable("ok", 0, 200, engaged=True) is True


def test_reliable_suspicious_empty_when_not_engaged():
    """engine ok + 大改动(>=20) + 0 建议 + 未 engaged = 可疑空收敛（diff 可能被裁空），不可信。"""
    assert RP.review_reliable("ok", 0, 20, engaged=False) is False
    assert RP.review_reliable("ok", 0, 500, engaged=False) is False


def test_reliable_small_change_relaxes_and_findings_trust():
    """engine ok + 小改动(<20) + 0 建议 = 合理（无米下锅），可信；有原始建议则不论大小可信。"""
    assert RP.review_reliable("ok", 0, 19, engaged=False) is True
    assert RP.review_reliable("ok", 3, 5, engaged=False) is True
    assert RP.review_reliable("ok", 1, 200, engaged=False) is True


# ============================================================================
# compute_engaged：内部标志键不得灌水（本 PR 修复，锁死）
# ============================================================================
def test_compute_engaged_internal_flag_keys_do_not_inflate():
    """_engaged / _raw_excerpt 是 runner 注入的内部标志，非真评审段——不计入 engaged 段数。
    修复前：仅 _engaged=True 无真段 → 误判 True → 假 review_reliable → 假收敛。
    单一真源 _NONCONTENT_REVIEW_KEYS 同时服务 compute_engaged 与 extract_review_excerpt。"""
    assert RP.compute_engaged({"review": {"_engaged": True}}) is False
    assert RP.compute_engaged({"review": {"_engaged": True, "_raw_excerpt": {"x": "1"}}}) is False
    assert RP.compute_engaged({"review": {"estimated_effort_to_review": "2",
                                          "_raw_excerpt": {"a": "1"}}}) is False   # 仅 1 真段
    # 对照：2 个真段（不含任何内部键）→ True
    assert RP.compute_engaged({"review": {"estimated_effort_to_review": "2",
                                          "security_concerns": "No"}}) is True


# ============================================================================
# 端到端：子进程故障 → review_pr engine_status 降级 → review_reliable False
# ============================================================================
@pytest.mark.parametrize("rc,out,err,expected_status", [
    ("timeout", None, None, "llm_failed"),                              # 子进程超时
    (2, "", "boom-detail", "no_engine"),                                # 非零退出（适配器自身崩）
    (0, "not json at all", "", "no_engine"),                            # 坏 JSON（无哨兵、raw_decode 也失败）
])
def test_e2e_subprocess_hard_fault_degrades(monkeypatch, rc, out, err, expected_status):
    """子进程硬故障（超时 / 非零退出 / 坏 JSON）→ engine_status 降级 → 不可信。"""
    def fake_run(*a, **k):
        if rc == "timeout":
            raise _sp.TimeoutExpired(cmd="pr-agent", timeout=1)
        return _Proc(rc, out=out, err=err)
    monkeypatch.setattr(RP.subprocess, "run", fake_run)
    pr = _pr([("src/Main.java", ["public class Main { int x; }"], True)])
    out_dict = orc.review_pr(pr, {}, _standards())
    assert out_dict["engine_status"] == expected_status
    assert RP.review_reliable(out_dict["engine_status"], out_dict["ai_raw_count"],
                              out_dict["added_lines"], out_dict["engaged"]) is False


def test_e2e_degraded_field_self_report_degrades(monkeypatch):
    """runner 自报 _degraded 字段（如 LLM AuthError / 内容过滤被 runner 捕获）→ 降级。"""
    payload = json.dumps({"_degraded": "llm_failed", "reason": "AuthError: 401",
                          "code_suggestions": [], "review": {"key_issues_to_review": []}})
    monkeypatch.setattr(RP.subprocess, "run", lambda *a, **k: _Proc(0, out=payload, err=""))
    pr = _pr([("src/Main.java", ["public class Main { int x; }"], True)])
    out = orc.review_pr(pr, {}, _standards())
    assert out["engine_status"] == "llm_failed"
    assert "AuthError" in out["engine_detail"]      # 具体原因被留进返回，供渲染层详列
    assert RP.review_reliable(out["engine_status"], 0, out["added_lines"], out["engaged"]) is False


def test_unreliable_callout_concise_names_stage():
    """CAUTION 精简（两行）：点明失败环节 + 指向原始错误折叠块（v3：并入告警区，不再是「验证与日志」段）。"""
    from touchstone import render
    detail = "improve 工具 LLM 调用失败：litellm.Timeout——stderr 失败相关行：\nError during LLM inference: ..."
    box = render.render_unreliable_callout("llm_failed", ai_raw_count=0, added_lines=50,
                                           engine_detail=detail)
    assert "本轮 AI 评审不可信" in box and "LLM 调用失败" in box   # 点明环节
    assert "下方折叠块" in box and "验证与日志" not in box         # 指向 v3 告警区折叠块
    assert "litellm.Timeout" not in box and "Error during LLM inference" not in box  # 原始 dump 不塞进精简框
    assert box.count("\n") <= 3                                   # 两行正文（含 [!CAUTION] 头）


# ---------------- engine_detail 进公开 PR 评论前的脱敏/围栏/截断（PRA-* PR #74）----------------
def test_redact_secrets_strips_credentials_unit():
    """_redact_secrets 抹凭据形子串（Bearer/sk-/gh_/api_key），保留错误正文/traceback 可读。"""
    raw = ("请求失败：Authorization: Bearer sk-abcdef0123456789abcdefXYZ，"
           "ghp_0123456789abcdef0123456789abcdef0123，api_key=AKIAIOSFODNN7EXAMPLE0123，"
           "Error during LLM inference: ConnectionError（正文，应保留）")
    red = orc._redact_secrets(raw)
    assert "sk-abcdef" not in red and "ghp_0123" not in red and "AKIAIOSF" not in red
    assert "***REDACTED***" in red
    assert "Error during LLM inference" in red and "ConnectionError" in red   # 正文/traceback 保留


def test_render_engine_detail_redacts_fences_truncates():
    """降级原始错误折叠块（v3：原「验证与日志」段并入②告警区，orchestrator._render_engine_detail
    退役）：脱敏仍在 orchestrator 呈现边界（post_results 先 _redact_secrets，本测直接喂脱敏后串）；
    本层负责 HTML 转义 + <pre> 包裹（<details> 内 markdown 围栏不解析）+ 超长截断标记 +
    空行折叠（空行会终止 type-6 HTML block，#168 同源）。"""
    from touchstone import render
    # 已脱敏的 raw error：含三反引号（旧 markdown 围栏会被提前闭合）+ HTML 敏感字符 + 超长
    blob = ("Bearer ***REDACTED***\n```python\n<b>Traceback</b> (most recent call last):\n```\n\n\n"
            + "x" * 2000)
    block = render._engine_detail_fold("llm_failed", blob)
    assert block.startswith("<details><summary>评审引擎降级（llm_failed）")   # 降级 + 有 detail → 出折叠块
    assert "<pre>" in block and "</pre>" in block                # <pre> 包裹（非 markdown 围栏）
    assert "&lt;b&gt;Traceback&lt;/b&gt;" in block               # HTML 转义（防注入/防吞段）
    assert "已截断" in block                                     # 截断标记
    assert "pr-agent-interaction.log" in block                   # llm_failed：子进程真跑过，指向交互日志
    # PRA-REVIEW round-3：no_engine/provider_failed 下 PR-Agent 没起、该 artifact 不存在——
    # 指过去是死链。截断指针按状态切换到 job 运行日志，不再误导。
    block_ne = render._engine_detail_fold("no_engine", blob)
    assert "pr-agent-interaction.log" not in block_ne and "本 job 运行日志" in block_ne
    # engine 正常 / 无 detail → 不出块
    assert render._engine_detail_fold("ok", blob) == ""
    assert render._engine_detail_fold("llm_failed", "") == ""
    # 短 detail 不加截断标记、内容保留
    short = render._engine_detail_fold("provider_failed", "短错误：连接超时")
    assert "已截断" not in short and "短错误" in short


def test_e2e_swallowed_failure_degrades(monkeypatch):
    """吞没失败：退出码 0、无 _degraded 字段、但 stderr 含失败签名 + 本轮 0 原始建议
    （LLM 空 content 被 retry 吞、run() 再吞）→ 命中 prediction_swallowed_failure → llm_failed。
    这是最阴险的静默故障：表面"正常返回 0 建议"，实为 LLM 失败。"""
    payload = json.dumps({"code_suggestions": [], "review": {"key_issues_to_review": []}})
    err = "...Failed to generate prediction with any model...\n"
    monkeypatch.setattr(RP.subprocess, "run", lambda *a, **k: _Proc(0, out=payload, err=err))
    pr = _pr([("src/Main.java", ["public class Main { int x; }"], True)])
    out = orc.review_pr(pr, {}, _standards())
    assert out["engine_status"] == "llm_failed"
    assert RP.review_reliable(out["engine_status"], 0, out["added_lines"], out["engaged"]) is False


def test_e2e_litellm_stdout_pollution_does_not_degrade(monkeypatch):
    """对照：litellm 延迟打印 'Logging Details LiteLLM-Async Success Call' 污染 stdout
    （PR #49 真根因）→ 哨兵/raw_decode 必须救回 → engine ok（不误降级）。"""
    payload = json.dumps({"code_suggestions": [], "review": {"key_issues_to_review": []}})
    noisy = payload + "Logging Details LiteLLM-Async Success Call, cache_hit=None"
    monkeypatch.setattr(RP.subprocess, "run", lambda *a, **k: _Proc(0, out=noisy, err=""))
    pr = _pr([("src/Main.java", ["public class Main { int x; }"], True)])
    out = orc.review_pr(pr, {}, _standards())
    assert out["engine_status"] == "ok"


# ============================================================================
# 注入 seam：数据级故障 / 正当干净评审
# ============================================================================
def _big_diff_pairs():
    """>=20 行新增（触发可疑空收敛阈值的"大改动"）。"""
    return [("src/Big.java", [f"int v{i} = {i};" for i in range(25)], True)]


def test_e2e_suspicious_empty_review_not_reliable():
    """大改动 + LLM 返回空 review（无任何结构段、not engaged）+ 0 建议 → 可疑空收敛，不可信。
    （模拟 diff 被 pr-agent 裁空 → glm 无米下锅 → 近乎空 review。）"""
    pr = _pr(_big_diff_pairs(), {"code_suggestions": [], "review": {"key_issues_to_review": []}})
    out = orc.review_pr(pr, {}, _standards())
    assert out["engine_status"] == "ok"
    assert out["ai_raw_count"] == 0
    assert out["engaged"] is False                       # 无真段
    assert out["added_lines"] >= 20
    assert RP.review_reliable(out["engine_status"], 0, out["added_lines"], out["engaged"]) is False


def test_e2e_legitimate_engaged_clean_is_reliable():
    """对照：大改动 + 0 建议 + glm 真多段评审（engaged）= 审完无问题，可信。
    锁正当路径——防我们把"engaged 干净"误伤成可疑空收敛。"""
    pr = _pr(_big_diff_pairs(), {"code_suggestions": [], "review": {
        "estimated_effort_to_review": "2", "relevant_tests": "Yes",
        "key_issues_to_review": [], "security_concerns": "No"}})
    out = orc.review_pr(pr, {}, _standards())
    assert out["engine_status"] == "ok"
    assert out["engaged"] is True
    assert RP.review_reliable(out["engine_status"], 0, out["added_lines"], out["engaged"]) is True


# ============================================================================
# loop：不可靠评审 → 不收敛（可靠性链的终点闸）
# ============================================================================
def test_loop_withholds_convergence_when_review_unreliable(rule_index):
    """review_reliable=False 时，即便无可自改发现、CI 绿，loop 也不收敛——
    "0 发现"在 diff 被裁空 / LLM 随机性下不可靠，回落 continue 待可靠轮复核。
    （PR #44 round-1 真根因兜底：首轮 diff 被裁空 → 0 发现 → 此处阻止假收敛。）"""
    dec, reason, _ = _lp.loop_step([], rule_index, _lp.LoopState(),
                                   ci_passed=True, review_reliable=False)
    assert dec != "converged"
    # 对照：可靠 + 无发现 + CI 绿 → 收敛
    dec_ok, _, _ = _lp.loop_step([], rule_index, _lp.LoopState(),
                                 ci_passed=True, review_reliable=True)
    assert dec_ok == "converged"


# ============================================================================
# 端到端：引擎故障（llm_failed 等）→ engine_failed → 轮次不消耗
# 锁 orchestrator 的接线表达式：engine_status in loop.INFRA_FAILURE_STATUSES
# （PR #183 实录：4 轮 llm_failed 白烧 4 轮预算——故障不该由作者买单）。
# ============================================================================
@pytest.mark.parametrize("fault_rc,fault_out,expected_status", [
    ("timeout", None, "llm_failed"),                          # 子进程超时 → LLM 调用失败
    (2, "", "no_engine"),                                     # 适配器自身崩
])
def test_e2e_engine_failure_does_not_burn_round(monkeypatch, rule_index,
                                                fault_rc, fault_out, expected_status):
    """review_pr 降级出引擎级故障状态 → 按 orchestrator 同款接线判 engine_failed →
    loop_step 本轮 round 冻结、不追 history、continue（待端点恢复复核）。"""
    from touchstone import loop as _loop_mod

    def fake_run(*a, **k):
        if fault_rc == "timeout":
            raise _sp.TimeoutExpired(cmd="pr-agent", timeout=1)
        return _Proc(fault_rc, out=fault_out, err="")

    monkeypatch.setattr(RP.subprocess, "run", fake_run)
    pr = _pr([("src/Main.java", ["public class Main { int x; }"], True)])
    out = orc.review_pr(pr, {}, _standards())
    assert out["engine_status"] == expected_status
    # 与 orchestrator.post_results 完全相同的接线表达式（防只改 loop 忘接线的半截修复）
    engine_failed = out["engine_status"] in _loop_mod.INFRA_FAILURE_STATUSES
    assert engine_failed is True
    st = _loop_mod.LoopState(round=4, history=[["OE-001:f:1"]])
    dec, reason, ns = _loop_mod.loop_step(out["findings"], rule_index, st,
                                          engine_failed=engine_failed)
    assert dec == "continue" and "不计入轮次" in reason
    assert ns.round == 4 and ns.history == [["OE-001:f:1"]]


# ============================================================================
# issue #211：编排器不得「崩溃 + 绿灯」——摘要评论契约收口 + 顶层看门狗
# ============================================================================
_RISK211 = {"risk_band": "low", "human_action": "skip",
            "verification_decision": "cheap_only", "blast_radius": []}
_DIFF211 = "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,1 @@\n+a\n"
_F211 = [{"rule_id": "R1", "confidence": 0.9, "agent": "pr-agent:review",
          "file": "x.py", "line": 1, "rationale": "r", "suggested_fix": "fix"}]


def test_summary_comment_failure_raises(monkeypatch):
    """issue #211 主症状：摘要评论 POST 失败（secondary rate limit 403 等）→ post_results
    大声失败，不再 [warn] 吞掉后绿灯收场（绿灯 job 里 stderr 一行不可见 = 静默）。"""
    def _gh(method, path, token, data=None, accept=""):
        if method == "POST" and path.endswith("/issues/1/comments"):
            import requests as _rq
            raise _rq.exceptions.HTTPError("403 secondary rate limit")
        return {}
    monkeypatch.setattr(orc, "gh", _gh)
    with pytest.raises(RuntimeError, match="摘要评论未发布"):
        orc.post_results("o", "r", 1, "sha", "tok", _RISK211, [], diff=_DIFF211)


def test_summary_comment_failure_still_attempts_inline_and_checkrun(monkeypatch):
    """失败后内联评论/check-run 仍尽力而为（诊断面留档），再统一转大声——顺序契约。"""
    calls = []

    def _gh(method, path, token, data=None, accept=""):
        calls.append(path)
        if method == "POST" and path.endswith("/issues/1/comments"):
            import requests as _rq
            raise _rq.exceptions.ConnectionError("boom")
        return {}
    monkeypatch.setattr(orc, "gh", _gh)
    with pytest.raises(RuntimeError):
        orc.post_results("o", "r", 1, "sha", "tok", _RISK211, _F211, diff=_DIFF211)
    joined = " ".join(calls)
    assert "/pulls/1/reviews" in joined and "/check-runs" in joined   # 尽力而为先于大声失败


def test_summary_comment_success_no_raise(monkeypatch):
    """正常路径不受影响：评论落地 → 不 raise（回归护栏，防收口误伤）。"""
    monkeypatch.setattr(orc, "gh", lambda m, p, t, data=None, accept="": {})
    orc.post_results("o", "r", 1, "sha", "tok", _RISK211, _F211, diff=_DIFF211)   # 不抛即通过


def test_watchdog_converts_silent_exit0_to_loud(monkeypatch):
    """SystemExit(0/None) = 无 traceback 的静默成功退出（穿透一切 except Exception）——
    看门狗必须转成 exit 1 + 降级评论（fail-closed：完成只能由 main() 走到底来宣告）。"""
    seen = []
    monkeypatch.setattr(orc, "_emergency_comment", seen.append)
    monkeypatch.setattr(orc, "main", lambda: sys.exit(0))
    with pytest.raises(SystemExit) as ei:
        orc._run_with_watchdog()
    assert ei.value.code == 1
    assert seen and "SystemExit(0)" in seen[0]


def test_watchdog_exit_none_same_as_zero(monkeypatch):
    monkeypatch.setattr(orc, "_emergency_comment", lambda r: None)
    monkeypatch.setattr(orc, "main", lambda: sys.exit())
    with pytest.raises(SystemExit) as ei:
        orc._run_with_watchdog()
    assert ei.value.code == 1


def test_watchdog_intentional_string_exit_untouched(monkeypatch):
    """主动跳过路径（非 PR 事件 / 规范缺失，string-arg sys.exit）本就大声——原样放行，
    不贴降级评论（没有丢失评审结果，只是没开审）。"""
    monkeypatch.setattr(orc, "_emergency_comment",
                        lambda r: pytest.fail("主动跳过不应贴降级评论"))
    monkeypatch.setattr(orc, "main", lambda: sys.exit("非 PR 事件，跳过。"))
    with pytest.raises(SystemExit) as ei:
        orc._run_with_watchdog()
    assert ei.value.code == "非 PR 事件，跳过。"


def test_watchdog_uncaught_exception_comments_and_exits_1(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(orc, "_emergency_comment", seen.append)

    def _main():
        raise RuntimeError("boom")
    monkeypatch.setattr(orc, "main", _main)
    with pytest.raises(SystemExit) as ei:
        orc._run_with_watchdog()
    assert ei.value.code == 1
    assert seen and "RuntimeError" in seen[0] and "boom" in seen[0]
    assert "RuntimeError" in capsys.readouterr().err          # traceback 留档 stderr


def test_watchdog_keyboardinterrupt_passes_through(monkeypatch):
    """concurrency 取消（SIGINT）不贴降级噪音：新轮自会接管该 SHA，取消语义归平台。"""
    monkeypatch.setattr(orc, "_emergency_comment",
                        lambda r: pytest.fail("平台取消不应贴降级评论"))
    monkeypatch.setattr(orc, "main", _raise_ki)
    with pytest.raises(KeyboardInterrupt):
        orc._run_with_watchdog()


def _raise_ki():
    raise KeyboardInterrupt()


def test_emergency_comment_posts_without_result_marker(monkeypatch, tmp_path):
    """降级评论贴到 PR；携带 touchstone-incomplete 而非 touchstone-result marker
    （后者是销项循环的完整轮记账入口——残缺状态不得被当完整轮）。"""
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps({"pull_request": {"number": 7}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(ev))
    posted = {}
    monkeypatch.setattr(orc, "gh",
                        lambda m, p, t, data=None, accept="": posted.update(path=p, data=data))
    orc._emergency_comment("未捕获异常 RuntimeError: boom")
    assert posted["path"] == "/repos/o/r/issues/7/comments"
    body = posted["data"]["body"]
    assert "本轮评审未完成" in body and "boom" in body
    assert "touchstone-incomplete" in body
    assert "touchstone-result" not in body


def test_emergency_comment_no_context_skips_quietly(monkeypatch, capsys):
    """PR 上下文不完整（非 PR 事件/缺 token）→ 不触网不抛，stderr 留痕（看门狗自身
    绝不压过原始退出语义）。"""
    for k in ("GITHUB_REPOSITORY", "GITHUB_TOKEN", "GITHUB_EVENT_PATH"):
        monkeypatch.delenv(k, raising=False)
    called = []
    monkeypatch.setattr(orc, "gh", lambda *a, **k: called.append(1) or {})
    orc._emergency_comment("x")                                   # 不抛即通过
    assert not called
    assert "跳过降级评论" in capsys.readouterr().err


def test_emergency_comment_post_failure_never_raises(monkeypatch, tmp_path, capsys):
    """评论 POST 也挂（API 大面积故障）→ 留痕后返回；退出码仍非零（看门狗主路径保证）。"""
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps({"pull_request": {"number": 7}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(ev))

    def _gh(m, p, t, data=None, accept=""):
        import requests as _rq
        raise _rq.exceptions.ConnectionError("api down")
    monkeypatch.setattr(orc, "gh", _gh)
    orc._emergency_comment("reason")                              # 不抛即通过
    assert "降级评论发布失败" in capsys.readouterr().err
