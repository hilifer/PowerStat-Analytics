/**
 * PowerStat-Analytics 前端逻辑
 */

document.addEventListener("DOMContentLoaded", function () {

    // ---- 刷新按钮防重复 + 状态轮询 ----
    const refreshBtn = document.getElementById("refreshBtn");
    const refreshStatus = document.getElementById("refreshStatus");
    const refreshProgress = document.getElementById("refreshProgress");
    const refreshSpinner = document.getElementById("refreshSpinner");

    if (refreshBtn) {
        refreshBtn.addEventListener("click", function (e) {
            // 按钮在 form 里，不需要 prevent default（让表单提交）
            refreshBtn.disabled = true;
            refreshBtn.textContent = "刷新中…";
            // 启动状态轮询
            startPolling();
        });

        // 页面加载时检查是否有正在运行的刷新任务
        checkRefreshStatus();
    }

    function startPolling() {
        if (refreshStatus) refreshStatus.style.display = "flex";
        const interval = setInterval(function () {
            fetch("/api/refresh-status")
                .then(r => r.json())
                .then(data => {
                    if (refreshProgress) {
                        refreshProgress.textContent = data.progress || "处理中…";
                    }
                    if (!data.running) {
                        clearInterval(interval);
                        if (refreshSpinner) refreshSpinner.style.display = "none";
                        if (data.result && !data.result.error) {
                            refreshProgress.textContent =
                                `完成！新增 ${data.result.new} 个附件，跳过 ${data.result.skipped} 个已处理，入库 ${data.result.meters_added} 条记录。`;
                            // 3秒后刷新页面
                            setTimeout(() => location.reload(), 3000);
                        } else if (data.result && data.result.error) {
                            refreshProgress.textContent = "错误: " + data.result.error;
                            if (refreshBtn) {
                                refreshBtn.disabled = false;
                                refreshBtn.textContent = "刷新邮件";
                            }
                        }
                    }
                })
                .catch(() => {});
        }, 1500);
    }

    function checkRefreshStatus() {
        fetch("/api/refresh-status")
            .then(r => r.json())
            .then(data => {
                if (data.running) {
                    if (refreshBtn) {
                        refreshBtn.disabled = true;
                        refreshBtn.textContent = "刷新中…";
                    }
                    startPolling();
                }
            })
            .catch(() => {});
    }

    // ---- 筛选表单自动提交 ----
    document.querySelectorAll(".auto-submit").forEach(function (el) {
        el.addEventListener("change", function () {
            this.closest("form").submit();
        });
    });

    // ---- Flash 自动消失 ----
    document.querySelectorAll(".flash").forEach(function (el) {
        setTimeout(() => {
            el.style.opacity = "0";
            el.style.transition = "opacity 0.3s";
            setTimeout(() => el.remove(), 300);
        }, 6000);
    });
});
