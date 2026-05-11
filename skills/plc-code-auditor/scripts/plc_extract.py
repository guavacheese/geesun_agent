#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PLC变量提取工具
功能：仅提取PLC XML中的变量列表，不进行Excel比对

使用方法：
    python plc_extract.py <plc_xml_file> [output_dir]

参数：
    plc_xml_file: PLC源码XML文件路径
    output_dir: 输出目录（可选，默认为当前目录）
"""

import xml.etree.ElementTree as ET
import re
import sys
import os
from datetime import datetime
from collections import defaultdict


def parse_plc_variables(plc_file):
    """
    解析PLC XML文件，提取所有变量名（支持数组下标）
    """
    tree = ET.parse(plc_file)
    root = tree.getroot()
    
    variables = []
    line_num = 0
    
    for elem in root.iter():
        line_num += 1
        text = elem.text if elem.text else ""
        attrib_text = str(elem.attrib)
        
        # 支持数组下标如: SFJ_KDJC[0], SFJ_KDJC[1]
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
                'VAR_OUTPUT', 'VAR_IN_OUT', 'VAR_GLOBAL', 'CONST', 'TYPE', 'STRUCT',
                'PROGRAM', 'FUNCTION', 'FUNCTION_BLOCK', 'END_PROGRAM', 'END_FUNCTION'}
    
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
    elif any(x in var_upper for x in ['SPEED', 'VEL']):
        return 'REAL'
    elif any(x in var_upper for x in ['POS', 'POSITION']):
        return 'REAL'
    else:
        return 'UNKNOWN'


def analyze_variables(variables):
    """
    分析变量，按类型和模块分组
    """
    # 去重
    unique_vars = {}
    for var in variables:
        name = var['name']
        if name not in unique_vars:
            unique_vars[name] = var
    
    # 按类型分组
    by_type = defaultdict(list)
    for var in unique_vars.values():
        by_type[var['type']].append(var)
    
    # 按模块前缀分组
    by_module = defaultdict(list)
    for var in unique_vars.values():
        base_name = re.sub(r'\[.*\]', '', var['name'])
        prefix = base_name.split('_')[0] if '_' in base_name else 'NO_PREFIX'
        by_module[prefix].append(var)
    
    # 检测数组变量
    array_vars = {}
    for var in unique_vars.values():
        match = re.match(r'(.+)\[(\d+)\]', var['name'])
        if match:
            base_name = match.group(1)
            index = int(match.group(2))
            if base_name not in array_vars:
                array_vars[base_name] = {'indices': [], 'type': var['type']}
            array_vars[base_name]['indices'].append(index)
    
    return unique_vars, by_type, by_module, array_vars


def generate_report(unique_vars, by_type, by_module, array_vars, output_dir, plc_file):
    """
    生成变量分析报告
    """
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    base_name = os.path.splitext(os.path.basename(plc_file))[0]
    report_file = os.path.join(output_dir, f'{base_name}_variables_{timestamp}.md')
    
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write('# PLC变量提取报告\n\n')
        f.write(f'生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')
        f.write(f'源文件: {plc_file}\n\n')
        
        # 统计摘要
        f.write('## 统计摘要\n\n')
        f.write(f'- 变量总数: {len(unique_vars)}\n')
        f.write(f'- 类型种类: {len(by_type)}\n')
        f.write(f'- 模块前缀数: {len(by_module)}\n')
        f.write(f'- 数组变量数: {len(array_vars)}\n\n')
        
        # 按类型统计
        f.write('## 按类型分布\n\n')
        f.write('| 类型 | 数量 |\n')
        f.write('|------|------|\n')
        for type_name in sorted(by_type.keys()):
            f.write(f'| {type_name} | {len(by_type[type_name])} |\n')
        f.write('\n')
        
        # 按模块前缀统计
        f.write('## 按模块前缀分布\n\n')
        f.write('| 前缀 | 数量 | 示例 |\n')
        f.write('|------|------|------|\n')
        for prefix in sorted(by_module.keys()):
            examples = [v['name'] for v in by_module[prefix][:3]]
            f.write(f'| {prefix} | {len(by_module[prefix])} | {", ".join(examples)} |\n')
        f.write('\n')
        
        # 数组变量详情
        if array_vars:
            f.write('## 数组变量详情\n\n')
            f.write('| 数组名 | 类型 | 下标范围 | 元素数 |\n')
            f.write('|--------|------|----------|--------|\n')
            for name in sorted(array_vars.keys()):
                info = array_vars[name]
                indices = sorted(info['indices'])
                if indices:
                    range_str = f"{indices[0]}..{indices[-1]}"
                    f.write(f'| {name} | {info["type"]} | {range_str} | {len(indices)} |\n')
            f.write('\n')
        
        # 完整变量列表
        f.write('## 完整变量列表\n\n')
        f.write('| 序号 | 变量名 | 类型 | 位置 |\n')
        f.write('|------|--------|------|------|\n')
        for idx, (name, var) in enumerate(sorted(unique_vars.items()), 1):
            f.write(f'| {idx} | `{name}` | {var["type"]} | 第{var["line"]}行 |\n')
        f.write('\n')
        
        # 变量名CSV格式（方便复制到Excel）
        f.write('## CSV格式（便于复制）\n\n')
        f.write('```csv\n')
        f.write('变量名,类型\n')
        for name, var in sorted(unique_vars.items()):
            f.write(f'{name},{var["type"]}\n')
        f.write('```\n\n')
    
    return report_file


def generate_clean_source(plc_file, output_dir):
    """
    生成清理后的源码
    """
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    base_name = os.path.splitext(os.path.basename(plc_file))[0]
    output_file = os.path.join(output_dir, f'{base_name}_cleaned_{timestamp}.txt')
    
    with open(plc_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    cleaned_lines = []
    for line in lines:
        # 去掉行尾注释
        if '//' in line:
            line = line.split('//')[0]
        if '#' in line:
            line = line.split('#')[0]
        
        # 去掉首尾空白
        line = line.strip()
        
        # 跳过空行
        if line:
            cleaned_lines.append(line)
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(cleaned_lines))
    
    return output_file


def main():
    if len(sys.argv) < 2:
        print("用法: python plc_extract.py <plc_xml_file> [output_dir]")
        print("示例: python plc_extract.py code.xml ./output")
        sys.exit(1)
    
    plc_file = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else '.'
    
    if not os.path.exists(plc_file):
        print(f"错误: PLC文件不存在: {plc_file}")
        sys.exit(1)
    
    os.makedirs(output_dir, exist_ok=True)
    
    print("=" * 60)
    print("PLC变量提取工具")
    print("=" * 60)
    
    print(f"\n[1/3] 正在解析PLC文件: {plc_file}")
    variables = parse_plc_variables(plc_file)
    print(f"      找到 {len(variables)} 个变量引用")
    
    print(f"\n[2/3] 正在分析变量...")
    unique_vars, by_type, by_module, array_vars = analyze_variables(variables)
    print(f"      去重后变量: {len(unique_vars)}")
    print(f"      类型种类: {len(by_type)}")
    print(f"      数组变量: {len(array_vars)}")
    
    print(f"\n[3/3] 正在生成报告...")
    report_file = generate_report(unique_vars, by_type, by_module, array_vars, output_dir, plc_file)
    print(f"      报告已保存: {report_file}")
    
    cleaned_file = generate_clean_source(plc_file, output_dir)
    print(f"      清理后源码: {cleaned_file}")
    
    print("\n" + "=" * 60)
    print("提取完成!")
    print("=" * 60)
    print(f"\n输出文件:")
    print(f"  1. {report_file}")
    print(f"  2. {cleaned_file}")


if __name__ == '__main__':
    main()
