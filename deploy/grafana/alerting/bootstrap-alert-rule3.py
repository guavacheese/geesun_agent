#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Grafana Alerting 规则 bootstrap（幂等，可重跑）

方案 A：Grafana-managed alert rule（Loki 数据源），规则存 Grafana DB，
创建时带 X-Disable-Provenance → UI 完全可编辑（阈值/条件），状态历史/触发图在
Alerting → Rules → 详情页可见（Grafana-managed 规则专属能力）。

规则：Geesun Agent ERROR 日志突增
  查询: sum(count_over_time({container=~"geesun_geesun-agent.*", level="ERROR"}[5m]))
  触发: 5m 内 ERROR > 5 条，持续 2m 确认（防抖）
  基线: 2026-09-03 实测 agent 1h 内 0 条 ERROR；阈值 5 留明显余量，可在 UI 调整
  噪声隔离: 只匹配 geesun_geesun-agent.*（全栈扫会命中 loki 自身 110条/h 的 error 级日志）
  level 大小写: agent JSON 日志 level 为大写(INFO/ERROR/WARNING)，实测确认

依赖: 本机 python3 + paramiko；67 Grafana admin 密码；.env 的 GRAFANA_PASSWORD 为有效 admin 密码

用法:
  python bootstrap-alert-rule3.py \
    --host 10.10.10.67 --ssh-pass 'Geesun2020.' --grafana-pass 'xxx'
  密码也可用环境变量 HOST67_PASS / GRAFANA_ADMIN_PASSWORD 传入，避免命令行泄露
"""
import argparse
import base64
import json
import os
import sys
import time

try:
    import paramiko
except ImportError:
    sys.exit("需要 paramiko: pip install paramiko")

RULE_TITLE = "Geesun Agent ERROR 日志突增"
FOLDER_TITLE = "geesun-alerts"

RULE = {
    "title": RULE_TITLE,
    "folderUID": None,  # 运行时按 title 查/建
    "ruleGroup": "geesun-logs",
    "noDataState": "OK",      # agent 无 ERROR 日志（查询空）→ 视为 OK 不告警
    "execErrState": "Error",  # 查询执行出错 → 规则状态 Error（有日志可查）
    "for": "2m",              # 持续 2m 确认，防单次抖动
    "labels": {"severity": "warning", "team": "geesun"},
    "annotations": {
        "summary": "geesun_agent 5 分钟内 ERROR 日志突增：{{ $values.B }} 条",
        "description": (
            'count_over_time({container=~"geesun_geesun-agent.*", level="ERROR"}[5m]) '
            "5m 内超过 5 条触发。查看日志：Grafana → Explore → 数据源 Loki 过滤 geesun_geesun-agent"
        ),
    },
    "data": [
        {
            "refId": "A",
            "datasourceUid": "geesun-loki",
            "queryType": "instant",
            "relativeTimeRange": {"from": 300, "to": 0},  # 查最近 5m（Loki alert 必需字段，漏了报 invalidRelativeTime）
            "model": {
                "expr": 'sum(count_over_time({container=~"geesun_geesun-agent.*", level="ERROR"}[5m]))',
                "queryType": "instant",
                "editorMode": "code",
                "legendFormat": "__auto",
                "refId": "A",
            },
        },
        {
            "refId": "B",
            "datasourceUid": "__expr__",
            "type": "reduce",
            "model": {"type": "reduce", "expression": "A", "reducer": "last", "refId": "B"},
        },
        {
            "refId": "C",
            "datasourceUid": "__expr__",
            "type": "threshold",
            "model": {
                "type": "threshold",
                "expression": "B",
                "refId": "C",
                "conditions": [
                    {
                        "evaluator": {"type": "gt", "params": [5]},
                        "operator": {"type": "and"},
                        "query": {"params": ["C"]},
                        "reducer": {"type": "last"},
                        "type": "query",
                    }
                ],
            },
        },
    ],
    "condition": "C",  # 顶层 condition 指向最终阈值节点（漏了报 condition must not be empty）
    "isPaused": False,
}


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST67", "10.10.10.67"))
    ap.add_argument("--ssh-user", default="root")
    ap.add_argument("--ssh-pass", default=os.environ.get("HOST67_PASS", ""))
    ap.add_argument("--grafana-pass", default=os.environ.get("GRAFANA_ADMIN_PASSWORD", ""))
    return ap.parse_args()


def main():
    a = parse_args()
    if not a.ssh_pass or not a.grafana_pass:
        sys.exit("缺密码：--ssh-pass/--grafana-pass 或环境变量 HOST67_PASS/GRAFANA_ADMIN_PASSWORD")

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(a.host, username=a.ssh_user, password=a.ssh_pass, timeout=15)

    def gapi(method, path, body=None, headers=""):
        data = ""
        if body is not None:
            b64 = base64.b64encode(json.dumps(body).encode()).decode()
            data = f" --data-binary @<(echo {b64} | base64 -d)"
        sh = (
            f'curl -s -m 30 -X {method} -u admin:{a.grafana_pass} '
            f'-H "Content-Type: application/json" {headers} '
            f"-w '\\nHTTP=%{{http_code}}\\n' {data} http://localhost:3000{path}"
        )
        b64all = base64.b64encode(sh.encode()).decode()
        _, stdout, _ = c.exec_command(
            f'G=$(docker ps -q -f name=geesun_grafana | head -1) && '
            f'docker exec $G sh -c "echo {b64all} | base64 -d | sh"', timeout=60
        )
        out = stdout.read().decode("utf-8", "replace")
        parts = out.rsplit("\nHTTP=", 1)
        return (parts[1].strip() if len(parts) > 1 else "?"), parts[0]

    # 1. folder（按 title 查/建）
    _, out = gapi("GET", "/api/folders?limit=50")
    folder_uid = None
    for f in json.loads(out or "[]"):
        if f.get("title") == FOLDER_TITLE:
            folder_uid = f["uid"]
            break
    if not folder_uid:
        code, out = gapi("POST", "/api/folders", {"title": FOLDER_TITLE})
        folder_uid = json.loads(out).get("uid")
        print(f"[folder] 创建 {FOLDER_TITLE} uid={folder_uid} HTTP={code}")
    else:
        print(f"[folder] 复用 {FOLDER_TITLE} uid={folder_uid}")

    # 2. 幂等：删同名旧规则
    _, out = gapi("GET", "/api/v1/provisioning/alert-rules")
    for r in json.loads(out or "[]"):
        if r.get("title") == RULE_TITLE:
            code, _ = gapi("DELETE", f"/api/v1/provisioning/alert-rules/{r['uid']}")
            print(f"[rule] 删除旧规则 {r['uid']} HTTP={code}")

    # 3. 创建（X-Disable-Provenance → UI 可编辑，不标记为 provisioned）
    rule = dict(RULE)
    rule["folderUID"] = folder_uid
    code, out = gapi("POST", "/api/v1/provisioning/alert-rules", rule,
                     headers='-H "X-Disable-Provenance: true"')
    if code != "201":
        sys.exit(f"创建失败 HTTP={code}: {out[:800]}")
    uid = json.loads(out).get("uid")
    print(f"[rule] 创建成功 uid={uid} title={RULE_TITLE}")
    print(f"[rule] 状态查看: 67 Grafana → Alerting → Rules（组 geesun-logs）")
    c.close()


if __name__ == "__main__":
    main()
