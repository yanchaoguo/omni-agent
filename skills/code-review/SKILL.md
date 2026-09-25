# 代码审查 Skill (Code Review)

> **强调审查的严谨性和可追溯性**：每条审查意见均标注文件名/行号/严重等级/规则编号, 可追溯到具体代码行, 并输出 HTML 审查报告与 diff 标记。

## 适用场景
- GitHub PR 自动审查与摘要
- 本地代码逐行审查与 diff 标记
- 测试用例与回归测试检查
- HTML 审查报告输出与推送

## 审查流程

### 1. 收集代码变更
- 本地审查: `bash` 执行 `git diff HEAD~1` 或 `git diff --cached` 获取变更
- PR 审查: `bash` 执行 `gh pr diff <PR_NUMBER>` 获取 PR diff
- 提取变更文件列表与每个文件的增删行

### 2. 逐文件审查 (严密度: 每个变更块必须覆盖)
对每个变更文件, 按以下维度逐行审查:

#### 2.1 安全性审查
- SQL 注入: 检查字符串拼接 SQL 语句
- XSS: 检查未转义的用户输入输出到 HTML
- 命令注入: 检查 `os.system`/`subprocess` 中使用未净化的用户输入
- 硬编码密钥: 检查代码中直接写入 API Key/密码/Token
- 路径穿越: 检查文件操作未做路径校验
- 不安全反序列化: 检查 `pickle.loads`/`yaml.load` 未限制

#### 2.2 逻辑正确性审查
- 空指针/None 解引用风险
- 数组越界/除零异常
- 资源泄漏(文件/连接未关闭)
- 并发安全(共享变量无锁访问)
- 异常吞没(空 except/过宽 except)

#### 2.3 代码规范审查
- 函数过长(>80行)或圈复杂度过高
- 命名不规范(魔术数字/含义不清变量名)
- 重复代码块
- 缺少文档字符串/类型标注

#### 2.4 测试用例审查
- 变更是否附带对应测试用例
- 测试覆盖率是否充分(关键路径是否有测试)
- 回归测试: 变更是否可能破坏已有功能
- 边界条件测试(空输入/超大输入/非法格式)

### 3. 审查意见输出格式
每条意见必须包含:
```
[严重等级] [规则编号] 文件路径:行号
问题描述: ...
修改建议: ...
```

严重等级:
-  CRITICAL: 安全漏洞/数据丢失风险, 必须修复后合并
- 🟠 MAJOR: 逻辑错误/资源泄漏, 强烈建议修复
-  MINOR: 代码规范/可维护性, 建议修复
- 🔵 INFO: 优化建议, 可选

### 4. HTML 审查报告生成
审查完成后, 使用 `write_file` 生成 HTML 报告:
- 文件名: `code_review_report.html`
- 内容: 包含审查摘要(统计各等级问题数)、变更文件列表、逐文件 diff 标记(红色删除/绿色新增)、审查意见列表
- 设置 `with_sources=false`
- 通过 `send_user_msg` 推送 HTML 报告文件

### 5. 推送与归档
- 使用 `connector_push` 推送审查摘要到飞书/企业微信(如已配置)
- 使用 `send_user_msg` 交付 HTML 审查报告
- 审查报告归档到 `.haisnap/reviews/` 目录

## 审查示例

### 示例输入
```
用户: 审查最近的代码变更
```

### 示例执行步骤
1. `bash` 执行 `git diff HEAD~1 --stat` 获取变更概览
2. `bash` 执行 `git diff HEAD~1` 获取完整 diff
3. `read_file` 读取每个变更文件的完整内容(获取行号上下文)
4. 逐文件逐行审查, 记录每条意见
5. `write_file` 生成 `code_review_report.html` 审查报告
6. `send_user_msg` 推送审查报告

### 示例审查意见
```
 CRITICAL [SEC001] auth.py:42
问题描述: SQL 语句直接拼接用户输入 `username`, 存在 SQL 注入风险
修改建议: 使用参数化查询 `cursor.execute("SELECT * FROM users WHERE name=?", (username,))`

🟠 MAJOR [LOG003] utils.py:128
问题描述: `except: pass` 吞没所有异常, 包括 KeyboardInterrupt 和 SystemExit
修改建议: 捕获具体异常 `except (ValueError, TypeError) as e:`, 并记录日志

 MINOR [STYLE002] handler.py:67
问题描述: 函数 `process_data` 长达 120 行, 圈复杂度过高
修改建议: 拆分为 `_validate_input()`, `_transform()`, `_write_output()` 三个子函数
```

## 审查报告 HTML 结构
```html
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>代码审查报告</title>
<style>
  body{font-family:system-ui,sans-serif;max-width:960px;margin:auto;padding:20px}
  .critical{color:#dc2626}.major{color:#ea580c}.minor{color:#ca8a04}.info{color:#2563eb}
  .diff-add{background:#dcfce7;color:#166534}.diff-del{background:#fee2e2;color:#991b1b}
  .file-section{border:1px solid #e5e7eb;border-radius:8px;margin:12px 0;padding:12px}
  .issue{margin:8px 0;padding:8px;border-left:3px solid #e5e7eb}
  table{width:100%;border-collapse:collapse}th,td{border:1px solid #e5e7eb;padding:6px}
</style></head>
<body>
  <h1>📋 代码审查报告</h1>
  <p>审查时间: {{datetime}} · 审查范围: {{range}} · 变更文件: {{file_count}}</p>
  <table>
    <tr><th>等级</th><th>数量</th></tr>
    <tr><td class="critical"> CRITICAL</td><td>{{critical_count}}</td></tr>
    <tr><td class="major">🟠 MAJOR</td><td>{{major_count}}</td></tr>
    <tr><td class="minor"> MINOR</td><td>{{minor_count}}</td></tr>
    <tr><td class="info">🔵 INFO</td><td>{{info_count}}</td></tr>
  </table>
  <!-- 逐文件 diff 与审查意见 -->
</body></html>
```

## 注意事项
- 审查必须覆盖所有变更文件, 不可跳过
- 每条意见必须标注行号, 确保可追溯
- CRITICAL 级问题必须在报告中醒目标注
- 审查报告 HTML 必须自包含(内联样式), 无外部依赖
