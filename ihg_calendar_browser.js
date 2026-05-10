/**
 * IHG Calendar Price Fetcher - 浏览器控制台版本
 * 获取每天最低现金价格 + 积分价格
 *
 * 使用方法:
 * 1. 在浏览器中打开 IHG 酒店页面 (如 ihg.com)
 * 2. 打开 DevTools (F12) -> Console
 * 3. 复制粘贴此脚本并运行
 *
 * API 响应结构:
 * 现金: data.hotels[].calendar[].lowestRate.totalAmount
 * 积分: data.hotels[].calendar[].offers[].totalPoints (ratePlanCode = IVAN*)
 */

(async function IHGCalendarFetcher() {
    'use strict';

    // ============ 配置 ============
    const CONFIG = {
        hotelCodes: [
            "BKKHB",  // InterContinental Bangkok
            // 添加更多酒店代码...
        ],

        startDate: "2026-05-10",
        endDate: "2026-07-10",

        lengthOfStay: 1,
        adults: 1,

        apiKey: "se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y",

        // 积分 rate plan codes
        pointsRatePlanCodes: ["IVAN1", "IVAN3", "IVAN5", "IVAN6", "IVAN7", "IVANI"],

        // 请求间隔(毫秒)
        delay: 2000,
    };
    // ============ 配置结束 ============

    const API_URL = "https://apis.ihg.com/availability/v1/calendar";

    function generateUUID() {
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
            const r = Math.random() * 16 | 0;
            const v = c === 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
        });
    }

    function sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    function buildPayload(hotelCode, pointsMode = false) {
        const payload = {
            hotelMnemonics: [hotelCode],
            startDate: CONFIG.startDate,
            endDate: CONFIG.endDate,
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

    async function fetchCalendar(hotelCode, pointsMode = false) {
        const payload = buildPayload(hotelCode, pointsMode);
        const modeStr = pointsMode ? "积分" : "现金";

        const headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json; charset=UTF-8",
            "ihg-language": "en-US",
            "ihg-sessionid": generateUUID(),
            "ihg-transactionid": generateUUID(),
            "x-ihg-api-key": CONFIG.apiKey,
        };

        try {
            const response = await fetch(API_URL, {
                method: "POST",
                headers: headers,
                body: JSON.stringify(payload),
                credentials: "include",
            });

            if (response.ok) {
                const data = await response.json();
                console.log(`✅ ${hotelCode} (${modeStr}): 获取成功`);
                return { hotelCode, data, success: true };
            } else {
                console.error(`❌ ${hotelCode} (${modeStr}): HTTP ${response.status}`);
                return { hotelCode, error: `HTTP ${response.status}`, success: false };
            }
        } catch (err) {
            console.error(`❌ ${hotelCode} (${modeStr}): ${err.message}`);
            return { hotelCode, error: err.message, success: false };
        }
    }

    function parseCashResponse(result) {
        /** 解析现金价格: data.hotels[].calendar[].lowestRate.totalAmount */
        const prices = [];
        const { hotelCode, data } = result;
        if (!data || !data.data) return prices;

        const hotels = data.data.hotels || [];
        for (const hotelEntry of hotels) {
            const info = hotelEntry.hotel || {};
            const code = info.hotelMnemonic || hotelCode;
            const brandCode = info.brandCode || "";
            const currency = info.propertyCurrency || "";

            for (const day of (hotelEntry.calendar || [])) {
                const date = day.start || "";
                const lowestRate = day.lowestRate;
                prices.push({
                    hotel_code: code,
                    brand_code: brandCode,
                    date: date,
                    cash_price: lowestRate ? parseFloat(lowestRate.totalAmount) || null : null,
                    cash_currency: lowestRate ? (lowestRate.currency || currency) : currency,
                });
            }
        }
        return prices;
    }

    function parsePointsResponse(result) {
        /**
         * 解析积分价格: data.hotels[].calendar[].offers[].totalPoints
         * 只取 ratePlanCode 为 IVAN* 或 isRewardNight=true 的 offer
         */
        const prices = [];
        const { hotelCode, data } = result;
        if (!data || !data.data) return prices;

        const hotels = data.data.hotels || [];
        for (const hotelEntry of hotels) {
            const info = hotelEntry.hotel || {};
            const code = info.hotelMnemonic || hotelCode;
            const brandCode = info.brandCode || "";

            // 找出 reward rate plan codes
            const rewardCodes = new Set();
            for (const rp of (hotelEntry.ratePlans || [])) {
                if (rp.isRewardNight) rewardCodes.add(rp.code);
            }

            for (const day of (hotelEntry.calendar || [])) {
                const date = day.start || "";
                const offers = day.offers || [];

                // 找最低积分
                let lowestPoints = null;
                for (const offer of offers) {
                    const rpCode = offer.ratePlanCode || "";
                    const isPointsOffer = rewardCodes.has(rpCode) || rpCode.startsWith("IVAN");

                    if (isPointsOffer && offer.totalPoints != null) {
                        const pts = parseFloat(offer.totalPoints);
                        if (!isNaN(pts) && (lowestPoints === null || pts < lowestPoints)) {
                            lowestPoints = pts;
                        }
                    }
                }

                prices.push({
                    hotel_code: code,
                    brand_code: brandCode,
                    date: date,
                    points_price: lowestPoints,
                });
            }
        }
        return prices;
    }

    function mergePrices(cashPrices, pointsPrices) {
        /** 合并现金和积分价格 */
        const cashMap = {};
        for (const row of cashPrices) {
            cashMap[`${row.hotel_code}_${row.date}`] = row;
        }
        const pointsMap = {};
        for (const row of pointsPrices) {
            pointsMap[`${row.hotel_code}_${row.date}`] = row;
        }

        const allKeys = new Set([...Object.keys(cashMap), ...Object.keys(pointsMap)]);
        const merged = [];

        for (const key of [...allKeys].sort()) {
            const cash = cashMap[key] || {};
            const points = pointsMap[key] || {};
            merged.push({
                hotel_code: cash.hotel_code || points.hotel_code,
                brand_code: cash.brand_code || points.brand_code || "",
                date: cash.date || points.date,
                cash_price: cash.cash_price || null,
                cash_currency: cash.cash_currency || "",
                points_price: points.points_price || null,
            });
        }

        return merged;
    }

    function exportToCSV(allPrices) {
        if (allPrices.length === 0) return;

        const headers = ["hotel_code", "brand_code", "date", "cash_price", "cash_currency", "points_price"];
        const csvLines = [headers.join(",")];

        for (const row of allPrices) {
            csvLines.push([
                row.hotel_code,
                row.brand_code,
                row.date,
                row.cash_price !== null ? row.cash_price : "",
                row.cash_currency,
                row.points_price !== null ? row.points_price : "",
            ].join(","));
        }

        const csvContent = csvLines.join("\n");
        const blob = new Blob(["\ufeff" + csvContent], { type: "text/csv;charset=utf-8;" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = `ihg_prices_${new Date().toISOString().slice(0, 10)}.csv`;
        link.click();
        URL.revokeObjectURL(url);
        console.log(`📁 CSV 已下载 (${allPrices.length} 条)`);
    }

    function exportToJSON(data) {
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = `ihg_raw_data_${new Date().toISOString().slice(0, 10)}.json`;
        link.click();
        URL.revokeObjectURL(url);
        console.log("📁 JSON 原始数据已下载");
    }

    // ============ 主流程 ============
    console.log("🏨 IHG Calendar Price Fetcher - 现金 + 积分");
    console.log("================================================");
    console.log(`酒店数量: ${CONFIG.hotelCodes.length}`);
    console.log(`日期范围: ${CONFIG.startDate} ~ ${CONFIG.endDate}`);
    console.log("================================================\n");

    const allCashPrices = [];
    const allPointsPrices = [];
    const allRawResults = [];

    for (let i = 0; i < CONFIG.hotelCodes.length; i++) {
        const code = CONFIG.hotelCodes[i];
        console.log(`\n[${i + 1}/${CONFIG.hotelCodes.length}] 酒店: ${code}`);

        // 获取现金价格
        const cashResult = await fetchCalendar(code, false);
        allRawResults.push({ type: "cash", ...cashResult });
        if (cashResult.success) {
            allCashPrices.push(...parseCashResponse(cashResult));
        }

        await sleep(CONFIG.delay);

        // 获取积分价格
        const pointsResult = await fetchCalendar(code, true);
        allRawResults.push({ type: "points", ...pointsResult });
        if (pointsResult.success) {
            allPointsPrices.push(...parsePointsResponse(pointsResult));
        }

        if (i < CONFIG.hotelCodes.length - 1) {
            await sleep(CONFIG.delay);
        }
    }

    // 合并
    const mergedPrices = mergePrices(allCashPrices, allPointsPrices);

    // 输出汇总
    console.log("\n================================================");
    console.log(`📊 共 ${mergedPrices.length} 条日价格记录`);

    if (mergedPrices.length > 0) {
        console.log("\n📋 价格预览 (前15条):");
        console.table(mergedPrices.slice(0, 15).map(p => ({
            酒店: p.hotel_code,
            日期: p.date,
            现金价: p.cash_price,
            货币: p.cash_currency,
            积分价: p.points_price,
        })));

        exportToCSV(mergedPrices);
    }

    exportToJSON(allRawResults);

    // 全局变量
    window.__IHG_PRICES = mergedPrices;
    window.__IHG_RAW = allRawResults;
    console.log("\n💡 数据已存储:");
    console.log("   window.__IHG_PRICES  - 合并后的每日价格");
    console.log("   window.__IHG_RAW     - 原始响应数据");
    console.log("   查看: console.table(window.__IHG_PRICES)");

    return mergedPrices;
})();
