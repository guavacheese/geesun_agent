#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run PLC audit with all available files"""
import sys
import os
import importlib.util

# Add the scripts directory to path
script_dir = os.path.join(os.path.dirname(__file__), 'skills', 'plc-code-auditor', 'scripts')
sys.path.insert(0, script_dir)

# Import the audit module
spec = importlib.util.spec_from_file_location("plc_audit", os.path.join(script_dir, "plc_audit.py"))
plc_audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plc_audit)

# Run with all PLC XML files and both Excel files
plc_files = [
    '78b44bcc-81d9-470c-a2df-b7e479d1351f.xml',
    'ac335d65-5243-4fe7-a2d3-c38faa56045d.xml'
]
excel_files = [
    '1111-IO表-012.xlsx',
    'IO数据库V1.0.03.xlsx'
]

# Call main with arguments
sys.argv = ['plc_audit.py'] + plc_files + excel_files + ['-o', '.']
plc_audit.main()
