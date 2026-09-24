# Touchstone — AI Committer

**用 AI 来检视 AI 生成的代码。** 挂在 GitHub PR 上的 AI 评审 + 客观质量门禁：

- **AI 评审（advisory）**：复用 [PR-Agent](https://github.com/qodo-ai/pr-agent) 评审你的 PR，发现回贴成带 checkbox 的待办清单，只给建议、**不拦截合入**；
- **质量门禁**：契约核对、栈专项规则、密钥/危险代码扫描等确定性检查（无 LLM），聚合成唯一总闸 `touchstone/gate`——它绿，才满足分支保护；
- **可选能力（默认关）**：独立验证 verify（异模型盲测验收测试）、自动合并 autonomy、学习回路（评审越用越准，含 TF-GRPO）。

## 快速部署到你的仓库（3 分钟）

不需要 fork 或 clone 本仓——在你的仓库创建一个 workflow 即可。

**Step 1**：创建 `.github/workflows/touchstone.yml`：

```yaml
name: Touchstone Review
on:
  pull_request_target:
    types: [opened, synchronize, reopened]

permissions:
  contents: read
  pull-requests: write
  checks: write

env:
  # 引擎版本单一来源。三选一：
  #   latest  → 自动跟最新 release tag（零摩擦跟 patch；breaking change 有 schema_version 告警兜底）
  #   v0.3.7  → 钉具体 tag（可复现，升级时手动改这一行）
  #   main    → 追主干（最激进，不推荐生产用）
  TOUCHSTONE_ENGINE_REPO: "AKDI-SE/touchstone"
  TOUCHSTONE_ENGINE_REF: "latest"

jobs:
  touchstone:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.base.ref }}
          fetch-depth: 0
      - name: Resolve engine ref（latest → 最新 release tag；具体 tag/main 原样透传）
        id: tsref
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          REF="${TOUCHSTONE_ENGINE_REF}"
          if [ "$REF" = "latest" ]; then
            REF=$(gh release view --repo "${TOUCHSTONE_ENGINE_REPO}" --json tagName --jq .tagName)
          fi
          [ -n "$REF" ] || { echo "::error::resolve engine ref 失败（TOUCHSTONE_ENGINE_REF=${TOUCHSTONE_ENGINE_REF}，检查 GH_TOKEN 权限 / ${TOUCHSTONE_ENGINE_REPO} 是否有 release）"; exit 1; }
          echo "resolved=$REF" >> "$GITHUB_OUTPUT"
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - run: pip install -e .
      - name: Set up PR-Agent
        run: |
          python -m venv .pragent-venv
          .pragent-venv/bin/pip install -U pip
          .pragent-venv/bin/pip install pr-agent
      - name: Checkout Touchstone
        uses: actions/checkout@v4
        with:
          repository: ${{ env.TOUCHSTONE_ENGINE_REPO }}
          ref: ${{ steps.tsref.outputs.resolved }}
          path: .touchstone-src
      - name: Run review
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          LLM_BASE_URL: ${{ secrets.LLM_BASE_URL }}
          LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
          LLM_MODEL: ${{ secrets.LLM_MODEL }}
          TOUCHSTONE_PRAGENT_CMD: ".pragent-venv/bin/python -m touchstone.pr_agent_runner"
          TOUCHSTONE_SKIP_GATE: "true"
        run: |
          pip install -e .touchstone-src
          cd .touchstone-src && python -m touchstone.orchestrator
```

引擎版本由顶部 `TOUCHSTONE_ENGINE_REF` 决定：

| 值 | 行为 | 适用 |
|---|---|---|
| `latest` | 自动解析为最新 release tag（默认） | 日常零摩擦跟 patch |
| `v0.3.7` | 钉具体 tag | 要求可复现；升级手动改这一行 |
| `main` | 追主干 | 实验性，不推荐生产 |

**Step 2**：配置 Secrets（Settings → Secrets and variables → Actions → New repository secret）:

| Secret 名 | 必填 | 说明 | 示例 |
|---|---|---|---|
| `LLM_BASE_URL` | ✅ | LLM 的 OpenAI 兼容端点 | `https://open.bigmodel.cn/api/coding/paas/v4` |
| `LLM_API_KEY` | ✅ | LLM 端点的 API key | `your-key-here` |
| `LLM_MODEL` | ✅ | 评审用的模型名 | `glm-5.3` |
| `TOUCHSTONE_LLM_REFLECT_MODEL` | 可选 | improve 自评打分专用的小模型（不设则沿用主模型）；`touchstone.yml` 已默认 `glm-4.7` | `glm-4.7` |

> `GITHUB_TOKEN` 由 GitHub Actions 自动提供，无需手动配。

**Step 3**：配置 Variables（Settings → Secrets and variables → Actions → Variables tab）:

| Variable 名 | 默认 | 说明 |
|---|---|---|
| `TOUCHSTONE_MAX_DIFF_LINES` | `3000` | 单 PR 行数上限，超限不调 LLM、直接 block 提示拆分。设 `0` 关闭 |
| `TOUCHSTONE_LLM_CONTEXT_TOKENS` | 未设回退 128000 | 模型上下文窗口（token）。按模型卡设置；GLM-5.3 支持 128K |
| `TOUCHSTONE_LLM_OUTPUT_TOKENS` | `4096` | 模型最大输出（token）。大 PR 建议产出多时调 8192 防截断 |
| `TOUCHSTONE_LLM_NUM_RETRIES` | `0` | tenacity 重试次数（实证轮内重试救回率 0，默认不重试） |
| `TOUCHSTONE_LLM_THINKING` | 未设=随端点默认 | 思考模式开关：`disabled`/`enabled`（思考型端点默认开思考是大 diff 单调用 10min+ 的头号成因，网关改不了时配 `disabled`） |

**Step 4**：分支保护（Settings → Branches）——把 `touchstone/gate` 设为 **Required status check**。

**Step 5**：验证——开一个测试 PR，应看到：
- PR 评论里出现 **Touchstone · AI Committer 代码检视** 评审（含待销项清单）；
- check `touchstone/gate` 为 success（无 block 级发现时）；
- 若 LLM 未配通，评论顶部会出现 `⚠️ AI 评审...` 横幅（不静默）。

## 部署后你会看到什么

- **评审评论**：AI 评审发现 + 待解决清单（GitHub 原生 checkbox，逐条带签名/方向/依据）。多轮销项期间，历史轮次自动折叠，只读最新一条即可。
- **PR 列表页零点击**：标签徽章 `touchstone:converged`（绿，已闭环）/ `touchstone:open-findings`（红，进行中）+ 未销项量级桶（1–3 / 4–10 / 11+）；悬停 checks 图标即见 `✅ 已闭环 / 🔁 未销项 M 项 / ⬆️ 已升级到人`。
- **不静默**：pr-agent 没装好、取不到 PR、LLM 调用失败时，评论顶部与 check 标题会显式横幅（`⚠️ AI 评审未运行 / 取 PR 失败 / LLM 调用失败`），不会假装"0 条发现"。

## 处置评审发现（销项）

评审发现逐条开列，销项是多轮交互：**改码 → 提交 → 发 ack 评论 → 推送**，复检按申报核销（done=已改码；waived=驳回须附反证，待人核准；split=拆后续 PR）；全部销项即 ✅ 收敛，始终不销将 ⬆️ 升级到人。

**给你的 AI agent 装上销项 skill（1 分钟）**：协议权威文档在 [skills/touchstone-ack/SKILL.md](skills/touchstone-ack/SKILL.md)（申报格式、语义、时序、轮询纪律）。接线方式任选其一：

```bash
# 方式 A：拷进你的仓（推荐——随仓版本化，agent 一定能读到）
mkdir -p .claude/skills && cp -r /path/to/touchstone/skills/touchstone-ack .claude/skills/

# 方式 B：仅在你的 AGENTS.md / CLAUDE.md 指一句（agent 按需来读正本）
#   本仓 PR 被 Touchstone 评审；销项协议见 touchstone 仓 skills/touchstone-ack/SKILL.md
```

**销项不是一发即中**：每轮复检可能新增 findings（评审看到修复后给出下一层意见是常态）——agent 推送申报后应每 1 分钟检查一次最新 bot 评论，直到 ✅ 收敛或 ⬆️ 升级到人才停。

## 让评审懂你的团队（可选）

- **手写规范种子 `.touchstone/seeds.yaml`**——立即生效、零基础设施：手写的团队规范直接注入评审提示词（样例见 `.touchstone/seeds.yaml.example`）。
- **学习回路（`learn.yml`，含 TF-GRPO）**——从历史 PR 的采纳/忽略自动蒸馏经验，需积累历史 + 旗舰模型端点；新经验先 shadow A/B 达标才启用，只调建议、不碰合入。

两者都受 `TOUCHSTONE_EXPERIENCE_ENABLED` 总闸控制，机制详见 `docs/learning-loop-design.html`。

## 更多文档

- `docs/DEPLOYMENT.md` —— 客户版部署指南（从零到上线）
- `docs/incident-runbook.md` —— 运维故障排查手册（症状→诊断→处置）
- `docs/touchstone-design.html` —— 详细设计
- `docs/learning-loop-design.html` —— 学习回路设计
- `SECURITY.md` —— 安全边界与漏洞披露
