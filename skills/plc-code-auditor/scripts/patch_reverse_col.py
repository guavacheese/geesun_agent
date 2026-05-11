#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Patch plc_audit.py to skip reverse cylinder checks when no '反气缸' column exists."""

import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(SCRIPT_DIR, 'plc_audit.py')

with open(TARGET, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# === 1. Line ~457: Add has_reverse_cylinder_col tracking ===
# Find: reverse_col = None  # 反气缸标记列
for i, line in enumerate(lines):
    if 'reverse_col = None' in line and '反气缸' in line:
        # Add after: has_reverse_cylinder_col = False
        indent = line[:len(line) - len(line.lstrip())]
        lines.insert(i + 1, indent + 'has_reverse_cylinder_col = False  # 标记是否有反气缸列\n')
        print(f"[1] Added has_reverse_cylinder_col tracking at line {i+2}")
        break

# === 2. Line ~466: Set has_reverse_cylinder_col when reverse_col found by name ===
for i, line in enumerate(lines):
    if 'kw in col_str for kw in' in line and '反气缸' in line:
        # Find the next line that sets reverse_col
        for j in range(i, min(i+3, len(lines))):
            if 'reverse_col = col' in lines[j] and 'reverse_col is None' not in lines[j]:
                indent2 = lines[j][:len(lines[j]) - len(lines[j].lstrip())]
                lines.insert(j + 1, indent2 + '                                has_reverse_cylinder_col = True  # 找到反气缸列\n')
                print(f"[2] Added has_reverse_cylinder_col = True at line {j+2}")
                break
        break

# === 3. Line ~626: Set has_reverse_cylinder_col when reverse_col found by content ===
for i, line in enumerate(lines):
    if 'reverse_col = col' in line and '找到可能的反气缸列' in line:
        indent3 = line[:len(line) - len(line.lstrip())]
        lines.insert(i, indent3 + '                                has_reverse_cylinder_col = True  # 通过内容推断找到反气缸列\n')
        print(f"[3] Added has_reverse_cylinder_col = True at line {i+1}")
        break

# === 4. Line ~1892: Modify check_reverse_cylinder_not signature ===
for i, line in enumerate(lines):
    if line.strip() == 'def check_reverse_cylinder_not(plc_vars, io_by_address):':
        lines[i] = line.replace(
            'def check_reverse_cylinder_not(plc_vars, io_by_address):',
            'def check_reverse_cylinder_not(plc_vars, io_by_address, has_reverse_cylinder_col=False):'
        )
        print(f"[4] Updated check_reverse_cylinder_not signature at line {i+1}")
        break

# === 5. Line ~1941: Modify check_normal_cylinder_not signature ===
for i, line in enumerate(lines):
    if line.strip() == 'def check_normal_cylinder_not(plc_vars, io_by_address):':
        lines[i] = line.replace(
            'def check_normal_cylinder_not(plc_vars, io_by_address):',
            'def check_normal_cylinder_not(plc_vars, io_by_address, has_reverse_cylinder_col=False):'
        )
        print(f"[5] Updated check_normal_cylinder_not signature at line {i+1}")
        break

# === 6. Add early return in check_reverse_cylinder_not ===
for i, line in enumerate(lines):
    if i > 0 and '检查反气缸是否' in line and '误加' in line:
        # Find the line after docstring ends (after """ and issues = [])
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip() == 'issues = []':
                indent = lines[j][:len(lines[j]) - len(lines[j].lstrip())]
                lines.insert(j+1, indent + '    # 如果没有反气缸列，跳过检查\n')
                lines.insert(j+2, indent + '    if not has_reverse_cylinder_col:\n')
                lines.insert(j+3, indent + '        return []\n')
                print(f"[6] Added early return in check_reverse_cylinder_not at line {j+2}")
                break
        break

# === 7. Add early return in check_normal_cylinder_not ===
for i, line in enumerate(lines):
    if i > 0 and '检查非反气缸' in line and '缺少NOT' in line:
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip() == 'issues = []':
                indent = lines[j][:len(lines[j]) - len(lines[j].lstrip())]
                lines.insert(j+1, indent + '    # 如果没有反气缸列，跳过检查\n')
                lines.insert(j+2, indent + '    if not has_reverse_cylinder_col:\n')
                lines.insert(j+3, indent + '        return []\n')
                print(f"[7] Added early return in check_normal_cylinder_not at line {j+2}")
                break
        break

# === 8. Update call sites at ~2359-2360 ===
for i, line in enumerate(lines):
    if 'check_reverse_cylinder_not(plc_vars, io_by_address)' in line:
        lines[i] = line.replace(
            'check_reverse_cylinder_not(plc_vars, io_by_address)',
            'check_reverse_cylinder_not(plc_vars, io_by_address, has_reverse_cylinder_col)'
        )
        print(f"[8a] Updated call site for check_reverse_cylinder_not at line {i+1}")
    if 'check_normal_cylinder_not(plc_vars, io_by_address)' in line and 'check_reverse' not in line:
        lines[i] = line.replace(
            'check_normal_cylinder_not(plc_vars, io_by_address)',
            'check_normal_cylinder_not(plc_vars, io_by_address, has_reverse_cylinder_col)'
        )
        print(f"[8b] Updated call site for check_normal_cylinder_not at line {i+1}")

# === 9. Update report output: wrap reverse/normal cylinder sections ===
for i, line in enumerate(lines):
    if 'if reverse_cylinder_missing_not:' in line and 'total_cylinder_issues' not in line:
        # This is the summary section (~2460)
        lines[i] = line.replace(
            'if reverse_cylinder_missing_not:',
            'if has_reverse_cylinder_col and reverse_cylinder_missing_not:'
        )
        print(f"[9a] Updated summary output for reverse_cylinder at line {i+1}")
    if 'if normal_cylinder_wrong_not:' in line and 'total_cylinder_issues' not in line:
        # This is the summary section (~2461)
        lines[i] = line.replace(
            'if normal_cylinder_wrong_not:',
            'if has_reverse_cylinder_col and normal_cylinder_wrong_not:'
        )
        print(f"[9b] Updated summary output for normal_cylinder at line {i+1}")
    if '### [错误] 反气缸误加NOT' in line:
        # This is the detailed output header (~2478)
        lines[i] = line.replace(
            'if reverse_cylinder_missing_not:',
            'if has_reverse_cylinder_col and reverse_cylinder_missing_not:'
        )
        print(f"[9c] Updated detailed output for reverse_cylinder at line {i+1}")
    if '### [注意] 非反气缸缺少NOT' in line:
        # This is the detailed output header (~2488)
        lines[i] = line.replace(
            'if normal_cylinder_wrong_not:',
            'if has_reverse_cylinder_col and normal_cylinder_wrong_not:'
        )
        print(f"[9d] Updated detailed output for normal_cylinder at line {i+1}")

# Save
with open(TARGET, 'w', encoding='utf-8') as f:
    f.writelines(lines)

print(f"\nDone. {len(lines)} lines written.")
