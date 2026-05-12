/**
 * IHG Calendar Price Fetcher - 浏览器控制台版本 (全量)
 * 获取日历所有开放日期的每天最低现金价 + 积分价
 *
 * 功能:
 * - 滑动窗口: 自动切分日期, 获取未来 N 天的全部价格 (默认 365 天)
 * - 智能合并请求: 先尝试一次拿现金+积分, 不行再分两次请求
 *
 * 使用方法:
 * 1. 浏览器打开 ihg.com
 * 2. F12 -> Console
 * 3. 粘贴此脚本运行
 */

(async function IHGCalendarFetcher() {
    'use strict';

    // ============ 配置 ============
    const CONFIG = {
        hotelCodes: [
            "BKKHB",  // InterContinental Bangkok
            // 添加更多...
        ],

        // 滑动窗口模式
        slidingWindow: true,       // true=自动滑动, false=用下面的固定范围
        daysAhead: 365,            // 获取从今天起未来多少天
        windowSizeDays: 60,        // 每次请求的日期窗口大小 (IHG 上限 ~60 天)

        // 固定范围 (slidingWindow=false 时使用)
        // 注意: startDate 必须 >= 今天, 否则会报 50027 Invalid system range
        startDate: "2026-06-01",
        endDate: "2026-07-31",

        lengthOfStay: 1,
        adults: 1,

        apiKey: "se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y",

        pointsRatePlanCodes: ["IVAN1", "IVAN3", "IVAN5", "IVAN6", "IVAN7", "IVANI"],

        // 诊断已确认 IHG API 不支持合并请求:
        //   - 不带 rates → 只返回现金价
        //   - 带 rates → 只返回积分价
        // 所以默认关闭, 直接双请求
        tryCombinedRequest: false,

        delay: 2000,
    };
    // ============ 配置结束 ============

    const API_URL = "https://apis.ihg.com/availability/v1/calendar";

    // 状态: 合并模式是否有效 (null=未知, true=有效, false=无效)
    let combinedModeWorks = null;

    function generateUUID() {
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
            const r = Math.random() * 16 | 0;
            const v = c === 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
        });
    }

    function sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    function isoDate(d) {
        return d.toISOString().slice(0, 10);
    }

    function genWindows(startDate, totalDays, windowSize) {
        /** 生成滑动窗口日期区间 */
        const windows = [];
        let current = new Date(startDate);
        const endTarget = new Date(startDate);
        endTarget.setDate(endTarget.getDate() + totalDays - 1);

        while (current <= endTarget) {
            const winEnd = new Date(current);
            winEnd.setDate(winEnd.getDate() + windowSize - 1);
            if (winEnd > endTarget) winEnd.setTime(endTarget.getTime());

            windows.push([isoDate(current), isoDate(winEnd)]);
            current = new Date(winEnd);
            current.setDate(current.getDate() + 1);
        }
        return windows;
    }

    function buildPayload(hotelCode, startDate, endDate, pointsMode) {
        const payload = {
            hotelMnemonics: [hotelCode],
            startDate: startDate,
            endDate: endDate,
            lengthOfStay: CONFIG.lengthOfStay,
            guestCounts: [{ otaCode: "AQC10", count: CONFIG.adults }],
            options: {
                includeSellStrategy: "followChannel",
                returnAmountsAfterTaxForLowestOffer: true,
                returnAverages: true,
                lowestOfferPerRatePlan: true,
                identifyLowestOfferPerRatePlan: true,
            }
        };
        if (pointsMode) {
            payload.rates = { ratePlanCodes: CONFIG.pointsRatePlanCodes };
        }
        return payload;
    }

    async function doFetch(payload, tag) {
        const headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json; charset=UTF-8",
            "ihg-language": "en-US",
            "ihg-sessionid": generateUUID(),
            "ihg-transactionid": generateUUID(),
            "x-ihg-api-key": CONFIG.apiKey,
        };

        try {
            const resp = await fetch(API_URL, {
                method: "POST",
                headers: headers,
                body: JSON.stringify(payload),
                credentials: "include",
            });
            if (resp.ok) {
                console.log(`  ✅ ${tag}`);
                return await resp.json();
            } else {
                console.error(`  ❌ ${tag} HTTP ${resp.status}`);
                return null;
            }
        } catch (err) {
            console.error(`  ❌ ${tag} ${err.message}`);
            return null;
        }
    }

    function hasBothCashAndPoints(response) {
        /** 判断响应是否同时包含现金价和积分价 */
        if (!response || !response.data) return false;
        const hotels = response.data.hotels || [];
        if (hotels.length === 0) return false;

        let hasCash = false;
        let hasPoints = false;

        for (const h of hotels) {
            for (const day of (h.calendar || [])) {
                if (day.lowestRate) hasCash = true;
                for (const offer of (day.offers || [])) {
                    const rp = offer.ratePlanCode || "";
                    if (rp.startsWith("IVAN") && offer.totalPoints != null) {
                        hasPoints = true;
                    }
                }
                if (hasCash && hasPoints) return true;
            }
        }
        return hasCash && hasPoints;
    }

    async function fetchWindow(hotelCode, startDate, endDate) {
        /**
         * 获取单个窗口的数据
         * 返回 { cash, points } (合并模式时两者是同一对象)
         */
        // 尝试合并请求
        if (CONFIG.tryCombinedRequest && combinedModeWorks !== false) {
            const payload = buildPayload(hotelCode, startDate, endDate, true);
            const tag = `${hotelCode} ${startDate}~${endDate} [合并]`;
            console.log(`  [*] ${tag} 请求中...`);
            const data = await doFetch(payload, tag);

            if (data && hasBothCashAndPoints(data)) {
                if (combinedModeWorks === null) {
                    console.log(`  [✓] 合并模式验证成功, 后续使用合并请求`);
                }
                combinedModeWorks = true;
                return { cash: data, points: data };
            }

            if (combinedModeWorks === null) {
                console.log(`  [!] 合并请求不完整, 回退到双请求模式`);
                combinedModeWorks = false;
            }
            await sleep(CONFIG.delay);
        }

        // 双请求
        const cashPayload = buildPayload(hotelCode, startDate, endDate, false);
        console.log(`  [*] ${hotelCode} ${startDate}~${endDate} [现金] 请求中...`);
        const cashData = await doFetch(cashPayload, `${hotelCode} 现金`);

        await sleep(CONFIG.delay);

        const ptsPayload = buildPayload(hotelCode, startDate, endDate, true);
        console.log(`  [*] ${hotelCode} ${startDate}~${endDate} [积分] 请求中...`);
        const ptsData = await doFetch(ptsPayload, `${hotelCode} 积分`);

        return { cash: cashData, points: ptsData };
    }

    function parseCashResponse(response) {
        /** 现金价: data.hotels[].calendar[].lowestRate.totalAmount */
        const prices = [];
        if (!response || !response.data) return prices;

        const hotels = response.data.hotels || [];
        for (const h of hotels) {
            const info = h.hotel || {};
            const code = info.hotelMnemonic || "UNKNOWN";
            const brand = info.brandCode || "";
            const currency = info.propertyCurrency || "";

            for (const day of (h.calendar || [])) {
                const date = day.start || "";
                const lr = day.lowestRate;
                prices.push({
                    hotel_code: code,
                    brand_code: brand,
                    date: date,
                    cash_price: lr ? parseFloat(lr.totalAmount) || null : null,
                    cash_currency: lr ? (lr.currency || currency) : currency,
                });
            }
        }
        return prices;
    }

    function parsePointsResponse(response) {
        /** 积分价: data.hotels[].calendar[].offers[].totalPoints (IVAN*) */
        const prices = [];
        if (!response || !response.data) return prices;

        const hotels = response.data.hotels || [];
        for (const h of hotels) {
            const info = h.hotel || {};
            const code = info.hotelMnemonic || "UNKNOWN";
            const brand = info.brandCode || "";

            const rewardCodes = new Set();
            for (const rp of (h.ratePlans || [])) {
                if (rp.isRewardNight) rewardCodes.add(rp.code);
            }

            for (const day of (h.calendar || [])) {
                const date = day.start || "";
                let lowest = null;
                for (const offer of (day.offers || [])) {
                    const rp = offer.ratePlanCode || "";
                    const isPts = rewardCodes.has(rp) || rp.startsWith("IVAN");
                    if (isPts && offer.totalPoints != null) {
                        const v = parseFloat(offer.totalPoints);
                        if (!isNaN(v) && (lowest === null || v < lowest)) lowest = v;
                    }
                }
                prices.push({
                    hotel_code: code,
                    brand_code: brand,
                    date: date,
                    points_price: lowest,
                });
            }
        }
        return prices;
    }

    function mergePrices(cashPrices, pointsPrices) {
        const cashMap = {};
        for (const r of cashPrices) cashMap[`${r.hotel_code}_${r.date}`] = r;
        const ptsMap = {};
        for (const r of pointsPrices) ptsMap[`${r.hotel_code}_${r.date}`] = r;

        const allKeys = new Set([...Object.keys(cashMap), ...Object.keys(ptsMap)]);
        const merged = [];

        for (const key of [...allKeys].sort()) {
            const c = cashMap[key] || {};
            const p = ptsMap[key] || {};
            const cashPrice = c.cash_price || null;
            const pointsPrice = p.points_price || null;
            const cpp = (cashPrice && pointsPrice && pointsPrice > 0)
                ? Math.round(cashPrice / pointsPrice * 10000) / 100
                : null;

            merged.push({
                hotel_code: c.hotel_code || p.hotel_code,
                brand_code: c.brand_code || p.brand_code || "",
                date: c.date || p.date,
                cash_price: cashPrice,
                cash_currency: c.cash_currency || "",
                points_price: pointsPrice,
                cents_per_point: cpp,
            });
        }
        return merged;
    }

    function exportToCSV(data) {
        if (data.length === 0) return;
        const headers = ["hotel_code", "brand_code", "date", "cash_price",
            "cash_currency", "points_price", "cents_per_point"];
        const lines = [headers.join(",")];
        for (const r of data) {
            lines.push([
                r.hotel_code, r.brand_code, r.date,
                r.cash_price != null ? r.cash_price : "",
                r.cash_currency,
                r.points_price != null ? r.points_price : "",
                r.cents_per_point != null ? r.cents_per_point : "",
            ].join(","));
        }
        const blob = new Blob(["\ufeff" + lines.join("\n")], { type: "text/csv;charset=utf-8;" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = `ihg_prices_${new Date().toISOString().slice(0, 10)}.csv`;
        link.click();
        URL.revokeObjectURL(url);
        console.log(`📁 CSV 已下载 (${data.length} 条)`);
    }

    function exportToJSON(data, name) {
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = name || `ihg_data_${new Date().toISOString().slice(0, 10)}.json`;
        link.click();
        URL.revokeObjectURL(url);
    }

    // ============ 主流程 ============
    console.log("🏨 IHG Calendar Price Fetcher - 全量版本");
    console.log("================================================");

    // 构造窗口
    let windows;
    if (CONFIG.slidingWindow) {
        // 从明天开始,避免 "Invalid system range" (50027) 错误
        const tomorrow = new Date();
        tomorrow.setDate(tomorrow.getDate() + 1);
        windows = genWindows(tomorrow, CONFIG.daysAhead, CONFIG.windowSizeDays);
        console.log(`📅 滑动窗口模式: 从 ${isoDate(tomorrow)} 起未来 ${CONFIG.daysAhead} 天, 拆分为 ${windows.length} 个窗口`);
    } else {
        windows = [[CONFIG.startDate, CONFIG.endDate]];
        console.log(`📅 固定范围: ${CONFIG.startDate} ~ ${CONFIG.endDate}`);
    }
    console.log(`🏨 酒店: ${CONFIG.hotelCodes.length} 个`);
    console.log(`🔗 合并请求优先: ${CONFIG.tryCombinedRequest ? "是" : "否"}`);
    console.log(`📊 预计总请求: ${CONFIG.hotelCodes.length * windows.length} (合并成功时)`);
    console.log("================================================\n");

    const allCash = [];
    const allPoints = [];
    const rawSamples = [];

    let reqIdx = 0;
    const totalReqs = CONFIG.hotelCodes.length * windows.length;

    for (let hi = 0; hi < CONFIG.hotelCodes.length; hi++) {
        const code = CONFIG.hotelCodes[hi];
        console.log(`\n${"=".repeat(50)}`);
        console.log(`[酒店 ${hi + 1}/${CONFIG.hotelCodes.length}] ${code}`);
        console.log("=".repeat(50));

        for (let wi = 0; wi < windows.length; wi++) {
            reqIdx++;
            const [s, e] = windows[wi];
            console.log(`\n--- 窗口 ${wi + 1}/${windows.length} (${s} ~ ${e}) [${reqIdx}/${totalReqs}] ---`);

            const { cash, points } = await fetchWindow(code, s, e);

            if (cash) allCash.push(...parseCashResponse(cash));
            if (points) allPoints.push(...parsePointsResponse(points));

            // 保存第一个窗口的原始响应作为样本
            if (wi === 0) {
                if (cash) rawSamples.push({ hotel: code, window: `${s}~${e}`, type: "cash", data: cash });
                if (points && points !== cash) rawSamples.push({ hotel: code, window: `${s}~${e}`, type: "points", data: points });
            }

            const isLast = (hi === CONFIG.hotelCodes.length - 1) && (wi === windows.length - 1);
            if (!isLast) await sleep(CONFIG.delay);
        }
    }

    // 合并 & 导出
    const merged = mergePrices(allCash, allPoints);

    console.log("\n" + "=".repeat(50));
    console.log(`📊 共 ${merged.length} 条每日价格记录`);

    if (merged.length > 0) {
        console.log("\n📋 价格预览 (前 15 条):");
        console.table(merged.slice(0, 15).map(r => ({
            酒店: r.hotel_code,
            日期: r.date,
            现金: r.cash_price,
            货币: r.cash_currency,
            积分: r.points_price,
            CPP: r.cents_per_point,
        })));

        exportToCSV(merged);
    }

    if (rawSamples.length > 0) {
        exportToJSON(rawSamples, "ihg_raw_samples.json");
    }

    window.__IHG_PRICES = merged;
    window.__IHG_RAW = rawSamples;
    console.log("\n💡 数据已存储:");
    console.log("   window.__IHG_PRICES - 每日价格数组");
    console.log("   window.__IHG_RAW    - 原始响应样本");
    console.log("   查看: console.table(window.__IHG_PRICES)");

    return merged;
})();
