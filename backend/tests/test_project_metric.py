"""项目分群指标场景测试：授权统计、披露防护、并发互斥与撤权审计。

场景设定（见 app.seed.INTERNAL_SEED_ROWS）：
- 群A：SMP-A1 高分、SMP-A2 低分，对项目R仅汇总（群A/合计），对项目D明细授权；
- 群B：SMP-B1 低分仅允许计入合计，SMP-B2 高分完全私有；
- 项目R 为只读项目，数据所有方持有全部样本。
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import service_for
from app.store import store


def reset_state():
    """每个场景从干净的授权与台账状态开始。"""
    store.reset_internal()
    store.rows("project_metric").clear()


def metric_svc():
    return service_for("project_metric")


def mining_svc():
    return service_for("mining_result")


# ── 场景1: A群统计与合计各自能用，组合即披露冲突 ──
def test_scope_stats_individually_ok_but_conflicting():
    """A群=1/2、合计=1/3各自可交付；先交付任一项后另一项必须被拒绝。"""
    reset_state()
    svc = metric_svc()

    # 私有样本不能算入分子或分母：群B 单独统计无可统计样本
    denied = svc.submit_comparison_metric("项目R", "群B")
    assert not denied["ok"] and denied["reason"] == "无可统计样本", denied

    # A 群统计：两条已授权记录，其中一条高分 → 1/2
    r_a = svc.submit_comparison_metric("项目R", "群A")
    assert r_a["ok"], r_a
    assert r_a["分子"] == 1 and r_a["分母"] == 2, r_a

    # 已交付 A 群后，合计会与之组合暴露 B 的低分 → 披露冲突
    r_total = svc.submit_comparison_metric("项目R", "合计")
    assert not r_total["ok"] and r_total["reason"] == "披露冲突", r_total

    # 反过来：先交付合计（1/3，私有高分记录不计入），A 群统计同样被拒绝
    reset_state()
    r_total2 = svc.submit_comparison_metric("项目R", "合计")
    assert r_total2["ok"], r_total2
    assert r_total2["分子"] == 1 and r_total2["分母"] == 3, r_total2
    r_a2 = svc.submit_comparison_metric("项目R", "群A")
    assert not r_a2["ok"] and r_a2["reason"] == "披露冲突", r_a2

    # 从首次交付起台账持续有效：项目仍可用首次交付的结果生成洞察
    insight = mining_svc().open_group_insight(r_total2["insight_id"], "项目R")
    assert insight["ok"] and insight["status"] == "待复核", insight
    assert insight["分母"] == 3 and insight["授权版本"] == "v2026.1", insight


# ── 场景2: 挖掘详情与对照操作同时提交，仅一项生成洞察 ──
def test_concurrent_entries_single_winner():
    """同一项目从两个入口同时提交 A 群统计与合计：一项成功，另一项被拒绝。"""
    reset_state()
    svc = metric_svc()
    results: dict[str, dict] = {}

    def from_mining():
        results["挖掘详情"] = mining_svc().generate_group_insight("项目R", "群A")

    def from_compare():
        results["对照操作"] = svc.submit_comparison_metric("项目R", "合计")

    threads = [threading.Thread(target=from_mining), threading.Thread(target=from_compare)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(results) == {"挖掘详情", "对照操作"}, results
    winners = [name for name, r in results.items() if r.get("ok")]
    losers = [name for name, r in results.items() if not r.get("ok")]
    assert len(winners) == 1 and len(losers) == 1, results

    loser = results[losers[0]]
    assert loser.get("reason") == "披露冲突", loser

    winner = results[winners[0]]
    if winners[0] == "挖掘详情":  # A 群若开放应为 1/2
        assert winner["分子"] == 1 and winner["分母"] == 2, winner
    else:  # 合计若开放应为 1/3
        assert winner["分子"] == 1 and winner["分母"] == 3, winner

    # 只有一项统计生成了待复核洞察
    insights = mining_svc().list_group_insights("项目R")
    assert insights["count"] == 1, insights
    assert insights["insights"][0]["status"] == "待复核", insights


# ── 场景3: 明细项目逐行可读，但不能靠合计反推 B 的分数 ──
def test_detail_project_cannot_infer_b_from_total():
    """获准看 A 明细的项目两条记录仍可读；其合计请求因暴露 B 被拒绝。"""
    reset_state()
    svc = metric_svc()

    samples = svc.project_samples("项目D")
    assert samples["ok"] and samples["count"] == 2, samples
    assert {s["样本编号"] for s in samples["samples"]} == {"SMP-A1", "SMP-A2"}, samples

    # 项目D 已知 A 群两条明细，合计只多出 B1 一条非明细样本 → 直接暴露
    r_total = svc.submit_comparison_metric("项目D", "合计")
    assert not r_total["ok"] and r_total["reason"] == "披露冲突", r_total

    # 仅由明细样本构成的 A 群统计不受影响
    r_a = svc.submit_comparison_metric("项目D", "群A")
    assert r_a["ok"] and r_a["分子"] == 1 and r_a["分母"] == 2, r_a


# ── 场景4: 撤权后旧洞察可核对、新合计剔除 B、新交付被披露核对拦截 ──
def test_revoke_sharing_and_owner_audit():
    """撤销 B 的共享：项目只见撤权状态，所有方核对旧分母与授权版本，新合计剔除 B。"""
    reset_state()
    svc = metric_svc()

    # 先交付合计 1/3 并生成待复核洞察
    delivered = svc.submit_comparison_metric("项目R", "合计")
    assert delivered["ok"] and delivered["分母"] == 3, delivered
    insight_id = delivered["insight_id"]

    before = mining_svc().open_group_insight(insight_id, "项目R")
    assert before["ok"] and before["分母"] == 3, before

    # 撤回 B 的共享
    revoked = svc.revoke_sharing("项目R", group="群B")
    assert revoked["ok"] and revoked["revoked_samples"] == ["SMP-B1"], revoked
    assert insight_id in revoked["affected_insights"], revoked

    # 被撤权项目重新打开原洞察：只看到撤权状态，看不到分子分母
    after = mining_svc().open_group_insight(insight_id, "项目R")
    assert after["ok"] and after["status"] == "已撤权", after
    assert "分母" not in after and "高分率" not in after, after

    # 数据所有方仍能从旧洞察查到 1/3，当时的分母和授权版本可供核对
    owner_view = mining_svc().open_group_insight(insight_id, "数据所有方")
    assert owner_view["ok"] and owner_view["分母"] == 3, owner_view
    assert owner_view["分子"] == 1 and owner_view["授权版本"] == "v2026.1", owner_view
    assert owner_view["撤权标记"], owner_view

    # 所有方运行新的合计：B 已剔除（A 群 1/2，B1 已撤销、B2 私有均不计入）
    audit = svc.run_owner_audit("数据所有方", "项目R", "合计")
    assert audit["ok"], audit
    assert audit["分子"] == 1 and audit["分母"] == 2, audit
    assert "SMP-B1" in audit["excluded_samples"], audit
    assert "SMP-B2" in audit["excluded_samples"], audit

    # 受限项目发起新统计：新旧结果（1/3 与 1/2）可推断 B 的分数 → 拒绝新的交付
    again = svc.submit_comparison_metric("项目R", "合计")
    assert not again["ok"] and again["reason"] == "披露冲突", again

    # 明细项目的两条记录不受撤权影响，仍可读
    samples = svc.project_samples("项目D")
    assert samples["count"] == 2, samples


if __name__ == "__main__":
    tests = [
        test_scope_stats_individually_ok_but_conflicting,
        test_concurrent_entries_single_winner,
        test_detail_project_cannot_infer_b_from_total,
        test_revoke_sharing_and_owner_audit,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"[PASS] {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"[FAIL] {test.__name__}: {e}")
            failed += 1
    print(f"\n总测试: {len(tests)}, 通过: {passed}, 失败: {failed}")
    if failed > 0:
        sys.exit(1)
