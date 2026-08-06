---
name: mysql-employees-analysis
description: >
  将中文业务问题转换为 SQL 查询并分析 MySQL employees 示例数据库。
  适用于员工信息查询（如"工号12345的员工信息"）、
  薪资统计（如"平均薪资最高的部门"）、
  部门分析（如"各部门人数分布"）、
  职位变动历史（如"某员工的晋升路径"）等场景。
version: 1.0.0
allowed_tools: [execute_sql]
tags: [database, mysql, sql, employees, analysis]
---

# MySQL 员工数据库分析技能

## 概述

这个技能专门用于分析 MySQL 官方提供的 `employees` 示例数据库。
该数据库包含约 300,000 名虚拟员工的记录，涵盖 1985-2000 年的数据。

**核心能力**：
- 理解中文自然语言的业务问题
- 转换为高效的 SQL 查询
- 执行查询并解读结果
- 提供业务洞察和数据解读

## 数据库结构

### 核心表结构

| 表名           | 说明         | 关键字段                                                     |
| -------------- | ------------ | ------------------------------------------------------------ |
| `employees`    | 员工基本信息 | emp_no, birth_date, first_name, last_name, gender, hire_date |
| `salaries`     | 薪资历史     | emp_no, salary, from_date, to_date                           |
| `titles`       | 职位历史     | emp_no, title, from_date, to_date                            |
| `dept_emp`     | 员工部门关系 | emp_no, dept_no, from_date, to_date                          |
| `dept_manager` | 部门经理     | emp_no, dept_no, from_date, to_date                          |
| `departments`  | 部门信息     | dept_no, dept_name                                           |

### 关键约定

⚠️ **重要**：`to_date = '9999-01-01'` 表示"当前有效"的记录。
查询"当前"状态时（如现任员工、当前薪资），必须加此过滤条件。

完整的表结构参见：`db_schema.sql`

## 工作流程

### 第一步：理解需求

仔细分析用户的中文描述，识别：
- **查询目标**：要查什么数据？（员工、薪资、部门...）
- **筛选条件**：有什么限制？（特定部门、时间范围、薪资区间...）
- **聚合维度**：需要统计吗？（平均值、总数、排名...）
- **时间范围**：是历史数据还是当前状态？

### 第二步：构建 SQL

根据需求选择合适的查询模式（见下方"常见查询模式"）。

**编写原则**：
1. 使用明确的表别名（如 `e` for employees）
2. JOIN 时优先使用主键/外键
3. 注意日期过滤（特别是 `to_date`）
4. 合理使用索引字段
5. 大结果集要加 LIMIT

### 第三步：执行查询

调用 `execute_sql` 工具执行构建好的 SQL。

```python
# 示例调用（智能体会自动转换为工具调用）
result = execute_sql(query="SELECT ...")


### 第四步：解读结果

将查询结果转化为自然语言回答：
- 用表格呈现结构化数据
- 突出关键数据点
- 提供业务洞察（如趋势、异常）
- 如果结果为空，说明可能的原因

## 常见查询模式

### 模式 1：基础信息查询


-- 查询特定员工的基本信息
SELECT emp_no, CONCAT(first_name, ' ', last_name) AS full_name,
       gender, birth_date, hire_date
FROM employees
WHERE emp_no = <员工号>;


### 模式 2：当前状态查询


-- 查询当前薪资最高的员工（TOP 10）
SELECT e.emp_no,
       CONCAT(e.first_name, ' ', e.last_name) AS name,
       s.salary
FROM employees e
JOIN salaries s ON e.emp_no = s.emp_no
WHERE s.to_date = '9999-01-01'  -- 当前薪资
ORDER BY s.salary DESC
LIMIT 10;


### 模式 3：历史趋势分析


-- 查询某员工的薪资变化历史
SELECT emp_no, salary, from_date, to_date,
       salary - LAG(salary) OVER (ORDER BY from_date) AS increase
FROM salaries
WHERE emp_no = <员工号>
ORDER BY from_date;


### 模式 4：跨表关联查询


-- 查询各部门的平均薪资（当前）
SELECT d.dept_name,
       COUNT(DISTINCT de.emp_no) AS emp_count,
       ROUND(AVG(s.salary), 2) AS avg_salary
FROM departments d
JOIN dept_emp de ON d.dept_no = de.dept_no
JOIN salaries s ON de.emp_no = s.emp_no
WHERE de.to_date = '9999-01-01'  -- 当前在职
  AND s.to_date = '9999-01-01'   -- 当前薪资
GROUP BY d.dept_name
ORDER BY avg_salary DESC;


### 模式 5：复杂业务分析


-- 分析"话语权"：综合管理层级、薪资、任职时长
WITH manager_hierarchy AS (
    -- 统计每个经理管理的下属数
    SELECT dm.emp_no, COUNT(de.emp_no) AS subordinate_count
    FROM dept_manager dm
    JOIN dept_emp de ON dm.dept_no = de.dept_no
    WHERE dm.to_date = '9999-01-01'
      AND de.to_date = '9999-01-01'
      AND de.emp_no != dm.emp_no
    GROUP BY dm.emp_no
),
current_salary AS (
    -- 当前薪资
    SELECT emp_no, salary
    FROM salaries
    WHERE to_date = '9999-01-01'
),
tenure AS (
    -- 任职时长（年）
    SELECT emp_no,
           TIMESTAMPDIFF(YEAR, hire_date, CURDATE()) AS years_employed
    FROM employees
)
SELECT e.emp_no,
       CONCAT(e.first_name, ' ', e.last_name) AS name,
       COALESCE(mh.subordinate_count, 0) AS team_size,
       cs.salary,
       t.years_employed,
       -- 简单的话语权评分（可根据业务调整权重）
       (COALESCE(mh.subordinate_count, 0) * 10 +
        cs.salary / 1000 +
        t.years_employed * 5) AS influence_score
FROM employees e
JOIN current_salary cs ON e.emp_no = cs.emp_no
JOIN tenure t ON e.emp_no = t.emp_no
LEFT JOIN manager_hierarchy mh ON e.emp_no = mh.emp_no
WHERE cs.salary > 60000  -- 过滤低薪员工
ORDER BY influence_score DESC
LIMIT 20;


## 注意事项

### ⚠️ 时间字段的正确处理

- <strong>当前状态</strong>：必须使用 `to_date = '9999-01-01'` 过滤
- <strong>历史查询</strong>：注意 `from_date` 和 `to_date` 的范围
- <strong>时间计算</strong>：使用 `TIMESTAMPDIFF`、`DATEDIFF` 等函数

### ⚠️ 性能优化

- <strong>大表 JOIN</strong>：优先使用索引字段（emp_no, dept_no）
- <strong>聚合查询</strong>：合理使用 GROUP BY 和 HAVING
- <strong>结果限制</strong>：对于展示类查询，添加 LIMIT 限制
- <strong>子查询优化</strong>：复杂查询使用 WITH (CTE) 提高可读性和性能

### ⚠️ 数据质量

- <strong>NULL 值处理</strong>：使用 COALESCE 或 IFNULL 处理空值
- <strong>重复记录</strong>：注意员工可能多次调岗，查询时考虑去重
- <strong>数据范围</strong>：数据库只包含 1985-2000 年的数据，查询时注意时间边界

## 故障排查

<strong>问题 1：查询结果为空</strong>
- 检查是否正确使用了 `to_date = '9999-01-01'`
- 验证员工号或部门号是否存在
- 检查日期范围是否合理

<strong>问题 2：查询速度慢</strong>
- 检查是否缺少索引字段的 WHERE 条件
- 考虑将复杂查询拆分为多步
- 使用 EXPLAIN 分析查询计划

<strong>问题 3：统计数据不准确</strong>
- 注意区分"历史"和"当前"状态
- 检查 JOIN 条件是否遗漏
- 验证聚合函数的使用是否正确