#!/bin/bash
cd /mnt/d/workspace/geesun_agent
python skills/plc-code-auditor/scripts/plc_audit.py "78b44bcc-81d9-470c-a2df-b7e479d1351f.xml" "ac335d65-5243-4fe7-a2d3-c38faa56045d.xml" "1111-IO表-012.xlsx" "IO数据库V1.0.03.xlsx" -o .
