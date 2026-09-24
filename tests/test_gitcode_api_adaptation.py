# ============================================================================
# tests/test_gitcode_api_adaptation.py —— GitCode 平台适配的离线单测
#
# 数据依据（勿删——这些形状不是猜的，是 2026-09-24 在 westmoreland/touchstone
# 探针 PR 上实测的 GitCode v5 API 真实响应，add/modify/delete/rename 四态全取过样）：
#   • /pulls/{n}/files 的 patch 是嵌套 dict（GitLab 系 diff 结构）：
#       {diff, old_path, new_path, a_mode, b_mode, new_file, renamed_file, deleted_file}
#     ——路径/布尔字段全在 patch【内】；文件级只有 filename/status/additions/deletions/
#     sha/blob_url/raw_url。
#   • file.status 词汇：新增='added'；删除='deleted'（GitLab 风格，pr-agent 只认
#     'removed'）；修改=【键缺失】→ PyGithub(1.59.1) 返回 None。
#   • 标签：POST /pulls/{n}/labels + 纯 JSON 数组体 → 201 追加；POST /issues/{n}/labels
#     对 PR 是 404；{"labels":[...]} 对象体两端点均 400 且【未】加签。
#   • 评论编辑：PATCH /pulls/comments/{id} JSON 体 200 且落库（id 与
#     /pulls/{n}/comments 列表同命名空间）。
# 全部离线：不发网络请求，gh/requests/ghclient 均经 monkeypatch 替身。
# ============================================================================
import base64

import pytest
import requests

from touchstone import orchestrator as O
from touchstone import pr_agent_runner as R
from touchstone import contract_check


# ---- 实测样本（探针 PR 原样摘录，字段有裁剪但层级/取值保真）--------------------

_ADDED_FILE = {                                   # 探针 probe_added.txt
    "sha": "a96dc927", "filename": "probe_added.txt", "status": "added",
    "additions": 2, "deletions": 0,
    "patch": {"diff": "@@ -0,0 +1,2 @@\n+line1\n+line2\n",
              "old_path": "probe_added.txt", "new_path": "probe_added.txt",
              "a_mode": "0", "b_mode": "100644",
              "new_file": True, "renamed_file": False, "deleted_file": False},
}

_MODIFIED_FILE = {                                # 探针 README.md（注意：无 status 键）
    "sha": "b6f31c2", "filename": "README.md", "additions": 194, "deletions": 191,
    "patch": {"diff": "@@ -1,2 +1,2 @@\n A\n-B\n+C\n",
              "old_path": "README.md", "new_path": "README.md",
              "a_mode": "100644", "b_mode": "100644",
              "new_file": False, "renamed_file": False, "deleted_file": False},
}

_DELETED_FILE = {                                 # 探针 CHANGELOG.md
    "sha": "c7e42d9", "filename": "CHANGELOG.md", "status": "deleted",
    "additions": 0, "deletions": 346,
    "patch": {"diff": "@@ -1,2 +0,0 @@\n-old1\n-old2\n",
              "old_path": "CHANGELOG.md", "new_path": "CHANGELOG.md",
              "a_mode": "100644", "b_mode": "0",
              "new_file": False, "renamed_file": False, "deleted_file": True},
}


# ---- File.status 归一/推断（pr_agent_runner._gitcode_infer_status）--------------

def test_status_deleted_normalized_to_removed():
    # 实测删除文件 status='deleted'；pr-agent 只认 'removed'，透传必现
    # "Unknown edit type: deleted" → EDIT_TYPE.UNKNOWN。本用例锁死归一行为。
    assert R._gitcode_infer_status("deleted", {}) == "removed"


def test_status_added_passthrough():
    assert R._gitcode_infer_status("added", {"patch": _ADDED_FILE["patch"]}) == "added"


def test_status_missing_modified_file_falls_back_to_modified():
    # 实测修改文件 status 键缺失（PyGithub NotSet.value → None），布尔全 False
    assert R._gitcode_infer_status(None, _MODIFIED_FILE) == "modified"


def test_status_missing_infers_from_patch_booleans():
    # 布尔在 patch dict 内（实测）；status 缺失时按布尔推断
    assert R._gitcode_infer_status(None, _ADDED_FILE) == "added"
    assert R._gitcode_infer_status(None, _DELETED_FILE) == "removed"
    assert R._gitcode_infer_status(
        None, {"patch": {"renamed_file": True, "new_file": False}}) == "renamed"


def test_status_gitee_vocabulary_defensive_mapping():
    # Gitee 系 'updated'/'update' 未在 GitCode 实测到——纯防御映射，未见即零影响
    assert R._gitcode_infer_status("updated", {}) == "modified"
    assert R._gitcode_infer_status("Update", {}) == "modified"


def test_status_github_string_patch_still_modified():
    # GitHub/Gitee 老形态：patch 是纯字符串（无 dict 布尔可推断）→ 兜底 modified
    assert R._gitcode_infer_status(None, {"patch": "@@ -1 +1 @@\n-a\n+b\n"}) == "modified"


def test_status_all_missing_fallback_modified():
    assert R._gitcode_infer_status(None, None) == "modified"
    assert R._gitcode_infer_status(None, {"filename": "x"}) == "modified"


# ---- _gitcode_files_to_diff（orchestrator）------------------------------------

def test_gitcode_files_to_diff_headers_per_edit_type():
    diff = O._gitcode_files_to_diff([_ADDED_FILE, _MODIFIED_FILE, _DELETED_FILE])
    # 新增：旧侧 /dev/null（实测 new_file=True / a_mode="0"）
    assert "diff --git a/probe_added.txt b/probe_added.txt" in diff
    assert "--- /dev/null" in diff
    assert "+++ b/probe_added.txt" in diff
    # 修改：旧实现恒拼 /dev/null（文件级 old_path 恒 None）——现在必须 a/README.md
    assert "--- a/README.md" in diff
    assert "+++ b/README.md" in diff
    # 删除：新侧 /dev/null（实测 deleted_file=True / b_mode="0"）
    assert "--- a/CHANGELOG.md" in diff
    assert "+++ /dev/null" in diff


def test_gitcode_files_to_diff_unidiff_parses_and_matches_github_semantics():
    diff = O._gitcode_files_to_diff([_ADDED_FILE, _MODIFIED_FILE, _DELETED_FILE])
    files, added = contract_check.parse_diff(diff)
    # GitHub 路径语义：删除文件（+++ /dev/null）不入 changed_files——两平台一致性
    assert files == {"probe_added.txt", "README.md"}
    assert added["probe_added.txt"] == [(1, "line1"), (2, "line2")]
    assert added["README.md"] == [(2, "C")]
    assert contract_check._PARSE_WARNING is None     # unidiff 全量可解析


def test_gitcode_files_to_diff_string_patch_compat():
    # Gitee 老形态：patch 为纯字符串、路径在文件级——不得回归
    f = {"filename": "x.py", "old_path": "x.py", "new_path": "x.py",
         "new_file": False, "deleted_file": False,
         "patch": "@@ -1 +1 @@\n-a\n+b\n"}
    diff = O._gitcode_files_to_diff([f])
    assert "--- a/x.py" in diff and "+++ b/x.py" in diff
    files, added = contract_check.parse_diff(diff)
    assert files == {"x.py"} and added["x.py"] == [(1, "b")]


def test_gitcode_files_to_diff_mode_zero_alone_marks_new_and_deleted():
    # 布尔缺失但 a_mode/b_mode=="0"（实测模式位）仍应判新/删——防御路径
    f_new = {"filename": "n.txt", "patch": {"diff": "@@ -0,0 +1 @@\n+x\n", "a_mode": "0"}}
    f_del = {"filename": "d.txt", "patch": {"diff": "@@ -1 +0,0 @@\n-x\n", "b_mode": "0"}}
    diff = O._gitcode_files_to_diff([f_new, f_del])
    assert diff.index("--- /dev/null") < diff.index("+++ b/n.txt")
    assert "+++ /dev/null" in diff


# ---- escalate 标签（orchestrator._post_escalate_label / _label_names）----------

def _gh_gitcode_off(monkeypatch):
    for k in ("TOUCHSTONE_PLATFORM", "GITHUB_API_URL", "TOUCHSTONE_GITHUB_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def test_post_escalate_label_gitcode_uses_pulls_labels_with_array_body(monkeypatch):
    # 实测正确调用：POST /pulls/{n}/labels + 纯 JSON 数组体（追加语义，不覆盖既有标签）
    monkeypatch.setenv("TOUCHSTONE_PLATFORM", "gitcode")
    calls = []

    def fake_gh(method, path, token, data=None, accept=None):
        calls.append((method, path, data))
        if method == "POST":
            return [{"name": "touchstone:needs-human"}]
        return {"labels": [{"id": 1, "name": "touchstone:needs-human"}]}

    monkeypatch.setattr(O, "gh", fake_gh)
    O._post_escalate_label("o", "r", 9, "t")
    assert calls[0] == ("POST", "/repos/o/r/pulls/9/labels", ["touchstone:needs-human"])
    assert calls[1][:2] == ("GET", "/repos/o/r/pulls/9")     # 追加核验（防端点行为漂移）


def test_post_escalate_label_gitcode_accepts_string_labels(monkeypatch):
    # GitCode 旧版本出现过纯字符串标签列表（gitcode-adaptation round-8）——
    # 核验只认 dict 会把"已打上"误判为"缺失"（本 PR 修复的回归点）
    monkeypatch.setenv("TOUCHSTONE_PLATFORM", "gitcode")
    monkeypatch.setattr(O, "gh", lambda m, p, t, data=None, accept=None:
                        [{"name": "x"}] if m == "POST"
                        else {"labels": ["touchstone:needs-human"]})
    O._post_escalate_label("o", "r", 9, "t")     # 不抛即通过


def test_post_escalate_label_gitcode_raises_when_label_missing_after_post(monkeypatch):
    monkeypatch.setenv("TOUCHSTONE_PLATFORM", "gitcode")
    monkeypatch.setattr(O, "gh", lambda m, p, t, data=None, accept=None:
                        [] if m == "POST" else {"labels": [{"name": "other"}]})
    with pytest.raises(requests.exceptions.RequestException):
        O._post_escalate_label("o", "r", 9, "t")


def test_post_escalate_label_github_path_unchanged(monkeypatch):
    _gh_gitcode_off(monkeypatch)
    calls = []

    def fake_gh(method, path, token, data=None, accept=None):
        calls.append((method, path, data))
        return {}

    monkeypatch.setattr(O, "gh", fake_gh)
    O._post_escalate_label("o", "r", 9, "t")
    assert calls == [("POST", "/repos/o/r/issues/9/labels",
                      {"labels": ["touchstone:needs-human"]})]


def test_label_names_handles_dict_string_and_mixed():
    assert O._label_names([{"name": "a"}, {"name": "b"}]) == ["a", "b"]
    assert O._label_names(["a", "b"]) == ["a", "b"]
    assert O._label_names([{"name": "a"}, "b", {}, "", None]) == ["a", "b"]
    assert O._label_names(None) == []


# ---- 历史评论折叠（orchestrator._collapse_stale_reviews GitCode 分支）----------

def test_collapse_stale_reviews_gitcode_sends_json_body(monkeypatch):
    # 官方文档声明 application/json；实测 JSON 体 200 且落库。锁定 json= 体与
    # /pulls/comments/{id} 端点（与取评论的 /pulls/{n}/comments 同 id 命名空间）。
    monkeypatch.setenv("TOUCHSTONE_PLATFORM", "gitcode")
    monkeypatch.setenv("TOUCHSTONE_GITHUB_BASE_URL", "https://api.gitcode.com/api/v5")
    monkeypatch.setattr(O, "_collapse_review_body", lambda body: "FOLDED:" + body)
    seen = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

    def fake_patch(url, headers=None, **kw):
        seen.update(url=url, headers=headers, kw=kw)
        return _Resp()

    monkeypatch.setattr(O.requests, "patch", fake_patch)
    O._collapse_stale_reviews("o", "r", "tok", [{"id": 123, "body": "b"}])
    assert seen["url"] == "https://api.gitcode.com/api/v5/repos/o/r/pulls/comments/123"
    assert seen["kw"].get("json") == {"body": "FOLDED:b"}
    assert "data" not in seen["kw"]                 # 不再走 form-data
    assert seen["headers"].get("Content-Type") == "application/json"


# ---- sync_touchstone_config（orchestrator）-------------------------------------

def _fake_ghclient_router(monkeypatch, routes):
    """routes: list of (predicate(url) -> bool, response)。命中即返回；未命中抛错。"""
    def fake_request(method, url, token, data=None, accept=None, **kw):
        for pred, resp in routes:
            if pred(url):
                return resp
        raise AssertionError(f"unexpected API call: {method} {url}")

    monkeypatch.setattr(O.ghclient, "request", fake_request)


def test_sync_config_skips_when_yaml_present(monkeypatch, tmp_path):
    ts = tmp_path / ".touchstone"
    ts.mkdir()
    (ts / "pr.yaml").write_text("scope: []\n", encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("本地已有 yaml 配置就不该再打 API")

    monkeypatch.setattr(O.ghclient, "request", boom)
    O.sync_touchstone_config("o", "r", 9, "t", str(tmp_path))


def test_sync_config_fetches_from_base_when_missing(monkeypatch, tmp_path):
    payload = b"rules: []\n"
    _fake_ghclient_router(monkeypatch, [
        (lambda u: u.endswith("/pulls/9"), {"base": {"sha": "S1"}, "head": {"sha": "H"}}),
        (lambda u: "/contents/.touchstone?" in u,
         [{"type": "file", "name": "pr.yaml"}, {"type": "dir", "name": "sub"},
          {"type": "file", "name": "notes.md"}]),
        (lambda u: "/contents/.touchstone/pr.yaml?" in u,
         {"type": "file", "encoding": "base64",
          "content": base64.b64encode(payload).decode()}),
    ])
    O.sync_touchstone_config("o", "r", 9, "t", str(tmp_path))
    got = (tmp_path / ".touchstone" / "pr.yaml").read_bytes()
    assert got == payload                      # 非目录、非 yaml 的条目都被过滤


def test_sync_config_rejects_path_traversal_names(monkeypatch, tmp_path):
    # contents 列表带路径段的名字（攻击面：把写文件指到 .touchstone 之外）必须跳过
    _fake_ghclient_router(monkeypatch, [
        (lambda u: u.endswith("/pulls/9"), {"base": {"sha": "S1"}}),
        (lambda u: "/contents/.touchstone?" in u,
         [{"type": "file", "name": "../evil.yaml"},
          {"type": "file", "name": "sub/evil.yaml"},
          {"type": "file", "name": "good.yaml"}]),
        (lambda u: "/contents/.touchstone/good.yaml?" in u,
         {"type": "file", "encoding": "base64",
          "content": base64.b64encode(b"x: 1\n").decode()}),
    ])
    O.sync_touchstone_config("o", "r", 9, "t", str(tmp_path))
    assert (tmp_path / ".touchstone" / "good.yaml").exists()
    assert not (tmp_path / "evil.yaml").exists()
    assert not (tmp_path / ".touchstone" / "sub").exists()


def test_sync_config_missing_base_sha_is_noop(monkeypatch, tmp_path):
    _fake_ghclient_router(monkeypatch, [
        (lambda u: u.endswith("/pulls/9"), {"base": {}}),
    ])
    O.sync_touchstone_config("o", "r", 9, "t", str(tmp_path))
    assert not (tmp_path / ".touchstone").exists()


def test_sync_config_api_failure_only_warns(monkeypatch, tmp_path, capsys):
    def boom(*a, **k):
        raise requests.exceptions.HTTPError("500 boom")

    monkeypatch.setattr(O.ghclient, "request", boom)
    O.sync_touchstone_config("o", "r", 9, "t", str(tmp_path))   # 不抛
    assert "sync .touchstone/ from base branch failed" in capsys.readouterr().err
