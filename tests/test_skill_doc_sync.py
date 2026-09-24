"""skill 文档与实现不漂移：SKILL.md 里的示例与语义必须被真实解析器认可。
SKILL.md 是销项协议的权威文档（agent 照它办事）；它说谎 = agent 按错误协议申报。
本测试把文档里的 ack 示例喂给 parse_acks/reconcile，断言文档描述的行为成立。"""
import os
import re

from touchstone import checklist as ck


def _skill_path():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "skills", "touchstone-ack", "SKILL.md")


def _skill_text():
    return open(_skill_path(), encoding="utf-8").read()


def test_skill_file_exists_with_frontmatter():
    m = re.match(r"^---\n(.*?)\n---\n", _skill_text(), re.S)
    assert m, "SKILL.md 缺 frontmatter（name/description）"
    assert "name: touchstone-ack" in m.group(1)
    assert "description:" in m.group(1)


def test_skill_ack_examples_parse_with_real_parser():
    """文档示例 ack 块必须被 _ACK_BLOCK/_ACK_LINE 真实解析（动词 done/waived/split、
    note 非空）——示例与解析器漂移时此处红，文档即失去权威性。"""
    text = _skill_text()
    # 文档里示例在四反引号围栏内嵌三反引号块；取所有 touchstone-ack 围栏块
    blocks = re.findall(r"```touchstone-ack\s*\n(.*?)```", text, re.S)
    assert blocks, "SKILL.md 未包含 touchstone-ack 示例块"
    acks = ck.parse_acks(["```touchstone-ack\n" + blocks[0] + "```"])
    assert any(v["verb"] == "done" for v in acks.values()), "示例须含 done"
    assert any(v["verb"] == "waived" for v in acks.values()), "示例须含 waived"
    assert any(v["verb"] == "split" for v in acks.values()), "示例须含 split"
    for sig, v in acks.items():
        assert v["note"], f"{sig} 的示例须带 note（waived 无理由不受理）"


def test_skill_semantics_match_implementation():
    """文档语义表与 VERIFIED/CLAIMED 集合一致（防文档宣称与销项判据加固脱节）。"""
    text = _skill_text()
    assert "done" in ck.VERIFIED
    assert ck.CLAIMED == {"waived", "split"}
    assert "`done`" in text and "`waived`" in text and "`split`" in text
    # 文档必须写明 done 的复检语义（下轮签名不再命中才落 done）
    assert "复检" in text
    # 反博弈语义必须写明（全 waived 不触发收敛）
    assert "不触发收敛" in text or "不阻塞收敛" in text


def test_skill_inline_comments_not_counted_is_stated():
    """核心事实（PR 级评论计数、行内线程不计数）必须出现在文档开头。"""
    head = _skill_text()[:1500]
    assert "行内" in head and "PR 级评论" in head


def test_pr_template_names_the_two_reads():
    """PR 模板（创建时触点）防漂移：必须点名「两读」——.touchstone/pr.yaml 必填三件
    （intent/acceptance_criteria/scope，与 pr.yaml 头声明同源）与 CLAUDE.md（写代码规矩），
    并指向销项 skill。agent/人在开 PR 的编辑器里读到原始 body 即被提醒。"""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tpl = open(os.path.join(root, ".github", "PULL_REQUEST_TEMPLATE.md"), encoding="utf-8").read()
    for key in (".touchstone/pr.yaml", "intent", "acceptance_criteria", "scope",
                "CLAUDE.md", "SCOPE-001",
                "https://github.com/AKDI-SE/touchstone/blob/main/skills/touchstone-ack/SKILL.md"):
        assert key in tpl, f"PR 模板缺关键提醒：{key}"
    assert "仓内 skills" not in tpl        # 受评仓（开发者代码仓）没有该路径——不得引用


def test_contract_findings_point_to_pr_yaml():
    """契约发现（违规时触点）统一带正本指针：agent 收到 SCOPE/TEST/DUP 类发现时
    知道修的方向在 .touchstone/pr.yaml（scope/tests_added/reused_components）。"""
    from touchstone import contract_check as cc
    ri = {"SCOPE-001": {"severity": "warn"}}
    (f,) = cc.check_scope(["src/x.py"], ["docs/**"], ri)
    assert "改动文件不在提交契约声明的 scope 内" in f["rationale"]
    assert f["fix_direction"].endswith("（契约正本 .touchstone/pr.yaml）")


def test_render_digest_phrases_exist_in_skill_doc():
    """防漂移（双向）：render._ack_skill_ref 的「要点速览」与 SKILL.md 正本同源——
    速览里的关键措辞必须在正本中逐字存在。改 SKILL.md 口径而忘改速览（或反之）
    在此红，避免评论里的速览悄悄变成第二套规则。"""
    from touchstone import render
    digest = render._ack_skill_ref().replace("<br>\n", "\n")
    skill = _skill_text()
    for phrase in ("改码 → 提交 → 发 ack 评论（并空提交触发评审） → 推送",
                   "行内评论线程一律不计数",
                   "空提交",
                   "每 1 分钟检查一次",        # ⑤ 多轮轮询节奏（2026-08-29 指定 2 分钟；2026-09-10 收紧为 1 分钟）
                   "直到 ✅ 收敛",
                   "本地全量测试不必每轮跑",   # ⑥ 全量测试降频（用户 2026-09-10：首轮前 + findings 清零后各一次）
                   "findings 清零后"):
        assert phrase in skill, f"SKILL.md 缺逐字措辞：{phrase}"
        assert phrase in digest, f"渲染速览缺逐字措辞：{phrase}"


def test_version_bump_has_changelog_section():
    """release 纪律（防漂移）：__version__ 必须有对应的 CHANGELOG 小节——
    bump 版本忘写 changelog 的 release PR 在此红；changelog 先行而版本号
    忘 bump 同样红。非恒真断言：任一侧独走即挂。"""
    import os
    from touchstone import __version__
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    changelog = open(os.path.join(root, "CHANGELOG.md"), encoding="utf-8").read()
    assert f"## [{__version__}]" in changelog, (
        f"CHANGELOG 缺 [{__version__}] 小节——版本号已 bump 但未记录发布内容")
    assert "## [未发布]" in changelog, "CHANGELOG 缺「未发布」占位段（新变更的记入口）"
