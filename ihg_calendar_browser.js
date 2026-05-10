/**
 * IHG Calendar Price Fetcher - 浏览器控制台版本
 * 获取每天最低现金价格
 *
 * 使用方法:
 * 1. 在浏览器中打开 IHG 酒店页面 (如 ihg.com)
 * 2. 打开 DevTools (F12) -> Console
 * 3. 复制粘贴此脚本并运行
 *
 * 优势: 直接利用浏览器已有的 Cookie 和会话，无需额外配置
 *
 * 实际 API 响应结构:
 * {
 *   "data": {
 *     "hotels": [{
 *       "hotel": { "brandCode": "IC", "hotelMnemonic": "BKKHB", "propertyCurrency": "THB" },
 *       "calendar": [{
 *         "start": "2026-05-09",
 *         "end": "2026-05-09",
 *         "lowestRate": {
 *           "totalAmount": "6270.00",
 *           "averageDailyAmount": "6270.00",
 *           "currency": "THB"
 *         }
 *       }]
 *     }]
 *   }
 * }
 */

(async function IHGCalendarFetcher() {
    'use strict';

    // ============ 配置 ============
    const CONFIG = {
        // 要查询的酒店代码列表
        hotelCodes: [
            "BKKHB",  // InterContinental Bangkok
            // 添加更多酒店代码...
        ],

        // 日期范围 (API 支持最多约2个月)
        startDate: "2026-05-10",
        endDate: "2026-07-10",

        // 住宿配置
        lengthOfStay: 1,
        adults: 1,

        // API Key
        apiKey: "se9ym5iAzaW8pxfBjkmgbuGjJcr3Pj6Y",

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

    async function fetchCalendar(hotelCode) {
        const payload = {
            hotelMnemonics: [hotelCode],
            startDate: CONFIG.startDate,
            endDate: CONFIG.endDate,
            lengthOfStay: CONFIG.lengthOfStay,
            guestCounts: [{
                otaCode: "AQC10",
                count: CONFIG.adults
            }],
            options: {
                includeSellStrategy: "followChannel",
                returnAmountsAfterTaxForLowestOffer: true,
                returnAverages: true,
                lowestOfferPerRatePlan: true,
                identifyLowestOfferPerRatePlan: true,
            }
        };

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
                console.log(`✅ ${hotelCode}: 获取成功`);
                return { hotelCode, data, success: true };
            } else {
                console.error(`❌ ${hotelCode}: HTTP ${response.status}`);
                return { hotelCode, error: `HTTP ${response.status}`, success: false };
            }
        } catch (err) {
            console.error(`❌ ${hotelCode}: ${err.message}`);
            return { hotelCode, error: err.message, success: false };
        }
    }

    function parseCalendarData(result) {
        /**
         * 解析响应，提取每天最低价格
         * 路径: data.hotels[].calendar[].lowestRate.totalAmount
         */
        const prices = [];
        const { hotelCode, data } = result;

        if (!data || !data.data) {
            console.warn(`⚠️ ${hotelCode}: 响应中无 data 字段`);
            return prices;
        }

        const hotels = data.data.hotels || [];

        for (const hotelEntry of hotels) {
            const hotelInfo = hotelEntry.hotel || {};
            const code = hotelInfo.hotelMnemonic || hotelCode;
            const brandCode = hotelInfo.brandCode || "";
            const propertyCurrency = hotelInfo.propertyCurrency || "";

            const calendar = hotelEntry.calendar || [];

            for (const day of calendar) {
                const date = day.start || "";
                const lowestRate = day.lowestRate || null;

                if (lowestRate) {
                    prices.push({
                        hotel_code: code,
                        brand_code: brandCode,
                        date: date,
                        lowest_price: parseFloat(lowestRate.totalAmount) || null,
                        average_daily_price: parseFloat(lowestRate.averageDailyAmount) || null,
                        currency: lowestRate.currency || propertyCurrency,
                    });
                } else {
                    // 当天无可用房间
                    prices.push({
                        hotel_code: code,
                        brand_code: brandCode,
                        date: date,
                        lowest_price: null,
                        average_daily_price: null,
                        currency: propertyCurrency,
                    });
                }
            }
        }

        if (prices.length === 0) {
            console.warn(`⚠️ ${hotelCode}: 未解析出价格数据`);
            console.log("原始响应键:", Object.keys(data));
        }

        return prices;
    }

    function exportToCSV(allPrices) {
        if (allPrices.length === 0) {
            console.warn("没有数据可导出");
            return;
        }

        const headers = ["hotel_code", "brand_code", "date", "lowest_price", "average_daily_price", "currency"];
        const csvLines = [headers.join(",")];

        for (const row of allPrices) {
            csvLines.push([
                row.hotel_code,
                row.brand_code,
                row.date,
                row.lowest_price !== null ? row.lowest_price : "",
                row.average_daily_price !== null ? row.average_daily_price : "",
                row.currency,
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
        console.log(`📁 CSV 文件已下载 (${allPrices.length} 条记录)`);
    }

    function exportToJSON(allResults) {
        const jsonStr = JSON.stringify(allResults, null, 2);
        const blob = new Blob([jsonStr], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = `ihg_raw_data_${new Date().toISOString().slice(0, 10)}.json`;
        link.click();
        URL.revokeObjectURL(url);
        console.log("📁 JSON 原始数据已下载");
    }

    // ============ 主流程 ============
    console.log("🏨 IHG Calendar Price Fetcher - 每日最低价格");
    console.log("================================================");
    console.log(`酒店数量: ${CONFIG.hotelCodes.length}`);
    console.log(`日期范围: ${CONFIG.startDate} ~ ${CONFIG.endDate}`);
    console.log("================================================\n");

    const allResults = [];
    const allPrices = [];

    for (let i = 0; i < CONFIG.hotelCodes.length; i++) {
        const code = CONFIG.hotelCodes[i];
        console.log(`[${i + 1}/${CONFIG.hotelCodes.length}] 获取 ${code}...`);

        const result = await fetchCalendar(code);
        allResults.push(result);

        if (result.success) {
            const prices = parseCalendarData(result);
            allPrices.push(...prices);
        }

        if (i < CONFIG.hotelCodes.length - 1) {
            await sleep(CONFIG.delay);
        }
    }

    // 输出汇总
    console.log("\n================================================");
    console.log(`📊 汇总: 成功 ${allResults.filter(r => r.success).length}/${CONFIG.hotelCodes.length} 个酒店`);
    console.log(`📊 共 ${allPrices.length} 条每日价格记录`);

    // 打印表格预览
    if (allPrices.length > 0) {
        console.log("\n📋 价格预览 (前15条):");
        console.table(allPrices.slice(0, 15).map(p => ({
            酒店: p.hotel_code,
            日期: p.date,
            最低价: p.lowest_price,
            货币: p.currency,
        })));

        // 导出
        exportToCSV(allPrices);
    }

    exportToJSON(allResults);

    // 全局变量
    window.__IHG_RESULTS = allResults;
    window.__IHG_PRICES = allPrices;
    console.log("\n💡 数据已存储到 window.__IHG_PRICES");
    console.log("   查看全部: console.table(window.__IHG_PRICES)");

    return { results: allResults, prices: allPrices };
})();
