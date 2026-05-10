/**
 * IHG Calendar Price Fetcher - 浏览器控制台版本
 * 
 * 使用方法:
 * 1. 在浏览器中打开 IHG 酒店页面 (如 ihg.com)
 * 2. 打开 DevTools (F12) -> Console
 * 3. 复制粘贴此脚本并运行
 * 
 * 优势: 直接利用浏览器已有的 Cookie 和会话，无需额外配置
 */

(async function IHGCalendarFetcher() {
    'use strict';

    // ============ 配置 ============
    const CONFIG = {
        // 要查询的酒店代码列表
        hotelCodes: [
            "BKKHB",  // InterContinental Bangkok
            "FAICW",  // 示例
            // 添加更多...
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
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
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
                credentials: "include",  // 携带 cookie
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
         * 解析日历响应，提取每日价格
         * 注意: 需要根据实际响应结构调整此函数
         */
        const prices = [];
        const { hotelCode, data } = result;

        if (!data) return prices;

        // 先打印响应结构的顶层键，方便调试
        console.log(`📋 ${hotelCode} 响应顶层键:`, Object.keys(data));

        // 尝试解析 - 以下是几种可能的响应结构
        // 结构1: { hotelCalendars: [{ hotelMnemonic, calendar: [...] }] }
        const hotelCalendars = data.hotelCalendars || data.calendars || data.data || [];
        
        if (Array.isArray(hotelCalendars)) {
            for (const hotelCal of hotelCalendars) {
                const code = hotelCal.hotelMnemonic || hotelCal.hotelCode || hotelCode;
                const days = hotelCal.calendar || hotelCal.dates || hotelCal.days || [];
                
                for (const day of days) {
                    prices.push({
                        hotel_code: code,
                        date: day.date || day.startDate,
                        cash_price: extractCashPrice(day),
                        points_price: extractPointsPrice(day),
                        available: day.available !== false,
                        raw: day,  // 保留原始数据供调试
                    });
                }
            }
        }

        // 如果上面没解析出来，保存原始数据
        if (prices.length === 0) {
            console.warn(`⚠️ ${hotelCode}: 无法自动解析，请查看原始数据`);
            console.log("原始响应:", JSON.stringify(data, null, 2).substring(0, 3000));
        }

        return prices;
    }

    function extractCashPrice(dayData) {
        // 尝试各种可能的现金价格字段
        if (dayData.lowestOffer) {
            return dayData.lowestOffer.amount || dayData.lowestOffer.price;
        }
        if (dayData.lowest) {
            return dayData.lowest.amount || dayData.lowest.price;
        }
        if (dayData.amounts) {
            return dayData.amounts.afterTax || dayData.amounts.beforeTax;
        }
        if (dayData.cashPrice !== undefined) return dayData.cashPrice;
        if (dayData.price !== undefined) return dayData.price;
        return null;
    }

    function extractPointsPrice(dayData) {
        // 尝试各种可能的积分价格字段
        if (dayData.pointsOffer) {
            return dayData.pointsOffer.points || dayData.pointsOffer.amount;
        }
        if (dayData.lowestPointsOffer) {
            return dayData.lowestPointsOffer.points || dayData.lowestPointsOffer.amount;
        }
        if (dayData.points !== undefined) return dayData.points;
        if (dayData.pointsPrice !== undefined) return dayData.pointsPrice;
        return null;
    }

    function exportToCSV(allPrices) {
        if (allPrices.length === 0) {
            console.warn("没有数据可导出");
            return;
        }

        const headers = ["hotel_code", "date", "cash_price", "points_price", "available"];
        const csvLines = [headers.join(",")];

        for (const row of allPrices) {
            csvLines.push([
                row.hotel_code,
                row.date,
                row.cash_price || "",
                row.points_price || "",
                row.available,
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
    console.log("🏨 IHG Calendar Price Fetcher");
    console.log("================================");
    console.log(`酒店数量: ${CONFIG.hotelCodes.length}`);
    console.log(`日期范围: ${CONFIG.startDate} ~ ${CONFIG.endDate}`);
    console.log("================================\n");

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

        // 间隔
        if (i < CONFIG.hotelCodes.length - 1) {
            await sleep(CONFIG.delay);
        }
    }

    // 输出汇总
    console.log("\n================================");
    console.log(`📊 汇总: 共获取 ${allResults.filter(r => r.success).length}/${CONFIG.hotelCodes.length} 个酒店`);
    console.log(`📊 解析出 ${allPrices.length} 条日价格记录`);

    // 导出数据
    if (allPrices.length > 0) {
        exportToCSV(allPrices);
    }
    exportToJSON(allResults);

    // 存储到全局变量方便后续分析
    window.__IHG_RESULTS = allResults;
    window.__IHG_PRICES = allPrices;
    console.log("\n💡 数据已存储到 window.__IHG_RESULTS 和 window.__IHG_PRICES");
    console.log("   可以在控制台中查看: console.table(window.__IHG_PRICES)");

    return { results: allResults, prices: allPrices };
})();
