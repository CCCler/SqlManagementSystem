# SQL 扩展编译器报告

## 1. 交付状态

基于 `897d79e` 扩展成员一的编译器。十二类功能已实现编译路径、语义检查、计划及独立测试；**执行器、存储、对象持久化和最终端到端验收仍待接入**。本次没有迁移数据库、初始化管理员或修改实际账户，没有提交或推送。

旧语句仍使用既有可执行计划。含新语义、扩展表元数据或索引访问的语句生成 `ExtendedPlan`，现有执行器在业务写入前以 `FEATURE_NOT_EXECUTABLE` 拒绝。成功编译不等于能执行；`EXPLAIN` 可以展示扩展计划，不执行内部语句，页面显示“执行待接入”。

## 2. 编译流程与公共契约

`SQLCompiler.compile(sql, catalog)`：Lexer → Parser → SemanticAnalyzer/Binder → Planner → 保守优化；保留 `split_statements(sql)`。编译结果包含 tokens、ast、semantic、plan、optimized_plan、output_fields、dependencies、required_capabilities。原计划为不可变数据，优化构造新计划。

实现位置：`compiler/extended_parser.py`、`extended_semantic.py`、`extended_optimizer.py`、`contracts/extensions.py`。旧语法优先走旧解析器；扩展语法使用递归下降解析。已存在扩展表元数据、视图或索引时，即使文本看似旧 SQL 也重新绑定为扩展计划，避免遗漏新属性。

- `TypeSpec(kind, precision, scale, nullable)` 描述编译类型；NULL 是值类型，不能声明 NULL 表列。
- `FieldBinding(scope, source, ordinal, qualifier, name, type)` 区分查询作用域、同一表的不同来源实例及列位置。内层来源遮蔽同名外层来源，字段歧义报错。
- `OutputField` 描述输出名称/类型；集合采用首分支名称，按列统一类型。
- `ColumnSchema` 新增 precision、scale、nullable、default、has_default，`TableSchema` 新增 constraints；默认参数兼容旧构造方式。
- `ExtendedPlan` 携带 operator、children、expressions、output、attributes、capabilities、dependencies。表达式子查询也携带计划；执行器必须递归理解这些计划，不能仅识别顶层。
- 只读目录协议提供表、视图、索引、触发器、账户、数据库名及依赖查询。`ReadOnlyCatalogAdapter` 仅转发读取方法；缺少必需能力报 `CATALOG_CAPABILITY_UNAVAILABLE`，不会伪造对象存在性。缺少可选索引枚举时保留全表扫描。
- `ExtendedMemoryCatalog` 用显式夹具支持独立验收；不代表真实 Catalog 已保存扩展对象。

能力标识包括 `extended_expression`、`join`、`aggregate`、`subquery`、`set_operation`、`extended_schema`、`extended_dml`、`alter_table`、`index_ddl`、`index_scan`、`view`、`view_ddl`、`trigger_ddl`、`user_management`、`authorization`，以及对象删除对应的 `*_ddl`。当前执行器不启用这些能力。

## 3. 固定语义

### 查询、条件与 NULL

普通 JOIN 等同 INNER JOIN；INNER/LEFT/RIGHT 必须 ON，CROSS 不接受 ON。外连接缺失侧字段可空。不支持 FULL/NATURAL/USING/LATERAL。FROM 子查询必须命名且不能引用外部来源；标量、IN、EXISTS 子查询可相关绑定。标量仅接受一列，多行错误由运行期检查。

支持 COUNT(*)/COUNT(expr)、SUM、AVG、MAX、MIN；拒绝嵌套聚合、WHERE/ON 聚合。输出及 HAVING 的非聚合字段必须由分组表达式构成；不支持聚合 DISTINCT 或窗口函数。ORDER BY 支持输出别名和合法表达式；DISTINCT 排序必须在输出中。保留旧 SQL 已有排序规则。

INTERSECT 优先于 UNION/EXCEPT，后两者左结合；UNION 支持 ALL，其余不支持。括号可改变结合，外层 ORDER BY/LIMIT/OFFSET 应用于整个集合。

运行期必须遵守以下契约（尚未实现新算子）：

- 比较 NULL 得 UNKNOWN；WHERE/HAVING/ON 仅保留 TRUE，CHECK 仅 FALSE 违规。
- NULL 在分组和集合去重时归同组；UNIQUE 允许含 NULL 的重复键，外键采用 MATCH SIMPLE。
- ASC 默认 NULL LAST，DESC 默认 NULL FIRST。
- 聚合忽略 NULL，空输入 COUNT=0，其他聚合=NULL。
- LIKE 区分大小写，支持 `%`、`_` 和单字符 ESCAPE；BETWEEN 含两端；IN 列表非空。

### 数值与日期类型

BOOL 与 INT 严格区分；仅 INT 可提升 DECIMAL，不隐式解析字符串。整数保持有符号 64 位边界。DECIMAL 默认 (18,2)，参数满足 1≤p≤38、0≤s≤p。使用精确 Decimal；字面量赋值不得超过整数位容量或丢弃非零小数，表达式赋值的运行期结果仍需检查。

数值类型推导将 INT 视为 (19,0)。令输入为 (p1,s1)、(p2,s2)：

| 操作 | 候选整数位 i | 候选小数位 s |
|---|---|---|
| 加减 | max(p1−s1,p2−s2)+1 | max(s1,s2) |
| 乘法 | p1+p2−s1−s2+1 | s1+s2 |
| 除法/AVG | p1−s1+s2 | max(6,s1+p2+1) |

先将 s 限制到 max(0,38−i)，除法/AVG 再保证至少 6 位；结果 p=min(38,i+s)。INT 的加减乘及 SUM 仍为 INT。AVG 使用同一输入类型作为两个参数。共同类型采用最大整数位加最大小数位，超过 38 则拒绝。结果位数不足时必须运行期溢出报错，不能静默截断；除法/AVG 采用 ROUND_HALF_EVEN，其余运算不得丢弃非零小数。固定测试锁定 `1/3 = 0.3333333333333333333` 的编译折叠，并验证 38 位结果的六位小数 HALF_EVEN 中点向偶数舍入。

DATE/TIME/TIMESTAMP 使用标准类型字面量加 ISO 字符串；拒绝非法日历值、时区和超过六位秒小数。JSON 将 Decimal 转为精确十进制字符串、日期时间转 ISO 字符串，None 转 null。

### 对象管理

ALTER 一次一项：ADD/DROP COLUMN、RENAME COLUMN/TO、ALTER COLUMN TYPE、命名约束 ADD/DROP。编译检查旧定义与候选定义，主键不可空，外键必须引用类型一致的主键/唯一键，只支持 RESTRICT/NO ACTION。DEFAULT 仅常量表达式；CHECK 不含子查询或聚合。存量行检查、表重写和事务恢复交由成员二、三。

存在目录依赖时保守拒绝 ALTER/DROP；受约束列重命名或删除须先处理约束。CHECK 存在时当前对删除/重命名列采用保守拒绝，不自动改写 CHECK。

视图保存查询定义并独立展开，检查输出名称唯一、列数和循环；禁止视图写入。触发器仅 AFTER 行级，INSERT 仅 NEW、DELETE 仅 OLD、UPDATE 两者都有，OLD/NEW 不可赋值。动作仅 INSERT/UPDATE/DELETE/SELECT，BEGIN…END 中分号不切断触发器，普通事务 BEGIN 保持原行为。编译检测目录已知事件依赖环，计划注明丢弃 SELECT 动作结果；动态递归及失败时整体回滚属于执行器责任。

用户语句只检查账户/对象存在性及权限组合，不校验密码或真实管理员身份。数据库权限为 CREATE TABLE/INDEX/VIEW/TRIGGER；表权限 SELECT/INSERT/UPDATE/DELETE/DROP/ALTER；只读视图 SELECT/DROP；索引和触发器 DROP。不支持角色、列权限或 WITH GRANT OPTION。最终鉴权须在统一数据库入口覆盖所有子查询、视图和触发动作，按调用者身份执行。

密码在 Token、AST、计划的敏感字段中保留供未来接入，字段不进入 repr/跟踪 JSON；GUI 捕获的 SQL 同样脱敏。不得使用通用 asdict 或直接记录原始输入替代提供的序列化函数。本模块不持久化明文；密码派生、独立盐和账户存储由成员三实现。

## 4. 优化与资源保护

对单表 Filter 的 AND 条件选择可用索引：等值前缀最长、紧邻范围可用优先，最终以索引名称稳定排序。支持倒置比较常量与字段。不跨 OR 或外连接下推，始终保留完整 Filter；含潜在算术/子查询错误的谓词不缩窄扫描，避免隐藏其他行上的错误。不宣称成本最优或已有 B+ 树。

仅安全常量折叠；遇溢出、除零、精度丢失或子查询时保留表达式。测试专用受限参考求值器验证数值/三值布尔等价，不加入数据库运行时。

每表达式结构 Token 上限 64，查询树/视图展开深度 16，语句 AST 节点上限 4096。预算在解析或绑定递归前检查，迭代遍历同时覆盖旧 AST；组合表达式递归另有 64 层保护，失败带位置。

## 5. 十二类可复现示例

在仓库根目录安装项目后运行：

```powershell
python examples/compile_extensions.py
python -m pytest tests/compiler/test_extensions.py tests/integration/test_compiler_extensions.py -q
```

演示使用显式内存只读目录，不打开数据库文件，逐条输出以下计划：

| 类别 | 示例要点 | 预期计划节点 |
|---|---|---|
| F01 多表 | student a LEFT JOIN audit b ON a.id=b.id | Join |
| F02 聚合 | id,COUNT(*) GROUP BY id HAVING COUNT(*)>0 | Aggregate、Having |
| F03 条件 | name LIKE 'A_%' AND id BETWEEN 1 AND 10 | Filter |
| F04 集合 | student UNION ALL audit | SetOperation |
| F05 表达式 | id*2 AS doubled ORDER BY doubled | ExpressionProject、Sort |
| F06 结构 | ADD COLUMN score DECIMAL(10,2) | AlterTable |
| F07 约束 | PRIMARY KEY、CHECK(score>=0) | CreateTable |
| F08 类型 | NULL、1.25、DATE、TRUE | ExpressionProject |
| F09 索引 | CREATE INDEX by_id ON student(id) | CreateIndex |
| F10 视图 | CREATE VIEW ids AS SELECT id FROM student | CreateView |
| F11 触发器 | AFTER INSERT … VALUES(NEW.id,NEW.name) | CreateTrigger |
| F12 权限 | GRANT SELECT ON TABLE student TO alice | Grant |

完整 SQL 在演示脚本中。索引查询计划需要目录显式提供 IndexDefinition；示例不伪造真实已建索引。真实 GUI 可先建普通表，再运行 `EXPLAIN SELECT COUNT(*) FROM student;` 查看编译结果。

## 6. 错误与验证

主要新增错误码：AMBIGUOUS_COLUMN、DUPLICATE_ALIAS、INVALID_AGGREGATE、NON_GROUPED_COLUMN、SUBQUERY_COLUMN_COUNT、SET_COLUMN_COUNT、INVALID_ORDER_BY、INVALID_ESCAPE、INVALID_LITERAL、INVALID_TYPE、NUMERIC_OUT_OF_RANGE、INVALID_FOREIGN_KEY、DEPENDENT_OBJECT、CYCLIC_VIEW、READ_ONLY_VIEW、RECURSIVE_TRIGGER、UNKNOWN_USER、UNKNOWN_PERMISSION、CATALOG_CAPABILITY_UNAVAILABLE、FEATURE_NOT_EXECUTABLE、QUERY_TOO_DEEP、STATEMENT_TOO_COMPLEX。沿用 UNKNOWN_COLUMN、TYPE_MISMATCH、INTEGER_OUT_OF_RANGE 和 EXPRESSION_TOO_COMPLEX。语法错误仍包含 expected 和行列位置。

最终全套 Python 回归 **807 项通过，30.89 秒，无失败无跳过**；其中新增扩展编译专项 169 项、真实数据库/GUI 集成专项 10 项。原来四项“新语法应拒绝”的旧断言已迁移为合法编译用例，跟踪测试同步检查新增字段。历史 632 项通过不作为本次重测结果。

浏览器专项 `tests/browser/update.cjs` 通过（Chrome 无头模式）；十二类 `examples/compile_extensions.py` 演示全部编译成功。Markdown 本地链接、代码围栏及 Git 差异格式检查通过。测试数据库均为隔离目录，不操作业务库。

最终回归命令：

```powershell
python -m pytest -q -p no:cacheprovider --basetemp data/ext_verified11 --tb=short
```

专项覆盖十二类合法/非法编译、名字/类型错误、计划形状、作用域、自连接、外连接可空、视图循环、索引前缀/保守回退、精度/日期、密码哨兵、触发器切分和复杂度。集成验证真实文件重开、批次中途失败、显式事务失败/回滚、GUI 捕获一次编译和脱敏。浏览器验证扩展 EXPLAIN 可见、错误可见、原数据不变及现有 UPDATE 流程。

## 7. 后续接入清单与限制

成员二：实现新值类型编码、NULL 标志和记录版本；沿用现有 RecordId 作为记录定位值，定义 B+ 树键序列与 NULL/DECIMAL 比较编码；提供索引页、维护及恢复接口。本报告不选择系统表编号，不实现旧库迁移。

成员三：实现每类计划/表达式算子、Catalog 对象保存、依赖枚举和实际账户接口；实现标量多行错误、默认值应用、存量约束验证、索引同步、触发调度/递归保护/失败回滚、统一鉴权及 GUI 对象展示；按调用者权限执行视图和触发器。扩展 INSERT 暂沿用旧接口要求显式提供全部列，运行期默认值补齐入口待联调。

旧库迁移、首次管理员初始化继续待三人确认；不自动转换业务目录，不生成默认弱密码。新功能的真实数据结果、持久化、性能和端到端完成标准均未验收，不能将本报告的编译测试当作这些能力已经实现。
