# PLC变量命名规范

本文档定义PLC变量的命名规范和模块前缀标准。

## 命名规范总则

### 基本格式

```
[模块前缀]_[功能描述][序号]
```

**示例**:
- `JR_DustBoxCheck` - 入卷模块_除尘盒检查
- `XX_Cylinder1` - 下料模块_气缸1
- `QJ_AirPressure` - 气检模块_气压检测

### 命名要求

1. **必须包含模块前缀** - 标识变量所属的功能模块
2. **使用英文描述** - 功能描述使用英文单词或缩写
3. **驼峰命名** - 多个单词使用首字母大写连接
4. **数组下标** - 数组变量使用 `[n]` 格式，如 `SFJ_KDJC[0]`

## 模块前缀对照表

### 标准前缀

| 前缀 | 模块名称 | 英文 | 适用范围 | 示例 |
|------|---------|------|---------|------|
| JR_ | 入卷 | Join Roll | 入卷相关设备 | JR_DustBoxCheck |
| XX_ | 下料 | 下料 | 下料相关设备 | XX_Cylinder1 |
| QJ_ | 气检 | 气检 | 气密性检测 | QJ_PressureCheck |
| QD_ | 切断 | 切断 | 切断机构 | QD_CutterPos |
| TJ_ | 贴胶 | 贴胶 | 贴胶机构 | TJ_TapeLength |
| MQ_ | 模切 | 模切 | 模切机构 | MQ_DieCutPos |
| CJ_ | 出卷 | 出卷 | 出卷机构 | CJ_RollOut |
| ZJ_ | 张力 | 张力 | 张力控制 | ZJ_Tension |

### 特殊前缀

| 前缀 | 用途 | 示例 |
|------|------|------|
| SFJ_ | 上卷辅助 | SFJ_KDJC[0] |
| XF_ | 下辅助 | XF_Cylinder |
| ZH_ | 综合 | ZH_Status |
| GY_ | 供液 | GY_Pump |
| HQ_ | 供气 | HQ_Valve |

## 功能描述词汇表

### 通用词汇

| 英文 | 中文 | 用途 |
|------|------|------|
| Check | 检查 | 检测状态 |
| Cylinder | 气缸 | 气缸控制 |
| Motor | 电机 | 电机控制 |
| Sensor | 传感器 | 传感器信号 |
| Valve | 阀门 | 阀门控制 |
| Pump | 泵 | 泵控制 |
| Position | 位置 | 位置相关 |
| Speed | 速度 | 速度相关 |
| Pressure | 压力 | 压力相关 |
| Tension | 张力 | 张力相关 |
| Length | 长度 | 长度相关 |
| Width | 宽度 | 宽度相关 |
| Height | 高度 | 高度相关 |

### 状态词汇

| 英文 | 中文 | 示例 |
|------|------|------|
| Ready | 就绪 | JR_Ready |
| Running | 运行中 | JR_Running |
| Error | 错误 | JR_Error |
| Alarm | 报警 | JR_Alarm |
| Complete | 完成 | XX_Complete |
| Busy | 忙 | QJ_Busy |
| Done | 完成 | QD_Done |

### 动作词汇

| 英文 | 中文 | 示例 |
|------|------|------|
| Start | 启动 | JR_Start |
| Stop | 停止 | JR_Stop |
| Reset | 复位 | JR_Reset |
| Home | 回零 | XX_Home |
| Enable | 使能 | QJ_Enable |

## 命名示例

### 输入信号

```
JR_DustBoxCheck     # 入卷_除尘盒检查
XX_MaterialSensor   # 下料_物料传感器
QJ_AirLeakDetect    # 气检_漏气检测
QD_CutterHome       # 切断_切刀回零
TJ_TapePresent      # 贴胶_胶带存在
```

### 输出信号

```
JR_DustBoxOpen      # 入卷_除尘盒打开
XX_CylinderExtend   # 下料_气缸伸出
QJ_ValveOpen        # 气检_阀门打开
QD_CutterMove       # 切断_切刀动作
TJ_TapeFeed         # 贴胶_送胶带
```

### 模拟量

```
JR_TensionValue     # 入卷_张力值
XX_SpeedActual      # 下料_实际速度
QJ_PressureActual   # 气检_实际压力
QD_PositionActual   # 切断_实际位置
TJ_LengthActual     # 贴胶_实际长度
```

### 数组变量

```
SFJ_KDJC[0]         # 上卷辅助_宽度检测[0]
SFJ_KDJC[1]         # 上卷辅助_宽度检测[1]
XX_Cylinder[0]      # 下料_气缸[0]
XX_Cylinder[1]      # 下料_气缸[1]
```

## 常见错误

### ❌ 错误示例

```
DustBoxCheck        # 缺少模块前缀
Cylinder1           # 缺少模块前缀
jr_dustboxcheck     # 小写，不符合驼峰规范
JR_Dust_Box_Check   # 使用下划线连接，应使用驼峰
JR_DustBox          # 描述不完整，缺少动作
```

### ✅ 正确示例

```
JR_DustBoxCheck     # 入卷_除尘盒检查
XX_Cylinder1Extend  # 下料_气缸1伸出
QJ_AirPressureHigh  # 气检_气压高
QD_CutterPosActual  # 切断_切刀位置实际值
TJ_TapeLengthSet    # 贴胶_胶带长度设定
```

## 命名检查清单

在命名变量前，请检查：

- [ ] 是否包含正确的模块前缀？
- [ ] 功能描述是否清晰明确？
- [ ] 是否使用驼峰命名法？
- [ ] 是否避免使用下划线连接单词？
- [ ] 是否避免使用拼音？
- [ ] 数组变量下标是否正确？

## 遗留变量处理

对于不符合规范的遗留变量：

1. **记录备案** - 在审查报告中标记为例外
2. **逐步迁移** - 新代码使用规范命名
3. **别名映射** - 必要时建立新旧名称映射表

## 更新记录

| 版本 | 日期 | 更新内容 |
|------|------|---------|
| 1.0 | 2026-03-17 | 初始版本，定义8个标准前缀 |
