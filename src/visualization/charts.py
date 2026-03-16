"""可视化模块：基于 matplotlib 的本地图表生成。

功能：
- 折线图：单表/多表的月度尖峰平谷电量趋势
- 柱状图：上网表 vs 发电表的电量及电费对比
- 支持按项目或用户编号筛选
"""

from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # 无 GUI 环境
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import numpy as np

from src.config_loader import config
from src.data.models import Database
from src.logger import log


def _setup_chinese_font():
    """配置中文字体支持。"""
    viz_cfg = config.get("visualization") or {}
    font_family = viz_cfg.get("font_family", "SimHei")
    fallback = viz_cfg.get("fallback_fonts", [])

    # 刷新字体缓存以发现新安装的字体
    fm.fontManager.addfont("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc") if Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc").exists() else None

    # 尝试查找可用的中文字体
    available = {f.name for f in fm.fontManager.ttflist}
    candidates = [font_family] + fallback + ["WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "Noto Sans CJK SC", "Noto Sans SC"]
    for font in candidates:
        if font in available:
            plt.rcParams["font.family"] = "sans-serif"
            plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            log.debug("使用字体: %s", font)
            return

    log.warning("未找到中文字体，图表中文可能显示异常。可安装字体包解决。")
    plt.rcParams["axes.unicode_minus"] = False


class ChartGenerator:
    """图表生成器。"""

    def __init__(self, db: Database):
        self.db = db
        viz_cfg = config.get("visualization") or {}
        self.output_dir = Path(viz_cfg.get("output_dir", "output/charts"))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.dpi = viz_cfg.get("dpi", 150)
        self.figsize = tuple(viz_cfg.get("figure_size", [14, 8]))

        style = viz_cfg.get("style", "seaborn-v0_8-whitegrid")
        try:
            plt.style.use(style)
        except OSError:
            plt.style.use("ggplot")

        _setup_chinese_font()

    def generate_all(self, project_name: str = None, user_id: str = None):
        """生成所有图表。"""
        bills = self.db.get_monthly_bill(project_name=project_name, user_id=user_id)
        if not bills:
            log.info("无数据，跳过图表生成")
            return

        suffix = ""
        if project_name:
            suffix += f"_proj_{project_name}"
        if user_id:
            suffix += f"_user_{user_id}"

        self.line_chart_monthly_trend(bills, suffix)
        self.bar_chart_meter_type_comparison(bills, suffix)
        self.bar_chart_monthly_amount(bills, suffix)
        self.stacked_bar_tou_breakdown(bills, suffix)

        log.info("图表已生成到: %s", self.output_dir)

    def line_chart_monthly_trend(self, bills: list[dict], suffix: str = ""):
        """折线图：月度尖峰平谷电量趋势（按电表分组）。"""
        fig, ax = plt.subplots(figsize=self.figsize)

        # 按电表分组
        meters = {}
        for b in bills:
            key = f"{b['meter_number']} ({b['meter_type']})"
            meters.setdefault(key, []).append(b)

        for meter_key, records in meters.items():
            records.sort(key=lambda x: x["reading_month"])
            months = [r["reading_month"] for r in records]
            total = [r.get("total_kwh") or 0 for r in records]
            ax.plot(months, total, marker="o", label=meter_key, linewidth=2)

        ax.set_xlabel("月份")
        ax.set_ylabel("总电量 (kWh)")
        ax.set_title("月度电量趋势")
        ax.legend(bbox_to_anchor=(1.05, 1), loc="upper left", fontsize=8)
        ax.tick_params(axis="x", rotation=45)
        fig.tight_layout()

        path = self.output_dir / f"trend_total{suffix}.png"
        fig.savefig(str(path), dpi=self.dpi, bbox_inches="tight")
        plt.close(fig)
        log.info("  图表: %s", path.name)

        # 分时段趋势
        self._line_chart_tou_detail(bills, suffix)

    def _line_chart_tou_detail(self, bills: list[dict], suffix: str):
        """折线图：分时段（尖峰平谷）趋势。"""
        fig, axes = plt.subplots(2, 2, figsize=(self.figsize[0], self.figsize[1] * 1.2))
        segments = [
            ("sharp_peak", "尖峰", axes[0, 0]),
            ("peak", "峰", axes[0, 1]),
            ("flat", "平", axes[1, 0]),
            ("valley", "谷", axes[1, 1]),
        ]

        meters = {}
        for b in bills:
            key = f"{b['meter_number']}"
            meters.setdefault(key, []).append(b)

        for field, title, ax in segments:
            for meter_key, records in meters.items():
                records_sorted = sorted(records, key=lambda x: x["reading_month"])
                months = [r["reading_month"] for r in records_sorted]
                values = [r.get(field) or 0 for r in records_sorted]
                ax.plot(months, values, marker="s", label=meter_key, linewidth=1.5)
            ax.set_title(f"{title}电量趋势")
            ax.set_xlabel("月份")
            ax.set_ylabel("kWh")
            ax.tick_params(axis="x", rotation=45, labelsize=7)
            ax.legend(fontsize=6)

        fig.suptitle("分时段电量趋势", fontsize=14, y=1.02)
        fig.tight_layout()

        path = self.output_dir / f"trend_tou{suffix}.png"
        fig.savefig(str(path), dpi=self.dpi, bbox_inches="tight")
        plt.close(fig)

    def bar_chart_meter_type_comparison(self, bills: list[dict], suffix: str = ""):
        """柱状图：上网表 vs 发电表的电量对比。"""
        grid_data = {"sharp_peak": 0, "peak": 0, "flat": 0, "valley": 0}
        gen_data = {"sharp_peak": 0, "peak": 0, "flat": 0, "valley": 0}

        for b in bills:
            target = grid_data if b.get("meter_type") == "上网表" else gen_data
            for k in target:
                target[k] += b.get(k) or 0

        fig, ax = plt.subplots(figsize=self.figsize)
        labels = ["尖峰", "峰", "平", "谷"]
        keys = ["sharp_peak", "peak", "flat", "valley"]
        x = np.arange(len(labels))
        width = 0.35

        grid_vals = [grid_data[k] for k in keys]
        gen_vals = [gen_data[k] for k in keys]

        bars1 = ax.bar(x - width / 2, grid_vals, width, label="上网表", color="#4C72B0")
        bars2 = ax.bar(x + width / 2, gen_vals, width, label="发电表", color="#DD8452")

        ax.set_xlabel("时段")
        ax.set_ylabel("电量 (kWh)")
        ax.set_title("上网表 vs 发电表 电量对比")
        ax.set_xticks(x)
        ax.set_xticklabels(labels)
        ax.legend()

        # 数值标签
        for bars in [bars1, bars2]:
            for bar in bars:
                h = bar.get_height()
                if h > 0:
                    ax.annotate(f"{h:.1f}", xy=(bar.get_x() + bar.get_width() / 2, h),
                                ha="center", va="bottom", fontsize=8)

        fig.tight_layout()
        path = self.output_dir / f"comparison_type{suffix}.png"
        fig.savefig(str(path), dpi=self.dpi, bbox_inches="tight")
        plt.close(fig)
        log.info("  图表: %s", path.name)

    def bar_chart_monthly_amount(self, bills: list[dict], suffix: str = ""):
        """柱状图：月度电费金额。"""
        monthly = {}
        for b in bills:
            month = b["reading_month"]
            mtype = b.get("meter_type", "未知")
            key = (month, mtype)
            monthly.setdefault(key, 0)
            monthly[key] += b.get("total_amount") or 0

        if not monthly:
            return

        months = sorted(set(k[0] for k in monthly))
        grid_amounts = [monthly.get((m, "上网表"), 0) for m in months]
        gen_amounts = [monthly.get((m, "发电表"), 0) for m in months]

        fig, ax = plt.subplots(figsize=self.figsize)
        x = np.arange(len(months))
        width = 0.35

        ax.bar(x - width / 2, grid_amounts, width, label="上网表电费", color="#4C72B0")
        ax.bar(x + width / 2, gen_amounts, width, label="发电表电费", color="#DD8452")

        ax.set_xlabel("月份")
        ax.set_ylabel("金额 (元)")
        ax.set_title("月度电费金额对比")
        ax.set_xticks(x)
        ax.set_xticklabels(months, rotation=45)
        ax.legend()
        fig.tight_layout()

        path = self.output_dir / f"amount_monthly{suffix}.png"
        fig.savefig(str(path), dpi=self.dpi, bbox_inches="tight")
        plt.close(fig)
        log.info("  图表: %s", path.name)

    def stacked_bar_tou_breakdown(self, bills: list[dict], suffix: str = ""):
        """堆叠柱状图：各月尖峰平谷电量占比。"""
        monthly = {}
        for b in bills:
            month = b["reading_month"]
            monthly.setdefault(month, {"sharp_peak": 0, "peak": 0, "flat": 0, "valley": 0})
            for k in monthly[month]:
                monthly[month][k] += b.get(k) or 0

        if not monthly:
            return

        months = sorted(monthly.keys())
        fig, ax = plt.subplots(figsize=self.figsize)

        sp = [monthly[m]["sharp_peak"] for m in months]
        pk = [monthly[m]["peak"] for m in months]
        fl = [monthly[m]["flat"] for m in months]
        vl = [monthly[m]["valley"] for m in months]

        x = np.arange(len(months))
        ax.bar(x, sp, label="尖峰", color="#C44E52")
        ax.bar(x, pk, bottom=sp, label="峰", color="#DD8452")
        bottom2 = [a + b for a, b in zip(sp, pk)]
        ax.bar(x, fl, bottom=bottom2, label="平", color="#8172B3")
        bottom3 = [a + b for a, b in zip(bottom2, fl)]
        ax.bar(x, vl, bottom=bottom3, label="谷", color="#55A868")

        ax.set_xlabel("月份")
        ax.set_ylabel("电量 (kWh)")
        ax.set_title("月度分时段电量构成")
        ax.set_xticks(x)
        ax.set_xticklabels(months, rotation=45)
        ax.legend()
        fig.tight_layout()

        path = self.output_dir / f"tou_breakdown{suffix}.png"
        fig.savefig(str(path), dpi=self.dpi, bbox_inches="tight")
        plt.close(fig)
        log.info("  图表: %s", path.name)
