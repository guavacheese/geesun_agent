# 更新说明：PLC代码审查工具

## 修改内容概要

根据最新需求，对 plc-code-auditor 技能进行了更新，主要修改内容如下：

### 1. Sheet处理范围限制
- **修改前**：工具会处理Excel文件中的所有Sheet
- **修改后**：工具只处理指定的四个Sheet类型：
  - 气缸Sheet
  - 真空吹气Sheet  
  - 数字量Sheet
  - 模拟量Sheet

### 2. Sheet识别规则更新
- 新增针对不同类型Sheet的列识别规则
- 气缸Sheet：A-M列的特定列识别规则
- 真空吹气Sheet：A-E列的特定列识别规则  
- 数字量Sheet：A-C列的特定列识别规则
- 模拟量Sheet：A-E列的特定列识别规则

### 3. 处理逻辑优化
- 只处理名称匹配特定关键词的Sheet（气缸、真空、数字量、模拟量等）
- 其他Sheet将被自动跳过，不再参与处理
- 审核标准和变量匹配检查也限定在这四类变量范围内

### 4. 输出报告优化
- 报告中只会包含这四类变量的分析结果
- Sheet IO分析详情文件也会相应地只包含这四类Sheet的分析结果

## 使用说明

### 文件格式要求
- PLC源码文件：Sysmac Studio导出的JSON Lines格式（带UTF-8 BOM）
- Excel文件：.xlsx格式，必须包含气缸、真空吹气、数字量、模拟量这四个类型的Sheet

### 命令使用示例
```bash
# 基本用法
python scripts/plc_audit.py code.xml io_database.xlsx

# 多个Excel文件
python scripts/plc_audit.py code.xml io_table.xlsx io_database.xlsx

# 指定输出目录
python scripts/plc_audit.py code.xml io_table.xlsx io_db.xlsx -o ./reports
```

### 处理的Sheet类型
1. **气缸Sheet**：包含气缸相关的IO地址和变量
2. **真空吹气Sheet**：包含真空吹气相关的IO地址和变量  
3. **数字量Sheet**：包含数字量IO地址和变量
4. **模拟量Sheet**：包含模拟量IO地址和变量

## 注意事项

1. Excel文件必须包含上述四类Sheet，否则工具将无法正常处理
2. Sheet名称应该包含相应的关键词以便工具识别
3. 只有这四类变量会被纳入比对和分析范围
4. 其他类型的Sheet将被自动跳过，不会影响处理结果