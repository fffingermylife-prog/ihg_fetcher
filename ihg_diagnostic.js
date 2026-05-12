/**
 * IHG Combined Request Diagnostic
 * 诊断脚本 - 验证合并请求是否能同时返回现金+积分
 *
 * 使用: 在 ihg.com 任意页面的 Console 中粘贴运行
 * 会发 1 个请求, 打印关键诊断信息
 */

(async function () {
    const uuid = () => 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
        const r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 3 | 8);
        return v.toString(16);
    });

    const payload = {
        hotelMnemonics: ["BKKHB"],
        startDate: "2026-05-10",
        endDate: "2026-07-10",
        lengthOfStay: 1,
        guestCounts: [{ otaCode: "AQC10", count: 1 }],
        options: {
            includeSellStrategy: "followChannel",
            returnAmountsAfterTaxForLowestOffer: true,
            returnAverages: true,
            lowestOfferPerRatePlan: true,
            identifyLowestOfferPerRatePlan: true,
        },
        // 合并请求: 带 rates
        rates: { ratePlanCodes: ["IVAN1", "IVAN3", "IVAN5", "IVAN6", "IVAN7", "IVANI"] }
    };

    console.log("🔬 发送合并请求 (带 rates.ratePlanCodes)...");

    const resp = await fetch("https://apis.ihg.com/availability/v1/calendar", {
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

    if (!resp.ok) {
        console.error(`❌ HTTP ${resp.status}`);
        return;
    }

    const data = await resp.json();
    const hotel = data?.data?.hotels?.[0];
    if (!hotel) {
        console.error("❌ 响应中无 hotels 数据");
        return;
    }

    // === 诊断 1: ratePlans 里有哪些 plan? ===
    const ratePlans = hotel.ratePlans || [];
    const rewardPlans = ratePlans.filter(r => r.isRewardNight);
    const cashPlans = ratePlans.filter(r => !r.isRewardNight);
    console.log(`\n📋 ratePlans 汇总:`);
    console.log(`   现金 plans: ${cashPlans.length} 个 (${cashPlans.slice(0, 5).map(p => p.code).join(", ")}...)`);
    console.log(`   积分 plans: ${rewardPlans.length} 个 (${rewardPlans.map(p => p.code).join(", ")})`);

    // === 诊断 2: 检查前 5 天的 calendar 数据 ===
    const cal = hotel.calendar || [];
    console.log(`\n📅 日历天数: ${cal.length}`);
    console.log(`\n🔍 前 5 天的数据分析:`);

    let daysWithCash = 0;
    let daysWithPoints = 0;
    let daysWithBoth = 0;

    for (const day of cal) {
        const hasCash = !!day.lowestRate;
        const hasPoints = (day.offers || []).some(o =>
            (o.ratePlanCode || "").startsWith("IVAN") && o.totalPoints != null
        );
        if (hasCash) daysWithCash++;
        if (hasPoints) daysWithPoints++;
        if (hasCash && hasPoints) daysWithBoth++;
    }

    // 打印前5天详情
    for (let i = 0; i < Math.min(5, cal.length); i++) {
        const day = cal[i];
        const cash = day.lowestRate ? `${day.lowestRate.totalAmount} ${day.lowestRate.currency}` : "❌无";
        const ptsOffers = (day.offers || []).filter(o => (o.ratePlanCode || "").startsWith("IVAN"));
        const pts = ptsOffers.length > 0 ? `${ptsOffers[0].totalPoints} (${ptsOffers[0].ratePlanCode})` : "❌无";
        console.log(`   ${day.start}: 现金=${cash}, 积分=${pts}`);
    }

    // === 诊断 3: 汇总结论 ===
    console.log(`\n📊 统计:`);
    console.log(`   有现金价的天数: ${daysWithCash}/${cal.length}`);
    console.log(`   有积分价的天数: ${daysWithPoints}/${cal.length}`);
    console.log(`   同时有两者的天数: ${daysWithBoth}/${cal.length}`);

    console.log(`\n🎯 结论:`);
    if (daysWithBoth >= cal.length * 0.8) {
        console.log(`   ✅ 合并请求可行! 一次请求即可拿到现金+积分价`);
    } else if (daysWithPoints > 0 && daysWithCash === 0) {
        console.log(`   ⚠️ 合并请求只返回了积分, 没有现金价 → 需要双请求模式`);
    } else if (daysWithCash > 0 && daysWithPoints === 0) {
        console.log(`   ⚠️ 合并请求只返回了现金, 没有积分价 → 需要双请求模式`);
    } else {
        console.log(`   ⚠️ 合并请求数据不完整 → 建议双请求模式`);
    }

    // 全局变量便于深入调查
    window.__IHG_DIAG = data;
    console.log(`\n💡 完整响应已存到 window.__IHG_DIAG`);
})();
