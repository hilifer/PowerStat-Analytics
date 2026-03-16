"""知识图谱模块：基于 NetworkX 构建电力数据关系网络。

节点类型：
    - project  : 项目（电站）
    - meter    : 电表
    - user     : 用户编号
    - month    : 月份

边类型（关系）：
    - 拥有     : project → meter
    - 关联用户 : meter   → user
    - 有读数   : meter   → month（属性含尖峰平谷电量）
    - 有单价   : user    → month（属性含尖峰平谷单价）

用途：
    - 数据完整性检测（孤立节点 = 缺失关联）
    - 跨实体查询（项目下所有电表所有月份的账单汇总）
    - 异常发现（同一用户关联了多个项目 / 电量突变 / 电价异常）
    - 电表类型分组分析（上网表 vs 发电表）
    - 账单计算（电量 × 倍率 × 单价）
    - 关系可视化
"""

import json
from collections import defaultdict
from pathlib import Path
from typing import Optional

import networkx as nx

from src.data.models import Database
from src.logger import log


class KnowledgeGraph:
    """电力数据知识图谱。"""

    def __init__(self, db: Database = None):
        self.db = db or Database()
        self.G = nx.DiGraph()

    # ================================================================
    # 构建
    # ================================================================

    def build(self):
        """从 SQLite 读取全部数据，构建知识图谱。"""
        self.G.clear()
        self._add_meters()
        self._add_readings()
        self._add_prices()

        stats = {
            "nodes": self.G.number_of_nodes(),
            "edges": self.G.number_of_edges(),
            "projects": len(self.get_nodes_by_type("project")),
            "meters": len(self.get_nodes_by_type("meter")),
            "users": len(self.get_nodes_by_type("user")),
            "months": len(self.get_nodes_by_type("month")),
        }
        log.info("知识图谱构建完成: %s", stats)
        return stats

    def _add_meters(self):
        """添加电表节点及其与项目、用户的关系。"""
        meters = self.db.get_meters()
        for m in meters:
            meter_id = f"meter:{m['meter_number']}"
            self.G.add_node(meter_id, type="meter", label=m["meter_number"],
                            meter_type=m.get("meter_type", "未知"),
                            multiplier=m.get("multiplier", 1.0),
                            discount=m.get("discount", 1.0),
                            asset_number=m.get("asset_number", ""),
                            is_locked=bool(m.get("is_locked", 0)),
                            db_id=m["id"])

            # 项目关系
            proj = m.get("project_name")
            if proj:
                proj_id = f"project:{proj}"
                self.G.add_node(proj_id, type="project", label=proj)
                self.G.add_edge(proj_id, meter_id, relation="拥有")

            # 用户关系
            uid = m.get("user_id")
            if uid:
                user_id = f"user:{uid}"
                self.G.add_node(user_id, type="user", label=uid)
                self.G.add_edge(meter_id, user_id, relation="关联用户")

    def _add_readings(self):
        """添加月度读数边：meter → month。"""
        with self.db.connection() as conn:
            rows = conn.execute("""
                SELECT r.*, m.meter_number
                FROM monthly_readings r
                JOIN meters m ON r.meter_id = m.id
            """).fetchall()
            rows = [dict(r) for r in rows]

        for r in rows:
            meter_id = f"meter:{r['meter_number']}"
            month_id = f"month:{r['reading_month']}"

            self.G.add_node(month_id, type="month", label=r["reading_month"])
            self.G.add_edge(meter_id, month_id, relation="有读数",
                            sharp_peak=r.get("sharp_peak"),
                            peak=r.get("peak"),
                            flat=r.get("flat"),
                            valley=r.get("valley"),
                            total_kwh=r.get("total_kwh"),
                            source_file=r.get("source_file", ""))

    def _add_prices(self):
        """添加单价边：user → month。"""
        with self.db.connection() as conn:
            rows = conn.execute("SELECT * FROM price_records").fetchall()
            rows = [dict(r) for r in rows]

        for r in rows:
            user_id = f"user:{r['user_id']}"
            month_id = f"month:{r['reading_month']}"

            # 确保节点存在
            if not self.G.has_node(user_id):
                self.G.add_node(user_id, type="user", label=r["user_id"])
            self.G.add_node(month_id, type="month", label=r["reading_month"])

            self.G.add_edge(user_id, month_id, relation="有单价",
                            sharp_peak_price=r["sharp_peak_price"],
                            peak_price=r["peak_price"],
                            flat_price=r["flat_price"],
                            valley_price=r["valley_price"])

    # ================================================================
    # 查询
    # ================================================================

    def get_nodes_by_type(self, node_type: str) -> list[str]:
        """获取指定类型的所有节点 ID。"""
        return [n for n, d in self.G.nodes(data=True) if d.get("type") == node_type]

    def get_neighbors(self, node_id: str, relation: str = None) -> list[dict]:
        """获取节点的邻居（支持按关系类型过滤）。"""
        results = []
        # 出边
        for _, target, data in self.G.out_edges(node_id, data=True):
            if relation and data.get("relation") != relation:
                continue
            results.append({"node": target, "direction": "out", **data,
                            **self.G.nodes[target]})
        # 入边
        for source, _, data in self.G.in_edges(node_id, data=True):
            if relation and data.get("relation") != relation:
                continue
            results.append({"node": source, "direction": "in", **data,
                            **self.G.nodes[source]})
        return results

    def get_project_network(self, project_name: str) -> dict:
        """获取项目的完整关系网络。"""
        proj_id = f"project:{project_name}"
        if not self.G.has_node(proj_id):
            return {"project": project_name, "meters": [], "error": "项目不存在"}

        result = {"project": project_name, "meters": []}

        # 项目 → 电表
        for _, meter_id, _ in self.G.out_edges(proj_id, data=True):
            meter_data = self.G.nodes[meter_id].copy()
            meter_info = {
                "id": meter_id,
                **meter_data,
                "user": None,
                "readings": [],
            }

            # 电表 → 用户
            for _, target, edata in self.G.out_edges(meter_id, data=True):
                if edata.get("relation") == "关联用户":
                    meter_info["user"] = self.G.nodes[target].get("label")
                elif edata.get("relation") == "有读数":
                    meter_info["readings"].append({
                        "month": self.G.nodes[target].get("label"),
                        "sharp_peak": edata.get("sharp_peak"),
                        "peak": edata.get("peak"),
                        "flat": edata.get("flat"),
                        "valley": edata.get("valley"),
                        "total_kwh": edata.get("total_kwh"),
                    })

            meter_info["readings"].sort(key=lambda x: x["month"])
            result["meters"].append(meter_info)

        return result

    def trace_meter(self, meter_number: str) -> dict:
        """追溯单个电表的完整关系链，含账单计算。"""
        meter_id = f"meter:{meter_number}"
        if not self.G.has_node(meter_id):
            return {"error": f"电表 {meter_number} 不存在"}

        node_data = self.G.nodes[meter_id]
        info = {"meter": meter_number, **node_data}
        multiplier = node_data.get("multiplier", 1.0) or 1.0
        discount = node_data.get("discount", 1.0) or 1.0

        # 所属项目
        info["projects"] = []
        for source, _, edata in self.G.in_edges(meter_id, data=True):
            if edata.get("relation") == "拥有":
                info["projects"].append(self.G.nodes[source].get("label"))

        # 关联用户
        info["users"] = []
        for _, target, edata in self.G.out_edges(meter_id, data=True):
            if edata.get("relation") == "关联用户":
                info["users"].append(self.G.nodes[target].get("label"))

        # 读数历史
        info["readings"] = []
        for _, target, edata in self.G.out_edges(meter_id, data=True):
            if edata.get("relation") == "有读数":
                info["readings"].append({
                    "month": self.G.nodes[target].get("label"),
                    "total_kwh": edata.get("total_kwh"),
                    "sharp_peak": edata.get("sharp_peak"),
                    "peak": edata.get("peak"),
                    "flat": edata.get("flat"),
                    "valley": edata.get("valley"),
                })
        info["readings"].sort(key=lambda x: x["month"])

        # 关联单价（通过用户节点）+ 账单计算
        price_map = {}  # month -> price data
        for user_label in info["users"]:
            user_node = f"user:{user_label}"
            for _, target, edata in self.G.out_edges(user_node, data=True):
                if edata.get("relation") == "有单价":
                    month = self.G.nodes[target].get("label")
                    price_map[month] = {
                        "sharp_peak_price": edata.get("sharp_peak_price"),
                        "peak_price": edata.get("peak_price"),
                        "flat_price": edata.get("flat_price"),
                        "valley_price": edata.get("valley_price"),
                    }

        info["prices"] = sorted(
            [{"month": m, **p} for m, p in price_map.items()],
            key=lambda x: x["month"]
        )

        # 账单计算：readings × multiplier × prices × discount
        info["bills"] = []
        for reading in info["readings"]:
            month = reading["month"]
            if month not in price_map:
                continue
            price = price_map[month]
            bill = {"month": month}
            total = 0.0
            for period, p_field in [("sharp_peak", "sharp_peak_price"),
                                     ("peak", "peak_price"),
                                     ("flat", "flat_price"),
                                     ("valley", "valley_price")]:
                kwh = reading.get(period) or 0
                unit_price = price.get(p_field) or 0
                amount = round(kwh * multiplier * unit_price * discount, 2)
                bill[f"{period}_amount"] = amount
                total += amount
            bill["total_amount"] = round(total, 2)
            info["bills"].append(bill)

        return info

    # ================================================================
    # 异常检测
    # ================================================================

    def detect_anomalies(self) -> dict:
        """全面检测数据异常，返回分类汇总。"""
        anomalies = {
            "orphan_meters": self._find_orphan_meters(),
            "missing_prices": self._find_missing_prices(),
            "missing_readings": self._find_missing_readings(),
            "reading_spikes": self._find_reading_spikes(),
            "multi_project_users": self._find_multi_project_users(),
            "price_anomalies": self._find_price_anomalies(),
            "meter_type_issues": self._find_meter_type_issues(),
        }

        total = sum(len(v) for v in anomalies.values())
        anomalies["total_issues"] = total
        if total:
            log.warning("知识图谱检测到 %d 个数据异常", total)
        else:
            log.info("知识图谱未检测到数据异常")
        return anomalies

    def _find_orphan_meters(self) -> list[dict]:
        """找出没有项目归属或没有用户的孤立电表。"""
        orphans = []
        for meter_id in self.get_nodes_by_type("meter"):
            data = self.G.nodes[meter_id]
            has_project = any(
                edata.get("relation") == "拥有"
                for _, _, edata in self.G.in_edges(meter_id, data=True)
            )
            has_user = any(
                edata.get("relation") == "关联用户"
                for _, _, edata in self.G.out_edges(meter_id, data=True)
            )
            issues = []
            if not has_project:
                issues.append("无项目归属")
            if not has_user:
                issues.append("无关联用户")
            if issues:
                orphans.append({
                    "meter": data.get("label"),
                    "meter_type": data.get("meter_type", "未知"),
                    "issues": issues,
                })
        return orphans

    def _find_missing_prices(self) -> list[dict]:
        """找出有读数但缺少对应单价的记录。"""
        missing = []
        for meter_id in self.get_nodes_by_type("meter"):
            user_ids = []
            for _, target, edata in self.G.out_edges(meter_id, data=True):
                if edata.get("relation") == "关联用户":
                    user_ids.append(target)

            if not user_ids:
                continue

            for _, month_node, edata in self.G.out_edges(meter_id, data=True):
                if edata.get("relation") != "有读数":
                    continue
                month_label = self.G.nodes[month_node].get("label")

                has_price = False
                for uid in user_ids:
                    if self.G.has_edge(uid, month_node):
                        edge_data = self.G.edges[uid, month_node]
                        if edge_data.get("relation") == "有单价":
                            has_price = True
                            break

                if not has_price:
                    missing.append({
                        "meter": self.G.nodes[meter_id].get("label"),
                        "month": month_label,
                        "issue": "有读数但缺单价，无法计算电费",
                    })
        return missing

    def _find_missing_readings(self) -> list[dict]:
        """找出同一项目下，某些电表某月有数据但其他电表没有的情况。"""
        missing = []
        for proj_id in self.get_nodes_by_type("project"):
            meter_ids = [
                target for _, target, edata in self.G.out_edges(proj_id, data=True)
                if edata.get("relation") == "拥有"
            ]
            if len(meter_ids) < 2:
                continue

            meter_months = {}
            for mid in meter_ids:
                months = set()
                for _, target, edata in self.G.out_edges(mid, data=True):
                    if edata.get("relation") == "有读数":
                        months.add(self.G.nodes[target].get("label"))
                meter_months[mid] = months

            all_months = set()
            for ms in meter_months.values():
                all_months |= ms

            for month in sorted(all_months):
                for mid in meter_ids:
                    if month not in meter_months.get(mid, set()):
                        missing.append({
                            "project": self.G.nodes[proj_id].get("label"),
                            "meter": self.G.nodes[mid].get("label"),
                            "month": month,
                            "issue": "同项目其他电表有此月数据，但此表缺失",
                        })
        return missing

    def _find_reading_spikes(self, threshold: float = 3.0) -> list[dict]:
        """检测电量突变（环比超过 threshold 倍）。"""
        spikes = []
        for meter_id in self.get_nodes_by_type("meter"):
            readings = []
            for _, target, edata in self.G.out_edges(meter_id, data=True):
                if edata.get("relation") == "有读数" and edata.get("total_kwh"):
                    readings.append({
                        "month": self.G.nodes[target].get("label"),
                        "total_kwh": edata["total_kwh"],
                    })
            readings.sort(key=lambda x: x["month"])

            for i in range(1, len(readings)):
                prev = readings[i - 1]["total_kwh"]
                curr = readings[i]["total_kwh"]
                if prev and prev > 0:
                    ratio = curr / prev
                    if ratio > threshold or (ratio > 0 and ratio < 1.0 / threshold):
                        spikes.append({
                            "meter": self.G.nodes[meter_id].get("label"),
                            "month": readings[i]["month"],
                            "prev_month": readings[i - 1]["month"],
                            "prev_kwh": prev,
                            "curr_kwh": curr,
                            "ratio": round(ratio, 2),
                            "issue": f"电量环比变化 {ratio:.1f}x（阈值 {threshold}x）",
                        })
        return spikes

    def _find_multi_project_users(self) -> list[dict]:
        """检测同一用户编号关联了多个项目的情况。"""
        results = []
        for user_id in self.get_nodes_by_type("user"):
            projects = set()
            for source, _, edata in self.G.in_edges(user_id, data=True):
                if edata.get("relation") == "关联用户":
                    for proj, _, pedata in self.G.in_edges(source, data=True):
                        if pedata.get("relation") == "拥有":
                            projects.add(self.G.nodes[proj].get("label"))

            if len(projects) > 1:
                results.append({
                    "user": self.G.nodes[user_id].get("label"),
                    "projects": sorted(projects),
                    "issue": f"用户关联了 {len(projects)} 个项目",
                })
        return results

    def _find_price_anomalies(self) -> list[dict]:
        """检测单价异常（价格不合理或同用户不同月价格波动过大）。"""
        anomalies = []
        for user_id in self.get_nodes_by_type("user"):
            prices_by_month = []
            for _, target, edata in self.G.out_edges(user_id, data=True):
                if edata.get("relation") != "有单价":
                    continue
                month = self.G.nodes[target].get("label")
                for field in ["sharp_peak_price", "peak_price", "flat_price", "valley_price"]:
                    val = edata.get(field)
                    if val is not None and (val < 0.1 or val > 3.0):
                        anomalies.append({
                            "user": self.G.nodes[user_id].get("label"),
                            "month": month,
                            "field": field,
                            "value": val,
                            "issue": f"{field} 值 {val} 超出合理范围 (0.1~3.0 元/kWh)",
                        })
                prices_by_month.append({"month": month, **{
                    f: edata.get(f) for f in ["sharp_peak_price", "peak_price",
                                               "flat_price", "valley_price"]
                }})

            # 检查价格顺序：通常 尖峰 > 峰 > 平 > 谷
            for pm in prices_by_month:
                vals = [pm.get(f) for f in ["sharp_peak_price", "peak_price",
                                             "flat_price", "valley_price"]]
                vals = [v for v in vals if v is not None]
                if len(vals) >= 4 and vals != sorted(vals, reverse=True):
                    anomalies.append({
                        "user": self.G.nodes[user_id].get("label"),
                        "month": pm["month"],
                        "issue": f"价格顺序异常（应 尖>峰>平>谷），实际: {vals}",
                    })

        return anomalies

    def _find_meter_type_issues(self) -> list[dict]:
        """检测电表类型相关异常。"""
        issues = []
        for proj_id in self.get_nodes_by_type("project"):
            meter_types = defaultdict(list)
            for _, meter_id, edata in self.G.out_edges(proj_id, data=True):
                if edata.get("relation") != "拥有":
                    continue
                mtype = self.G.nodes[meter_id].get("meter_type", "未知")
                meter_types[mtype].append(self.G.nodes[meter_id].get("label"))

            # 检查项目下是否有未知类型的电表
            unknown = meter_types.get("未知", [])
            if unknown:
                issues.append({
                    "project": self.G.nodes[proj_id].get("label"),
                    "meters": unknown,
                    "issue": f"{len(unknown)} 个电表类型未知，需确认是上网表还是发电表",
                })

        return issues

    # ================================================================
    # 统计分析
    # ================================================================

    def get_stats(self) -> dict:
        """生成图谱统计摘要。"""
        stats = {
            "total_nodes": self.G.number_of_nodes(),
            "total_edges": self.G.number_of_edges(),
            "node_types": {},
            "edge_types": defaultdict(int),
        }

        for _, data in self.G.nodes(data=True):
            ntype = data.get("type", "unknown")
            stats["node_types"][ntype] = stats["node_types"].get(ntype, 0) + 1

        for _, _, data in self.G.edges(data=True):
            stats["edge_types"][data.get("relation", "unknown")] += 1

        stats["edge_types"] = dict(stats["edge_types"])

        # 连通性
        undirected = self.G.to_undirected()
        components = list(nx.connected_components(undirected))
        stats["connected_components"] = len(components)
        stats["largest_component_size"] = max(len(c) for c in components) if components else 0
        stats["isolated_nodes"] = len(list(nx.isolates(self.G)))

        # 电表类型统计
        meter_types = defaultdict(int)
        for n, d in self.G.nodes(data=True):
            if d.get("type") == "meter":
                meter_types[d.get("meter_type", "未知")] += 1
        stats["meter_types"] = dict(meter_types)

        return stats

    def get_project_ranking(self) -> list[dict]:
        """按项目汇总电量和电表数，含电表类型分组。"""
        ranking = []
        for proj_id in self.get_nodes_by_type("project"):
            proj_label = self.G.nodes[proj_id].get("label")
            meter_count = 0
            total_kwh = 0.0
            months = set()
            type_counts = defaultdict(int)

            for _, meter_id, edata in self.G.out_edges(proj_id, data=True):
                if edata.get("relation") != "拥有":
                    continue
                meter_count += 1
                mtype = self.G.nodes[meter_id].get("meter_type", "未知")
                type_counts[mtype] += 1

                for _, month_node, rdata in self.G.out_edges(meter_id, data=True):
                    if rdata.get("relation") == "有读数":
                        total_kwh += rdata.get("total_kwh") or 0
                        months.add(self.G.nodes[month_node].get("label"))

            ranking.append({
                "project": proj_label,
                "meter_count": meter_count,
                "meter_types": dict(type_counts),
                "total_kwh": round(total_kwh, 2),
                "month_count": len(months),
                "month_range": f"{min(months)} ~ {max(months)}" if months else "-",
            })

        ranking.sort(key=lambda x: x["total_kwh"], reverse=True)
        return ranking

    def get_meter_type_summary(self) -> dict:
        """按电表类型汇总分析。"""
        summary = {}
        for meter_id in self.get_nodes_by_type("meter"):
            data = self.G.nodes[meter_id]
            mtype = data.get("meter_type", "未知")
            if mtype not in summary:
                summary[mtype] = {
                    "count": 0, "total_kwh": 0.0,
                    "meters": [], "months": set(),
                }

            summary[mtype]["count"] += 1
            summary[mtype]["meters"].append(data.get("label"))

            for _, target, edata in self.G.out_edges(meter_id, data=True):
                if edata.get("relation") == "有读数":
                    summary[mtype]["total_kwh"] += edata.get("total_kwh") or 0
                    summary[mtype]["months"].add(self.G.nodes[target].get("label"))

        # 序列化 set
        for v in summary.values():
            v["total_kwh"] = round(v["total_kwh"], 2)
            v["month_count"] = len(v["months"])
            del v["months"]

        return summary

    def calculate_project_bill(self, project_name: str) -> dict:
        """计算项目的月度账单汇总（通过图谱遍历）。"""
        proj_id = f"project:{project_name}"
        if not self.G.has_node(proj_id):
            return {"project": project_name, "error": "项目不存在", "bills": []}

        # 收集所有月份的账单
        monthly_bills = defaultdict(lambda: {
            "sharp_peak_amount": 0, "peak_amount": 0,
            "flat_amount": 0, "valley_amount": 0,
            "total_amount": 0, "meter_count": 0,
        })

        for _, meter_id, edata in self.G.out_edges(proj_id, data=True):
            if edata.get("relation") != "拥有":
                continue

            meter_data = self.G.nodes[meter_id]
            multiplier = meter_data.get("multiplier", 1.0) or 1.0
            discount = meter_data.get("discount", 1.0) or 1.0

            # 找用户节点以获取价格
            user_nodes = []
            for _, target, ed in self.G.out_edges(meter_id, data=True):
                if ed.get("relation") == "关联用户":
                    user_nodes.append(target)

            # 构建 price_map: month -> prices
            price_map = {}
            for uid in user_nodes:
                for _, target, ed in self.G.out_edges(uid, data=True):
                    if ed.get("relation") == "有单价":
                        month = self.G.nodes[target].get("label")
                        price_map[month] = {
                            "sharp_peak_price": ed.get("sharp_peak_price") or 0,
                            "peak_price": ed.get("peak_price") or 0,
                            "flat_price": ed.get("flat_price") or 0,
                            "valley_price": ed.get("valley_price") or 0,
                        }

            # 读数 × 倍率 × 单价 × 折扣
            for _, target, ed in self.G.out_edges(meter_id, data=True):
                if ed.get("relation") != "有读数":
                    continue
                month = self.G.nodes[target].get("label")
                if month not in price_map:
                    continue

                price = price_map[month]
                bill = monthly_bills[month]
                bill["meter_count"] += 1

                for period, p_field in [("sharp_peak", "sharp_peak_price"),
                                         ("peak", "peak_price"),
                                         ("flat", "flat_price"),
                                         ("valley", "valley_price")]:
                    kwh = ed.get(period) or 0
                    unit_price = price.get(p_field) or 0
                    amount = kwh * multiplier * unit_price * discount
                    bill[f"{period}_amount"] += amount
                    bill["total_amount"] += amount

        # 格式化输出
        bills = []
        for month in sorted(monthly_bills.keys()):
            b = monthly_bills[month]
            bills.append({
                "month": month,
                "meter_count": b["meter_count"],
                "sharp_peak_amount": round(b["sharp_peak_amount"], 2),
                "peak_amount": round(b["peak_amount"], 2),
                "flat_amount": round(b["flat_amount"], 2),
                "valley_amount": round(b["valley_amount"], 2),
                "total_amount": round(b["total_amount"], 2),
            })

        return {"project": project_name, "bills": bills}

    # ================================================================
    # 可视化
    # ================================================================

    def export_graph_image(self, output_path: str = None, project_name: str = None):
        """导出知识图谱为 PNG 图片。"""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.font_manager as fm

        # 中文字体
        wqy_path = "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"
        if Path(wqy_path).exists():
            fm.fontManager.addfont(wqy_path)
        available = {f.name for f in fm.fontManager.ttflist}
        for font in ["WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"]:
            if font in available:
                plt.rcParams["font.sans-serif"] = [font]
                break
        plt.rcParams["axes.unicode_minus"] = False

        # 选择子图
        if project_name:
            subgraph_nodes = self._get_project_subgraph_nodes(project_name)
            G = self.G.subgraph(subgraph_nodes)
            title = f"知识图谱 - {project_name}"
        else:
            G = self.G
            title = "知识图谱 - 全局"

        if G.number_of_nodes() == 0:
            log.warning("图谱为空，跳过导出")
            return None

        fig, ax = plt.subplots(figsize=(16, 12))

        # 布局
        try:
            pos = nx.spring_layout(G, k=2.0, iterations=50, seed=42)
        except Exception:
            pos = nx.circular_layout(G)

        # 按类型分色绘制
        type_styles = {
            "project": {"color": "#E74C3C", "size": 800, "shape": "s", "label": "项目"},
            "meter": {"color": "#3498DB", "size": 400, "shape": "o", "label": "电表"},
            "user": {"color": "#2ECC71", "size": 500, "shape": "d", "label": "用户"},
            "month": {"color": "#F39C12", "size": 300, "shape": "^", "label": "月份"},
        }

        for ntype, style in type_styles.items():
            nodes = [n for n in G.nodes() if G.nodes[n].get("type") == ntype]
            if not nodes:
                continue

            # 电表按类型细分颜色
            if ntype == "meter":
                grid_meters = [n for n in nodes if G.nodes[n].get("meter_type") == "上网表"]
                gen_meters = [n for n in nodes if G.nodes[n].get("meter_type") == "发电表"]
                unknown_meters = [n for n in nodes if G.nodes[n].get("meter_type") not in ("上网表", "发电表")]

                for sub_nodes, color, label_suffix in [
                    (grid_meters, "#3498DB", "上网表"),
                    (gen_meters, "#9B59B6", "发电表"),
                    (unknown_meters, "#95A5A6", "未知"),
                ]:
                    if sub_nodes:
                        node_pos = {n: pos[n] for n in sub_nodes}
                        nx.draw_networkx_nodes(G, node_pos, nodelist=sub_nodes,
                                               node_color=color,
                                               node_size=style["size"],
                                               node_shape=style["shape"],
                                               alpha=0.85, ax=ax)
            else:
                node_pos = {n: pos[n] for n in nodes}
                nx.draw_networkx_nodes(G, node_pos, nodelist=nodes,
                                       node_color=style["color"],
                                       node_size=style["size"],
                                       node_shape=style["shape"],
                                       alpha=0.85, ax=ax)

        # 边
        edge_colors = {
            "拥有": "#E74C3C",
            "关联用户": "#2ECC71",
            "有读数": "#3498DB",
            "有单价": "#F39C12",
        }
        for relation, color in edge_colors.items():
            edges = [(u, v) for u, v, d in G.edges(data=True) if d.get("relation") == relation]
            if edges:
                nx.draw_networkx_edges(G, pos, edgelist=edges,
                                       edge_color=color, alpha=0.5,
                                       arrows=True, arrowsize=15, ax=ax,
                                       connectionstyle="arc3,rad=0.1")

        # 标签
        labels = {}
        for n, d in G.nodes(data=True):
            label = d.get("label", n)
            if len(str(label)) > 15:
                label = str(label)[:12] + "..."
            labels[n] = label

        nx.draw_networkx_labels(G, pos, labels, font_size=7, ax=ax)

        # 图例（含电表类型分类）
        from matplotlib.lines import Line2D
        legend_elements = []

        # 项目/用户/月份
        for ntype in ["project", "user", "month"]:
            style = type_styles[ntype]
            nodes = [n for n in G.nodes() if G.nodes[n].get("type") == ntype]
            if nodes:
                legend_elements.append(
                    Line2D([0], [0], marker=style["shape"], color="w",
                           markerfacecolor=style["color"], markersize=10,
                           label=f"{style['label']} ({len(nodes)})")
                )

        # 电表按类型分列
        meter_nodes = [n for n in G.nodes() if G.nodes[n].get("type") == "meter"]
        if meter_nodes:
            for mtype, color in [("上网表", "#3498DB"), ("发电表", "#9B59B6"), ("未知", "#95A5A6")]:
                count = sum(1 for n in meter_nodes if G.nodes[n].get("meter_type") == mtype)
                if count:
                    legend_elements.append(
                        Line2D([0], [0], marker="o", color="w",
                               markerfacecolor=color, markersize=10,
                               label=f"{mtype} ({count})")
                    )

        for relation, color in edge_colors.items():
            edges = [(u, v) for u, v, d in G.edges(data=True) if d.get("relation") == relation]
            if edges:
                legend_elements.append(
                    Line2D([0], [0], color=color, linewidth=2,
                           label=f"{relation} ({len(edges)})")
                )

        ax.legend(handles=legend_elements, loc="upper left", fontsize=9)
        ax.set_title(title, fontsize=14, pad=20)
        ax.axis("off")
        fig.tight_layout()

        if output_path is None:
            from src.config_loader import config
            output_dir = Path(config.get("visualization", "output_dir", default="output/charts"))
            output_dir.mkdir(parents=True, exist_ok=True)
            suffix = f"_{project_name}" if project_name else ""
            output_path = str(output_dir / f"knowledge_graph{suffix}.png")

        fig.savefig(output_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        log.info("知识图谱已导出: %s", output_path)
        return output_path

    def _get_project_subgraph_nodes(self, project_name: str) -> set:
        """获取与指定项目相关的所有节点。"""
        proj_id = f"project:{project_name}"
        if not self.G.has_node(proj_id):
            return set()

        nodes = {proj_id}
        frontier = {proj_id}
        for _ in range(3):
            next_frontier = set()
            for n in frontier:
                for _, target, _ in self.G.out_edges(n):
                    if target not in nodes:
                        nodes.add(target)
                        next_frontier.add(target)
                for source, _, _ in self.G.in_edges(n):
                    if source not in nodes:
                        nodes.add(source)
                        next_frontier.add(source)
            frontier = next_frontier
        return nodes

    # ================================================================
    # 导出
    # ================================================================

    def export_json(self, output_path: str = None) -> str:
        """将图谱导出为 JSON（D3.js 兼容格式）。"""
        data = {
            "nodes": [],
            "edges": [],
        }

        for node_id, attrs in self.G.nodes(data=True):
            node = {
                "id": node_id,
                "label": attrs.get("label", node_id),
                "type": attrs.get("type", "unknown"),
            }
            if attrs.get("type") == "meter":
                node["meter_type"] = attrs.get("meter_type", "未知")
                node["multiplier"] = attrs.get("multiplier", 1.0)
                node["discount"] = attrs.get("discount", 1.0)
                node["is_locked"] = attrs.get("is_locked", False)
            data["nodes"].append(node)

        for source, target, attrs in self.G.edges(data=True):
            edge = {"source": source, "target": target}
            edge["relation"] = attrs.get("relation", "")
            # 包含边属性数据
            for k, v in attrs.items():
                if k != "relation" and v is not None:
                    edge[k] = v
            data["edges"].append(edge)

        if output_path is None:
            output_dir = Path("output/data")
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = str(output_dir / "knowledge_graph.json")

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        log.info("知识图谱 JSON 导出: %s (%d 节点, %d 边)",
                 output_path, len(data["nodes"]), len(data["edges"]))
        return output_path
