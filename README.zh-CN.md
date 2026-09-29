# mysql-migration-guard

**在迁移跑起来之前，找出那些会锁死生产库的语句。**

[![CI](https://github.com/zekia0104/Mysql/actions/workflows/ci.yml/badge.svg)](https://github.com/zekia0104/Mysql/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

[English](README.md)

```console
$ migration-guard migrations/2026_09_26__orders_indexing.sql
CRITICAL migrations/2026_09_26__orders_indexing.sql:4  orders: MODIFY COLUMN rebuilds the whole table
         ddl.table-rebuild
         → MODIFY COLUMN total DECIMAL(12, 2) NOT NULL
         changing a column definition usually copies every row; only pure NULL/NOT NULL
         or DEFAULT changes avoid a rebuild. On MySQL 8.0.29 this copies every row while
         holding a write lock, so the table is unavailable for as long as that takes and
         the migration cannot be rolled back half way.
         fix: Run it with gh-ost or pt-online-schema-change, or split it: add a nullable
         column, backfill in batches, then change it in a maintenance window.
```

每条结论都给出**文件、行号、语句原文、为什么在你的服务器版本上危险、以及同一件事的安全写法**。

## 为什么需要它

`ALTER TABLE` 是迁移里唯一一种"执行成功了却把库搞挂了"的语句。它的失效方式全都很安静：

- MySQL 在无法原地完成 ALTER 时会**静默回退成整表重建**。语句里完全看不出来，测试环境 40 毫秒的迁移，生产上跑 40 分钟。
- `lock_wait_timeout` 默认 **31536000 秒 = 一年**。只要有一个长事务占着元数据锁，你的迁移**不会失败，而是挂住**，后面所有查询跟着排队。
- 同一句 `ADD COLUMN`，在 MySQL 8.0.29 上是**纯元数据操作**，在 5.7 上是**整表重建**。安全与否是服务器的属性，不是 SQL 的属性。
- `UPDATE orders SET status = 'x'` 少了 WHERE，就是一个锁住所有行的大事务；在行式复制下还要把每一行变更发给每个从库。

现有工具各自覆盖了相邻的问题：`sqlfluff` 管代码风格，`gh-ost` 和 `pt-online-schema-change` 负责**执行**在线变更，`pt-query-digest` 分析已经跑过的查询。**没有人负责"上线前审一遍"。**

## 安装

零第三方依赖。只要有 Python 3.11+。

```bash
pipx install git+https://github.com/zekia0104/Mysql
# 或从源码
git clone https://github.com/zekia0104/Mysql guard
cd guard && python -m pip install .
```

不安装也能跑：

```bash
PYTHONPATH=/path/to/mysql-migration-guard/src python -m migration_guard migrations/
```

## 用法

```bash
migration-guard migrations/                       # 递归审查整个目录
migration-guard --target-version 5.7 migrations/  # 你还在 5.7 上
migration-guard --format github migrations/       # 给 PR 打注解
migration-guard --stdin < migration.sql           # 管道里用
migration-guard --list-rules                      # 列出 27 条规则 id
```

### 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 干净。`INFO` 级别永远不会让构建失败。 |
| `1` | 至少一条 `WARNING`。 |
| `2` | 至少一条 `CRITICAL`。 |
| `3` | 用法错误、输入读不了、或没找到 `.sql` 文件。 |

`--fail-on critical` 让警告不再致命；`--fail-on never` 永远返回 `0`。**不需要解析任何输出**就能给部署做门禁。

### `--target-version` 不是可选项

```console
$ migration-guard examples/good --target-version 8.0.29 ; echo $?
0
$ migration-guard examples/good --target-version 5.7 ; echo $?
2
```

同一个文件，两个答案都是对的。`examples/good` 加了一个可空列并要求 `ALGORITHM=INSTANT`——8.0.29 不动数据行就能完成，5.7 会把整张表复制一遍。**不说清楚要上哪台服务器，等于只审了一半的问题。**

## 当作 GitHub Action 用

仓库自带一个 composite action，四行 YAML 就能接入：

```yaml
- uses: zekia0104/Mysql@v1
  with:
    paths: migrations
    target-version: "8.0.29"
    fail-on: warning
```

配合 `--format github`，每条结论会变成挂在 diff 对应行上的工作流注解，reviewer 在上下文里就能看到：

```
::error file=migrations/001.sql,line=4,title=ddl.table-rebuild::orders: MODIFY COLUMN rebuilds the whole table -- ...
```

## 检查哪些东西

33 条规则。真正能拦住事故的那些：

| 规则 | 严重度 | 抓什么问题 |
| --- | --- | --- |
| `ddl.table-rebuild` | critical | 语句一边持写锁一边复制全表 |
| `ddl.drop-table` | critical | `DROP TABLE`，回滚迁移也救不回来 |
| `ddl.drop-column` | critical | 数据丢失，且所有引用该列的查询立刻报错 |
| `ddl.change-charset` | critical | `CONVERT TO CHARACTER SET` 重写全表且可能写坏数据 |
| `dml.update-without-where` | critical | 一个事务锁住所有行 |
| `dml.delete-without-where` | critical | 同上，并且复制延迟无上限 |
| `session.foreign-key-checks-off` | critical | 违反外键的行被静默接受 |
| `sql.unterminated` | critical | 无法正确词法分析的 SQL 根本没法审 |
| `ddl.blocks-writes` | warning | 原地执行但期间阻塞写入 |
| `ddl.implicit-algorithm` | warning | 没写 `ALGORITHM`/`LOCK`：MySQL 会静默回退而不是报错 |
| `ddl.instant-unsupported` | warning | 写了 `ALGORITHM=INSTANT` 但该操作不支持 |
| `ddl.no-lock-wait-timeout` | warning | 用着默认"一年"的元数据锁等待时间 |
| `ddl.add-column-not-null` | warning | `NOT NULL` 又没有默认值 |
| `ddl.foreign-key` | warning | 校验每一行已有数据，并且之后串行化两表写入 |
| `ddl.drop-index` | warning | 相关查询静默变成全表扫描 |
| `ddl.truncate` | warning | 隐式提交、无法回滚、重置 `AUTO_INCREMENT` |
| `ddl.rename-table` | warning | 所有视图、触发器、下游消费者立刻失效 |
| `dml.unbounded-predicate` | warning | `LIKE '%x'`、`!=`、`NOT IN`、列上套函数 |
| `file.executable-comment` | warning | MySQL 会执行但 review 会略过的代码 |
| `session.sql-mode-relaxed` | warning | 迁移里关掉了严格模式 |
| `ddl.repeated-alter` | info | 两条会重建表的语句，本可以合成一条 |
| `dml.delete-without-limit` | info | 无限定的 DELETE，本可以分批 |
| `file.use-statement` | info | 迁移自己指定数据库 |
| `file.delimiter-command` | info | `DELIMITER` 是客户端命令，不是 SQL |
| `directive.unknown-rule` | warning | 写错了规则 id 的屏蔽指令，什么都没屏蔽掉 |
| `directive.missing-reason` | info | 没写理由的例外，后人无法判断是否还有效 |
| `directive.empty` | warning | `allow()` 里没写任何规则 |

`migration-guard --list-rules` 可以拿到准确 id。

### 几个值得说明的设计取舍

- **取最坏情况。** MySQL 文档里带条件的描述（"原地执行，除非表上有全文索引"）一律按更坏的那一侧归类。**低估一把锁会造成事故，高估只会多一条警告。**
- **不认识的子句就报成"不认识"**，不假设它安全。有规则专门把未识别的操作报出来，让你知道什么**没被检查**。
- **可执行注释当代码处理。** MySQL 会执行 `/*!80000 ... */`，所以 `ALTER TABLE t ADD COLUMN c INT /*!80000 , ALGORITHM=INSTANT */` 确实请求了 INSTANT。工具既采纳它**又报告它**，因为人读文件时看不见。
- **`--` 后面必须有空白才算注释。** `SELECT 1--2` 是算术。切错的解析器会把语句切碎，然后给出自信的胡说。
- **没有终结符的最后一条语句也算语句。** 迁移执行器接受它；悄悄丢掉它会是这个工具最糟糕的失效方式。
- **扫描器不会死循环。** 有一道"必须推进"的防护：任何构造如果没消费字符，就强制前进一格。对跑在 CI 里的工具来说，挂住是最糟的失败。

## 屏蔽某条结论

有些 ALTER 确实没问题——249 行的国家代码表，随便重建。那就**在文件里说明**，并给出理由：

```sql
ALTER TABLE country_codes MODIFY code CHAR(2);  -- migration-guard: allow(ddl.table-rebuild) reason: 249 rows, static
```

```sql
-- migration-guard: allow-file(file.use-statement) reason: generated from the schema dump

USE shop;
```

`directive.*` 这几条规则**本身不能被屏蔽**，所以规则 id 写错时一定会被报出来，而不是悄悄什么都没保护。

## 局限（说清楚）

一个把话说得太满的检查工具比没有更糟：

- **不连数据库。** 完全静态，所以不知道表有多少行。50 行的表重建无所谓——这正是 `allow(...)` 的用途。
- **类型和索引检查需要 `--schema`。** 不给的时候这六条规则保持沉默而不是猜，报告里也会写明。输入是 `mysqldump --no-data` 的输出，所以**依然**不需要连库、不需要驱动、不需要凭据：

  ```bash
  mysqldump --no-data --skip-comments --compact shop > schema.sql
  migration-guard --schema schema.sql migrations/
  ```

  给了之后，`examples/bad` 会多报出六条平时看不见的结论，包括给 `TEXT` 列建索引、以及把 `DECIMAL(12,2)` 收窄成 `DECIMAL(8,2)`。
- **谓词分析是启发式的。** 没有 schema 就无法知道优化器能否用上索引，所以 `dml.unbounded-predicate` 只报"人一眼能看出来的东西"，并明说"先用 EXPLAIN 确认"。
- **只针对 MySQL，不是 MariaDB。** 在线 DDL 矩阵和可执行注释都按 MySQL 文档来。MariaDB 的 `/*M! */` 能识别，但矩阵没为它调过。
- **审的是语句，不是顺序。** 只有"在前面那条迁移之后执行才安全"的迁移，它看不出来。

## 参与开发

```bash
git clone https://github.com/zekia0104/Mysql guard
cd guard
python -m unittest discover -s tests -t . -v   # 119 个测试，零依赖
ruff check .                                    # 可选
```

不需要 `pip install`：`tests/__init__.py` 会把 `src` 加进路径，所有夹具都是字符串。

两个文件最值得先读：

- `src/migration_guard/sqltext.py` —— 词法分析器，也是"一个小错误变成一份自信的错误报告"的地方。
- `src/migration_guard/statements.py` —— 在线 DDL 矩阵，整个工具赖以成立的领域知识。

测试里有一条 **registry coverage 测试**：一个夹具同时违反所有规则，然后断言每条已注册规则都产出了结论。它是有价值的——开发过程中有两条规则把 token kind 和字面量 `NUMBER` 比较，而正确值是 `number` 常量，于是它们**永远不会触发**，在 review 里看起来是绿的，实际上没保护任何人。

## 许可证

[MIT](LICENSE) © 2026 zekia0104
