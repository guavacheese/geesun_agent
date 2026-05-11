#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PLC代码审查工具 - 完整版
功能：解析Excel中所有Sheet，进行完整的IO数据库审查

使用方法：
    python plc_audit_full.py <plc_xml_file> <excel_file> [output_dir]
"""

import xml.etree.ElementTree as ET
import pandas as pd
import re
import sys
import os
from datetime import datetime
from collections import defaultdict


def parse_plc_variables(plc_file):
    """解析PLC XML文件，提取变量名"""
    tree = ET.parse(plc_file)
    root = tree.getroot()
    
    variables = []
    line_num = 0
    
    for elem in root.iter():
        line_num += 1
        text = elem.text if elem.text else ""
        var_pattern = r'\b([A-Za-z_][A-Za-z0-9_]*(?:\[[0-9]+\])?)\b'
        
        if text:
            matches = re.findall(var_pattern, text)
            for match in matches:
                if is_valid_variable(match):
                    variables.append({
                        'name': match,
                        'type': guess_type(match),
                        'line': line_num,
                        'context': text[:100]
                    })
        
        for key, value in elem.attrib.items():
            matches = re.findall(var_pattern, str(value))
            for match in matches:
                if is_valid_variable(match):
                    variables.append({
                        'name': match,
                        'type': guess_type(match),
                        'line': line_num,
                        'context': f"{key}={value}"[:100]
                    })
    
    return variables


def is_valid_variable(name):
    """判断是否为有效的PLC变量名"""
    keywords = {'IF', 'THEN', 'ELSE', 'END', 'AND', 'OR', 'NOT', 'TRUE', 'FALSE',
                'INT', 'BOOL', 'REAL', 'STRING', 'ARRAY', 'OF', 'VAR', 'VAR_INPUT',
                'VAR_OUTPUT', 'VAR_IN_OUT', 'VAR_GLOBAL', 'CONST', 'TYPE', 'STRUCT'}
    
    if name.upper() in keywords:
        return False
    if re.match(r'^[0-9]+$', name):
        return False
    if len(name) <= 1:
        return False
    return True


def guess_type(var_name):
    """根据变量名猜测类型"""
    var_upper = var_name.upper()
    if var_upper.startswith('I'):
        return 'INPUT'
    elif var_upper.startswith('Q') or var_upper.startswith('O'):
        return 'OUTPUT'
    elif var_upper.startswith('M'):
        return 'MEMORY'
    elif 'FLAG' in var_upper:
        return 'BOOL'
    else:
        return 'UNKNOWN'


def parse_excel_all_sheets(excel_files):
    """
    读取一个或多个Excel文件中的所有Sheet，解析IO数据库
    返回: dict {变量名: {io地址, 注释, sheet名, 文件名}}, sheet_stats
    """
    io_db = {}
    sheet_stats = {}
    
    # 确保是列表
    if isinstance(excel_files, str):
        excel_files = [excel_files]
    
    for excel_file in excel_files:
        if not os.path.exists(excel_file):
            print(f"警告: Excel文件不存在: {excel_file}")
            continue
            
        try:
            print(f"正在读取文件: {excel_file}")
            
            xl = pd.ExcelFile(excel_file)
            sheet_names = xl.sheet_names
            
            print(f"  发现 {len(sheet_names)} 个Sheet: {sheet_names}")
            
            for sheet_name in sheet_names:
                print(f"  正在读取Sheet: {sheet_name}")
                
                try:
                    df = pd.read_excel(excel_file, sheet_name=sheet_name)
                    
                    # 尝试找到变量名列和IO地址列
                    var_col = None
                    io_col = None
                    comment_col = None
                    
                    for col in df.columns:
                        col_str = str(col).upper()
                        if any(kw in col_str for kw in ['变量', 'VAR', '名称', 'NAME']):
                            var_col = col
                        elif any(kw in col_str for kw in ['IO', '地址', 'ADDR', '点']):
                            io_col = col
                        elif any(kw in col_str for kw in ['注释', 'COMMENT', '说明', 'DESC']):
                            comment_col = col
                    
                    if var_col is None and len(df.columns) > 0:
                        var_col = df.columns[0]
                    if io_col is None and len(df.columns) > 1:
                        io_col = df.columns[1]
                    if comment_col is None and len(df.columns) > 2:
                        comment_col = df.columns[2]
                    
                    sheet_count = 0
                    for idx, row in df.iterrows():
                        if var_col and pd.notna(row[var_col]):
                            var_name = str(row[var_col]).strip()
                            io_addr = str(row[io_col]).strip() if io_col and pd.notna(row[io_col]) else ""
                            comment = str(row[comment_col]).strip() if comment_col and pd.notna(row[comment_col]) else ""
                            
                            if var_name and var_name != 'nan':
                                # 如果变量已存在，跳过（保留先出现的）
                                if var_name not in io_db:
                                    io_db[var_name] = {
                                        'io_address': io_addr,
                                        'comment': comment,
                                        'sheet': sheet_name,
                                        'file': os.path.basename(excel_file)
                                    }
                                    sheet_count += 1
                    
                    key = f"{os.path.basename(excel_file)}::{sheet_name}"
                    sheet_stats[key] = sheet_count
                    
                except Exception as e:
                    print(f"  读取Sheet {sheet_name} 时出错: {e}")
                    key = f"{os.path.basename(excel_file)}::{sheet_name}"
                    sheet_stats[key] = 0
        
        except Exception as e:
            print(f"读取Excel文件 {excel_file} 时出错: {e}")
    
    return io_db, sheet_stats


def check_naming_conventions(var_name):
    """检查命名规范"""
    issues = []
    prefixes = ['JR_', 'XX_', 'QJ_', 'QD_', 'TJ_', 'MQ_', 'CJ_', 'ZJ_']
    base_name = re.sub(r'\[.*\]', '', var_name)
    
    has_prefix = any(base_name.startswith(prefix) for prefix in prefixes)
    
    if not has_prefix:
        suggested_prefix = infer_prefix(var_name)
        if suggested_prefix:
            issues.append({
                'problem': '缺少模块前缀',
                'suggestion': f'建议改为 {suggested_prefix}{base_name}'
            })
        else:
            issues.append({
                'problem': '缺少模块前缀',
                'suggestion': f'建议添加模块前缀，如 JR_{base_name}'
            })
    
    return issues


def infer_prefix(var_name):
    """根据变量名推断模块前缀"""
    var_upper = var_name.upper()
    
    if any(kw in var_upper for kw in ['DUST', 'ROLL', 'JOIN', 'JR']):
        return 'JR_'
    elif any(kw in var_upper for kw in ['CUT', 'DISCHARGE', 'XX']):
        return 'XX_'
    elif any(kw in var_upper for kw in ['AIR', 'PRESSURE', 'CHECK', 'QJ']):
        return 'QJ_'
    elif any(kw in var_upper for kw in ['CUT', 'SEVER', 'QD']):
        return 'QD_'
    elif any(kw in var_upper for kw in ['TAPE', 'GLUE', 'TJ']):
        return 'TJ_'
    
    return None


def generate_report(plc_vars, io_db, sheet_stats, output_dir):
    """生成带时间戳的审查报告"""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    report_file = os.path.join(output_dir, f'plc_audit_full_report_{timestamp}.md')
    
    total_vars = len(plc_vars)
    matched_vars = 0
    unmatched_vars = []
    naming_issues = []
    
    for var in plc_vars:
        var_name = var['name']
        
        if var_name in io_db:
            matched_vars += 1
        else:
            unmatched_vars.append(var)
        
        issues = check_naming_conventions(var_name)
        if issues:
            for issue in issues:
                naming_issues.append({
                    'var_name': var_name,
                    'type': var['type'],
                    'problem': issue['problem'],
                    'suggestion': issue['suggestion']
                })
    
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write('# PLC代码审查报告（完整版）\n\n')
        f.write(f'生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n\n')
        
        # 统计摘要
        f.write('## 统计摘要\n\n')
        f.write(f'- PLC变量总数: {total_vars}\n')
        f.write(f'- Excel匹配成功: {matched_vars}\n')
        f.write(f'- Excel匹配失败: {len(unmatched_vars)}\n')
        f.write(f'- 命名规范问题: {len(naming_issues)}\n')
        f.write(f'- 覆盖Sheet数: {len(sheet_stats)}\n\n')
        
        # Sheet统计（按文件分组）
        f.write('## Sheet统计\n\n')
        f.write('| 文件 | Sheet名 | 变量数 |\n')
        f.write('|------|---------|--------|\n')
        
        # 按文件分组显示
        file_sheets = {}
        for key, count in sheet_stats.items():
            if '::' in key:
                file_name, sheet_name = key.split('::', 1)
                if file_name not in file_sheets:
                    file_sheets[file_name] = []
                file_sheets[file_name].append((sheet_name, count))
            else:
                if '未知文件' not in file_sheets:
                    file_sheets['未知文件'] = []
                file_sheets['未知文件'].append((key, count))
        
        for file_name in sorted(file_sheets.keys()):
            sheets = file_sheets[file_name]
            for i, (sheet_name, count) in enumerate(sheets):
                if i == 0:
                    f.write(f'| {file_name} | {sheet_name} | {count} |\n')
                else:
                    f.write(f'| | {sheet_name} | {count} |\n')
        f.write('\n')
        
        # 未匹配的变量
        if unmatched_vars:
            f.write('## Excel中未找到的变量\n\n')
            f.write('| 序号 | 变量名 | 类型 | 位置 |\n')
            f.write('|------|--------|------|------|\n')
            for idx, var in enumerate(unmatched_vars, 1):
                f.write(f'| {idx} | `{var["name"]}` | {var["type"]} | 第{var["line"]}行 |\n')
            f.write('\n')
        
        # 命名规范问题
        if naming_issues:
            f.write('## 命名规范问题\n\n')
            f.write('| 变量名 | 类型 | 问题 | 建议 |\n')
            f.write('|--------|------|------|------|\n')
            for issue in naming_issues:
                f.write(f'| `{issue["var_name"]}` | {issue["type"]} | {issue["problem"]} | {issue["suggestion"]} |\n')
            f.write('\n')
        
        # 匹配成功的变量（按文件和Sheet分组）
        if matched_vars > 0:
            f.write('## 匹配成功的变量（按文件分组）\n\n')
            
            # 按文件和Sheet分组
            vars_by_location = defaultdict(list)
            for var in plc_vars:
                var_name = var['name']
                if var_name in io_db:
                    file_name = io_db[var_name].get('file', '未知文件')
                    sheet_name = io_db[var_name].get('sheet', '未知Sheet')
                    key = (file_name, sheet_name)
                    vars_by_location[key].append({
                        'var': var,
                        'io_info': io_db[var_name]
                    })
            
            # 按文件分组显示
            current_file = None
            for (file_name, sheet_name) in sorted(vars_by_location.keys()):
                if file_name != current_file:
                    f.write(f'### 文件: {file_name}\n\n')
                    current_file = file_name
                
                f.write(f'#### Sheet: {sheet_name}\n\n')
                f.write('| 变量名 | 类型 | IO地址 | 注释 |\n')
                f.write('|--------|------|--------|------|\n')
                for item in vars_by_location[(file_name, sheet_name)]:
                    var = item['var']
                    io_info = item['io_info']
                    f.write(f'| `{var["name"]}` | {var["type"]} | {io_info["io_address"]} | {io_info["comment"]} |\n')
                f.write('\n')
    
    return report_file, timestamp


def clean_plc_source(plc_file, output_dir, timestamp):
    """清理PLC源码"""
    output_file = os.path.join(output_dir, f'plc_source_cleaned_{timestamp}.txt')
    
    with open(plc_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    cleaned_lines = []
    for line in lines:
        if '//' in line:
            line = line.split('//')[0]
        if '#' in line:
            line = line.split('#')[0]
        line = line.strip()
        if line:
            cleaned_lines.append(line)
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(cleaned_lines))
    
    return output_file


def main():
    # 解析参数
    if len(sys.argv) < 3:
        print("用法: python plc_audit_full.py <plc_xml_file> <excel_file1> [excel_file2 ...] [-o output_dir]")
        print("示例:")
        print("  python plc_audit_full.py code.xml io_table.xlsx")
        print("  python plc_audit_full.py code.xml io_table.xlsx io_database.xlsx -o ./reports")
        sys.exit(1)
    
    plc_file = sys.argv[1]
    
    # 收集excel文件和输出目录
    excel_files = []
    output_dir = '.'
    i = 2
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg in ['-o', '--output']:
            if i + 1 < len(sys.argv):
                output_dir = sys.argv[i + 1]
                i += 2
            else:
                print("错误: -o 参数需要指定输出目录")
                sys.exit(1)
        elif arg.endswith('.xlsx') or arg.endswith('.xls'):
            excel_files.append(arg)
            i += 1
        else:
            # 假设是输出目录（旧版兼容）
            if i == len(sys.argv) - 1 and not arg.endswith('.xml'):
                output_dir = arg
            i += 1
    
    if not excel_files:
        print("错误: 至少需要指定一个Excel文件")
        sys.exit(1)
    
    if not os.path.exists(plc_file):
        print(f"错误: PLC文件不存在: {plc_file}")
        sys.exit(1)
    
    for excel_file in excel_files:
        if not os.path.exists(excel_file):
            print(f"错误: Excel文件不存在: {excel_file}")
            sys.exit(1)
    
    os.makedirs(output_dir, exist_ok=True)
    
    print("=" * 60)
    print("PLC代码审查工具 - 完整版")
    print("=" * 60)
    
    print(f"\n[1/4] 正在解析PLC文件: {plc_file}")
    plc_vars = parse_plc_variables(plc_file)
    print(f"      找到 {len(plc_vars)} 个变量")
    
    print(f"\n[2/4] 正在读取Excel文件（共{len(excel_files)}个，所有Sheet）")
    for f in excel_files:
        print(f"      - {f}")
    io_db, sheet_stats = parse_excel_all_sheets(excel_files)
    print(f"      IO数据库条目总数: {len(io_db)}")
    
    print(f"\n[3/4] 正在生成审查报告...")
    report_file, timestamp = generate_report(plc_vars, io_db, sheet_stats, output_dir)
    print(f"      报告已保存: {report_file}")
    
    print(f"\n[4/4] 正在清理PLC源码...")
    cleaned_file = clean_plc_source(plc_file, output_dir, timestamp)
    print(f"      清理后源码已保存: {cleaned_file}")
    
    print("\n" + "=" * 60)
    print("审查完成!")
    print("=" * 60)
    print(f"\n输出文件:")
    print(f"  1. {report_file}")
    print(f"  2. {cleaned_file}")


if __name__ == '__main__':
    main()
