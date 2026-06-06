# 工作通 — BOSS Chat Assistant

Python + MySQL + DeepSeek + Playwright 的本地求职自动化助手。

## 架构

由三部分组成：

- **FastAPI 后端**：简历解析、DeepSeek 分析、岗位评分、首句生成、回复生成、事件记录。
- **Playwright 自动化引擎**：启动持久化 Chrome 浏览器，通过 `page.evaluate()` / `page.click()` / `page.fill()` 直接在 BOSS 页面执行自动化操作。反检测脚本在每页加载前注入。
- **Chrome 扩展（可选）**：在 BOSS 页面内提供浮动控制面板，兼容旧的使用方式。

## 1. 环境准备

### 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### MySQL

创建数据库和用户后：

```sql
SOURCE sql/schema.sql;
```

或者让 FastAPI 启动时 ORM 自动建表。

## 2. 环境变量

复制 `.env.example` 为 `.env`，填写：

```bash
APP_HOST=127.0.0.1
APP_PORT=8788
DATABASE_URL=mysql+pymysql://boss_user:boss_password@127.0.0.1:3306/boss_chat_assistant?charset=utf8mb4
DEEPSEEK_API_KEY=你的 DeepSeek API Key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

## 3. 启动后端

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8788 --reload
```

控制台地址：

```text
http://127.0.0.1:8788
```

## 4. 使用方式

### 方式一：Playwright 自动投递（推荐）

1. 打开控制台 `http://127.0.0.1:8788`
2. 上传简历 → 等待 DeepSeek 分析
3. 点击 **"启动浏览器"** → Playwright 打开专用 Chrome 窗口
4. 在 Chrome 窗口中登录 BOSS 直聘
5. 切换回控制台，点击 **"开始自动投递"**
6. 实时查看进度条、发送数、跳过数

Playwright 引擎会：
- 自动提取岗位列表
- 逐一评估每个岗位（DeepSeek 评分）
- 自动点击"立即沟通"
- 自动填写个性化的打招呼话术
- 自动发送
- 随机间隔（模拟人类行为）
- 遇到验证码/风险提示时停止

### 方式二：Chrome 扩展（兼容旧模式）

1. 打开 `chrome://extensions`，开启"开发者模式"
2. 加载 `extension/` 目录
3. 打开 BOSS 岗位搜索页面
4. 页面右下角出现 `BOSS Assistant` 面板
5. 通过面板或控制台的命令按钮操作

## 5. 自动化边界

- 不调用 BOSS 私有 API
- 不读取或重放 cookie
- 不处理滑块验证码
- 不切换代理或伪装设备
- 遇到验证码、安全验证、账号异常、操作频繁等提示自动停止
- 是否自动发送首句、是否自动回复，在控制台配置

## 6. 排查

| 问题 | 检查 |
|------|------|
| 浏览器启动失败 | 确认 `playwright install chromium` 已执行 |
| 点击"开始投递"无反应 | 确认简历已上传、DeepSeek API Key 已填写、今日额度未用完 |
| BOSS 检测到自动化 | 刷新页面、手动完成验证后重新点击"开始投递" |
| Playwright 找不到元素 | BOSS UI 更新可能导致选择器失效，查看事件日志了解具体错误 |
