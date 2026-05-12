/**
 * IHG Cookie 提取助手
 * 用法: 
 * 1. 浏览器打开 https://www.ihg.com/intercontinental/hotels/us/en/find-hotels/select-roomrate?...&qSlH=BKKHB&...
 * 2. 确保已点击过 Search 按钮 (建立会话)
 * 3. F12 → Console, 粘贴此脚本运行
 * 4. 脚本会自动把 cookie 复制到剪贴板, 直接贴到 Python 脚本的 COOKIES 变量里
 */

(function () {
    const cookies = document.cookie;
    console.log("📋 当前页面 Cookie:");
    console.log(cookies);

    // 尝试复制到剪贴板
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(cookies)
            .then(() => {
                console.log("\n✅ Cookie 已复制到剪贴板!");
                console.log("   请粘贴到 ihg_calendar_price.py 的 COOKIES 变量中");
            })
            .catch(err => {
                console.warn("⚠️ 无法自动复制, 请手动复制上方的 cookie 字符串");
            });
    } else {
        console.log("⚠️ 浏览器不支持自动复制, 请手动选中并复制上方的 cookie 字符串");
    }

    // 显示关键 cookie (Akamai 防护相关)
    const important = ["_abck", "bm_sz", "bm_sv", "ak_bmsc", "IHG-USER-TOKEN"];
    console.log("\n🔑 关键 Cookie 状态:");
    for (const name of important) {
        const has = cookies.includes(name + "=");
        console.log(`   ${has ? "✅" : "❌"} ${name}`);
    }

    window.__IHG_COOKIE = cookies;
})();
