# ACTN 增长周报数据 — 2026-09-28

采集时间：2026-09-28T00:59:23+0000

> 本文件只做**数据采集与呈现**，不含 SCALE / KEEP / REDUCE / PAUSE 判断。
> 判断需要人（或带 LLM 的 Agent）来做。缺失的指标一律标注为缺失，不推测、不填充。

## 1. 开发者资产仓（分发面）

| 指标 | 值 |
|---|---|
| 近 7 天提交数 | 1 |
| Release 数（近 5 个） | 1 |
| 未结 issue（入站信号） | 0 |
| 仓库存取量 views / uniques | 0 / 0 |
| 克隆量 clones / uniques | 0 / 0 |

## 2. Agent Skills 注册表（主渠道 3）

| 指标 | 值 |
|---|---|
| 已发布版本 | 2026.09.25 |
| commit | 966c79639900 |
| 版本数 | 2 |
| totalInstalls（注册表原始计数） | 16 |
| 其中：自测产生（已归因） | 2 |
| 归因起始时间 | 2026-09-25T18:57:26+0800 |
| **净外部安装（上界，非真值）** | **14** |

⚠️ **计数口径说明（重要）**：自动化的分发路径验证（`verify_skill_install.py`）
会**真实安装**该技能，因此注册表的原始 `totalInstalls` 包含我们自己的测试安装。
上表净值为 **上界而非真值**：归因机制启用**之前**发生的自测运行没有被计入，
且注册表可能把 `add` 与 `update` 各计一次（实测单次运行 delta 常为 2）。
因此真实外部安装数**很可能低于**该上界。任何引用都必须连同这句限定一起引用。

## 3. 自有社交渠道（低音量分发）

| 渠道 | 累计发布 | 近 7 天 |
|---|---|---|
| agent_skill_registry | 2 | 2 |
| bluesky_owned_account | 2 | 2 |
| github_owned_repo | 1 | 1 |
| github_release | 1 | 1 |
| mastodon_owned_account | 1 | 1 |

- 内容分发类渠道（受每日上限约束）：actn_own_community, bluesky_owned_account, douyin_official_api, mastodon_owned_account, opt_in_email, wechat_official_account

## 4. 平台公开只读口径（仅内部观察，**不对外引用**）

| 站点 | 公开帖总数 |
|---|---|
| china | 488 |
| global | 0 |


## 5. 结构性缺失（不得用推测填充）

- **站点侧漏斗事件**（`anonymous_visit` → `spam_complaint` 全序列）：缺失。埋点需改动现有生产代码仓，受宪法性边界禁止。
- **first-touch / last-touch 归因**：缺失。同上。
- **注册与合格激活数**：缺失。无站点侧回传。
- **真实任务案例 / 首次交付与结算**：缺失。负责人尚无可用真实任务。
- **邮件渠道指标**：不可用。DKIM 与 DMARC 缺失，渠道已阻断。
- **Search Console / Bing / 百度收录数据**：不可得。未授予站点验证权限。

## 6. 自动化运行健康

- 近 7 天运行次数：7
- RUN_STATUS 分布：{"NO_NEW_SIGNAL": 6, "OK": 1}
- 最近一次分发路径验证：{"ran": false, "reason": "not_due", "last_run": "2026-09-25T09:13:59+0800"}

## 7. 待人工/待决策

- 本文件**不给出** SCALE / KEEP / REDUCE / PAUSE 结论，需人工或 LLM 复盘任务判定。
- 若上表中任一关键指标为 `n/a` 或「缺失」，不得以其他指标替代推断。

