# NULL 三值逻辑与结果展示提案（成员三执行端草案）

本文是 SQL 扩展阶段 2 的成员三先行交付：执行端 NULL 三值逻辑规则与结果展示
规则草案，供三人评审；新类型契约（Value 扩展、编码）由成员二牵头定稿后，
本草案的展示层实现即生效，执行端按本文真值表实现。

## 一、NULL 表示与识别

- 执行端内部用 Python `None` 表示 NULL，与 int/str/bool 严格区分：
  任何类型判断前先做 `value is None` 短路，不依赖 isinstance 链兜底。
- 常量折叠与恒真/恒假优化必须感知 NULL：`NULL = NULL` 的结果是 UNKNOWN
  而不是 TRUE，**不能折叠为恒真**；`NULL AND FALSE` 折叠为 FALSE 是安全的
  （吸收律），`NULL OR TRUE` 折叠为 TRUE 同样安全——需在优化器常量求值中
  加入 None 分支（成员一实现时同步，本文给出执行端规则供对齐）。

## 二、三值逻辑真值表（执行端）

内部用 `Optional[bool]` 表示三值：`True` / `False` / `None`(UNKNOWN)。

| 运算 | 规则 |
|---|---|
| 比较（=、!=、<>、<、<=、>、>=） | 任一操作数为 NULL → UNKNOWN（不再报 TYPE_MISMATCH） |
| NOT | NOT UNKNOWN = UNKNOWN |
| AND | FALSE AND UNKNOWN = FALSE；TRUE AND UNKNOWN = UNKNOWN |
| OR | TRUE OR UNKNOWN = TRUE；FALSE OR UNKNOWN = UNKNOWN |
| 算术（+、-） | 任一操作数为 NULL → NULL |
| WHERE | 仅 TRUE 选中；FALSE 与 UNKNOWN 都不选中 |
| CHECK 约束 | UNKNOWN 视为通过（不拒绝行） |
| NOT NULL 约束 | NULL 直接拒绝 |
| UNIQUE / 主键 | 主键不允许 NULL；UNIQUE 建议允许多个 NULL（NULL 与 NULL 视为不等） |
| DISTINCT / 集合运算 | NULL 与 NULL 视为相等（集合语义） |
| 排序 | 建议 NULL 最小：ASC 时排最前、DESC 时排最后（待与成员一确认） |
| 聚合 | COUNT(*) 计全部行；COUNT(列) 跳过 NULL；SUM/AVG/MAX/MIN 跳过 NULL，输入全 NULL 时结果为 NULL，COUNT(列) 为 0 |

执行器改动点（阶段 2 实施时）：

- `_eval_bool` 返回 `Optional[bool]`，Filter 对 None 视为不匹配（现状是把
  非 bool 一律报 TYPE_MISMATCH，需同步调整）；
- `_eval_binary` 的比较分支先短路 None；AND/OR 按上表；
- 排序键含 None 时按"NULL 最小"比较。

## 三、结果展示规则

CLI 表格与 GUI 结果表统一按以下规则渲染（GUI 沿用同一实现）：

| 值 | 显示 |
|---|---|
| NULL | `NULL` |
| BOOL | `TRUE` / `FALSE`（对齐文法关键字，不再显示 Python 的 True/False） |
| DECIMAL | 按声明精度；无声明信息时显示最短精确表示 |
| DATE / TIME / TIMESTAMP | ISO 8601（日期 `YYYY-MM-DD`，时间 `HH:MM:SS`，时间戳 `YYYY-MM-DD HH:MM:SS`） |
| INT / VARCHAR | 现状不变 |

编译跟踪（`--trace`）序列化：NULL → JSON `null`；DECIMAL → 字符串；日期时间 →
ISO 字符串——保证 JSON Lines 可序列化且不丢失类型可读性。

## 四、展示层前瞻实现（已随本草案落地）

`cli/main.py` 的渲染与 `cli/trace.py` 的序列化已提前兼容上述值域（新类型契约
生效前不改变现有行为的输出）；对应测试见 `tests/integration/test_display.py`。
执行端求值与测试替身待成员二类型契约与成员一文法定稿后实施。

## 五、待组内确认

1. NULL 的排序位置（NULL 最小 / 最大 / 可配置）。
2. UNIQUE 是否允许多个 NULL（本草案建议允许）。
3. DECIMAL 显示精度来源：结果中是否携带列声明精度（依赖成员二的结果序列化契约）。
4. BOOL 显示改为 TRUE/FALSE 属于行为变更，评审时确认无回归顾虑。
