/**
 * IHG Combined Request Diagnostic v2
 * 逐步尝试不同的 payload 变体,定位 400 原因
 *
 * 使用: 在 ihg.com 酒店页面 (推荐 BKKHB 的搜索结果页) 的 Console 粘贴运行
 */

(async function () {
    const uuid = () => 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
        const r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 3 | 8);
        return v.toString(16);
    });

    const API_URL = "https://apis.ihg.com/availability/v1/calendar";

    async function tryRequest(label, payload) {
        console.log(`\n${"═".repeat(60)}`);
        console.log(`🧪 测试: ${label}`);
        console.log(`   payload: ${JSON.stringify(payload).slice(0, 200)}${JSON.stringify(payload).length > 200 ? "..." : ""}`);

        const resp = await fetch(API_URL, {
            method: "POST",
            headers: {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json; charset=UTF-8",
                "ihg-language": "en-US",
                "ihg-sessionid": uuid(),
                "ihg-transactionid": uuid(),
                "x-ihg-api-key": "se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y",
            },
            body: JSON.stringify(payload),
            credentials: "include",
        });

        console.log(`   HTTP ${resp.status}`);

        // 无论成功失败,都尝试读 body
        const text = await resp.text();
        let json = null;
        try { json = JSON.parse(text); } catch (e) { }

        if (resp.ok && json) {
            const hotel = json?.data?.hotels?.[0];
            const ratePlans = hotel?.ratePlans || [];
            const cal = hotel?.calendar || [];

            // 统计
            let daysWithCash = 0, daysWithPoints = 0, daysWithBoth = 0;
            for (const day of cal) {
                const hasCash = !!day.lowestRate;
                const hasPoints = (day.offers || []).some(o =>
                    (o.ratePlanCode || "").startsWith("IVAN") && o.totalPoints != null
                );
                if (hasCash) daysWithCash++;
                if (hasPoints) daysWithPoints++;
                if (hasCash && hasPoints) daysWithBoth++;
            }

            const rewardPlans = ratePlans.filter(r => r.isRewardNight).map(r => r.code);
            const cashPlans = ratePlans.filter(r => !r.isRewardNight).map(r => r.code);

            console.log(`   ✅ 成功`);
            console.log(`      ratePlans: 现金 ${cashPlans.length} 个, 积分 ${rewardPlans.length} 个 ${rewardPlans.length ? "[" + rewardPlans.join(",") + "]" : ""}`);
            console.log(`      calendar: ${cal.length} 天`);
            console.log(`      有 lowestRate 的天数: ${daysWithCash}`);
            console.log(`      有 IVAN* offer 的天数: ${daysWithPoints}`);
            console.log(`      同时有两者的天数: ${daysWithBoth}`);

            // 前2天详情
            for (let i = 0; i < Math.min(2, cal.length); i++) {
                const d = cal[i];
                const cash = d.lowestRate ? `${d.lowestRate.totalAmount} ${d.lowestRate.currency}` : "无";
                const ptsOffer = (d.offers || []).find(o => (o.ratePlanCode || "").startsWith("IVAN"));
                const pts = ptsOffer ? `${ptsOffer.totalPoints} (${ptsOffer.ratePlanCode})` : "无";
                console.log(`      ${d.start}: 现金=${cash}, 积分=${pts}`);
            }

            return { ok: true, data: json, stats: { daysWithCash, daysWithPoints, daysWithBoth, total: cal.length } };
        } else {
            console.log(`   ❌ 失败`);
            console.log(`   响应体:`, json || text.slice(0, 800));
            return { ok: false, status: resp.status, body: json || text };
        }
    }

    // ==========  日期: 使用从今天开始的动态日期,避免 startDate 过期 ==========
    const today = new Date();
    const addDays = (d, n) => {
        const x = new Date(d);
        x.setDate(x.getDate() + n);
        return x.toISOString().slice(0, 10);
    };
    const START_DATE = addDays(today, 1);    // 明天开始
    const END_DATE = addDays(today, 61);     // 60天窗口
    const SHORT_END = addDays(today, 31);    // 30天窗口

    // ==========  基础 payload ==========
    const BASE = {
        hotelMnemonics: ["BKKHB"],
        startDate: START_DATE,
        endDate: END_DATE,
        lengthOfStay: 1,
        guestCounts: [{ otaCode: "AQC10", count: 1 }],
        options: {
            includeSellStrategy: "followChannel",
            returnAmountsAfterTaxForLowestOffer: true,
            returnAverages: true,
            lowestOfferPerRatePlan: true,
            identifyLowestOfferPerRatePlan: true,
        }
    };

    console.log("🔬 IHG API 诊断 v3 - 使用动态日期");
    console.log(`   今天: ${today.toISOString().slice(0, 10)}`);
    console.log(`   测试范围: ${START_DATE} ~ ${END_DATE}\n`);

    // 测试 1: 纯现金请求 (无 rates)
    const r1 = await tryRequest("1️⃣ 纯现金 (无 rates 字段)", { ...BASE });
    await new Promise(r => setTimeout(r, 2000));

    // 测试 2: 合并请求 - 完整 IVAN* 列表
    const r2 = await tryRequest("2️⃣ 合并 (完整 IVAN1/3/5/6/7/I)", {
        ...BASE,
        rates: { ratePlanCodes: ["IVAN1", "IVAN3", "IVAN5", "IVAN6", "IVAN7", "IVANI"] }
    });
    await new Promise(r => setTimeout(r, 2000));

    // 测试 3: 合并请求 - 只用 IVANI (BKKHB 实际拥有的积分 plan)
    const r3 = await tryRequest("3️⃣ 合并 (只用 IVANI)", {
        ...BASE,
        rates: { ratePlanCodes: ["IVANI"] }
    });
    await new Promise(r => setTimeout(r, 2000));

    // 测试 4: 合并请求 - 缩短日期范围到 30 天
    const r4 = await tryRequest("4️⃣ 合并 (只 30 天, 看是否日期太长)", {
        ...BASE,
        endDate: SHORT_END,
        rates: { ratePlanCodes: ["IVANI"] }
    });

    // ============ 汇总分析 ============
    console.log(`\n${"═".repeat(60)}`);
    console.log("📊 汇总结论:");
    console.log(`   测试1 (纯现金):        ${r1.ok ? "✅" : "❌ " + r1.status}`);
    console.log(`   测试2 (完整IVAN*):     ${r2.ok ? "✅" : "❌ " + r2.status}`);
    console.log(`   测试3 (只IVANI):       ${r3.ok ? "✅" : "❌ " + r3.status}`);
    console.log(`   测试4 (短日期+IVANI):  ${r4.ok ? "✅" : "❌ " + r4.status}`);

    console.log("\n🎯 解读:");
    if (r1.ok && !r2.ok && r3.ok) {
        console.log("   → 完整IVAN*列表中有BKKHB不支持的plan,用单个IVANI即可");
    } else if (r1.ok && r2.ok && r2.stats.daysWithBoth > 0) {
        console.log("   → ✅ 合并请求完全可行! 一次就能拿现金+积分");
    } else if (r1.ok && r2.ok && r2.stats.daysWithBoth === 0 && r2.stats.daysWithPoints > 0) {
        console.log("   → ⚠️ 合并请求只返回积分,不返回现金 → 必须双请求");
    } else if (!r1.ok) {
        console.log("   → 基础请求就失败了,可能是 cookie/session 问题或酒店代码问题");
    } else {
        console.log("   → 情况特殊,请把上面的详细结果发给助手分析");
    }

    console.log("\n💡 所有结果已存到 window.__IHG_DIAG_V2");
    window.__IHG_DIAG_V2 = { r1, r2, r3, r4 };
})();
