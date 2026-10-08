# Invoice Manager 发票管理系统

AI 驱动的中文发票识别系统，采用 **OCR + LLM 双源验证**，准确率更高、数据完全本地可控。

> 本项目源码不含运行环境（Python 虚拟环境、前端依赖、内置 PostgreSQL 均在 `.gitignore` 中忽略），克隆后请按下方「如何使用」安装依赖。

---

## 如何使用

### 一、环境要求（首次准备）

| 需要 | 说明 | 安装地址 |
|------|------|---------|
| Windows 10/11（64 位）或 macOS / Linux | 本地运行；也支持 Docker | — |
| Python 3.10+ | 后端与内置数据库运行环境（安装时**务必勾选 "Add python.exe to PATH"**） | https://www.python.org/downloads/ |
| Node.js 18+ | 前端运行环境 | https://nodejs.org/ |
| 联网（仅首次） | 首次启动需联网下载依赖与 PaddleOCR 识别模型；之后可完全离线 | — |
| Docker（可选） | 如不想要本地 Python / Node 环境，可用 Docker Compose 一键启动 | https://www.docker.com/ |

### 二、方式 A：本地启动（推荐开发 / 调试）

1. 克隆仓库并进入目录：
   ```bash
   git clone <你的仓库地址>
   cd Invoice-Manager
   ```
2. 一键安装后端环境 + 初始化内置数据库（**首次约 5–15 分钟，请保持联网**）：
   ```bash
   python setup_env.py
   ```
   该脚本会创建 Python 虚拟环境（`.pgvenv`、`backend/venv`）并安装依赖，最后启动 PostgreSQL（端口 5435，数据库 `invoice_db`）。
3. 安装前端依赖（首次）：
   ```bash
   cd frontend
   npm install
   cd ..
   ```
4. 分别启动三个服务（每个用独立终端窗口）：

   **(1) 数据库**（若未运行，或重启电脑后需重新拉起）：
   ```bash
   python start_pg.py --once
   ```
   > 脚本会自动复用已运行的数据库；首次会自动初始化数据表。

   **(2) 后端**（在 `backend/` 目录）：
   ```bash
   cd backend
   venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 18080
   ```
   > macOS / Linux 用：`venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 18080`

   **(3) 前端**（在 `frontend/` 目录）：
   ```bash
   cd frontend
   npm run dev
   ```
5. 浏览器打开 `http://localhost:15173` 即可使用。

### 三、方式 B：Docker 一键启动（推荐纯部署）

本项目已提供 `docker-compose.yml`，一条命令拉起数据库 + 后端 + 前端：

```bash
docker-compose up --build
```

启动后访问 `http://localhost:15173`。后端 API 在 `http://localhost:18080`，Swagger 文档在 `http://localhost:18080/docs`。

> 说明：Docker 方式使用独立的 PostgreSQL 容器（容器内 5432，映射到本机 5435），不依赖本地的 `.pgvenv` / `.pgdata`。

### 四、配置 LLM（可选）

系统支持 **6 家** LLM 提供商，可在网页里实时切换：

| 厂商 | 推荐模型 | 申请 Key |
|------|---------|---------|
| OpenAI | GPT-4o / GPT-4o-mini | [platform.openai.com](https://platform.openai.com/) |
| Anthropic | Claude 3.5 Sonnet | [console.anthropic.com](https://console.anthropic.com/) |
| Google | Gemini 1.5 Flash | [aistudio.google.com](https://aistudio.google.com/) |
| 阿里云 通义千问 | Qwen-VL | [dashscope.console.aliyun.com](https://dashscope.console.aliyun.com/) |
| DeepSeek | DeepSeek-VL | [platform.deepseek.com](https://platform.deepseek.com/) |
| 智谱 GLM | GLM-4V | [open.bigmodel.cn](https://open.bigmodel.cn/) |

- **方式 A（推荐）**：直接在网页的「设置」页填写 API Key，实时生效，无需重启。
- **方式 B**：编辑 `backend/.env`（首次运行 `setup_env.py` 后会自动生成，含数据库连接），加入例如 `OPENAI_API_KEY=sk-xxxx`，保存后重启后端服务。

> 注意：`backend/.env` 已在 `.gitignore` 中忽略，**不会进入仓库**，可放心填写你的密钥。

### 五、访问地址

| 服务 | 地址 | 说明 |
|-----|------|------|
| 前端界面 | http://localhost:15173 | 主操作界面 |
| 后端 API | http://localhost:18080 | REST API |
| API 文档 | http://localhost:18080/docs | Swagger UI |

### 六、常见问题

- **首次一定要联网**：后端依赖、前端依赖、以及 PaddleOCR 识别模型都是在第一次使用时联网获取的，之后可离线。
- **启动很慢 / 卡住**：首次安装依赖确实要几分钟，请耐心等待；若被杀毒软件拦截，请允许该程序运行。
- **端口被占用**：确保 5435 / 18080 / 15173 没被别的程序占用，关掉占用程序或重启电脑后再启动。
- **PaddleOCR 模型下载**：首次识别会从官网下载模型到用户目录，之后离线可用；若 `setup_env.py` 因 Paddle 体积过大安装失败，程序会自动退回到不含 OCR 的核心依赖，应用仍可启动（仅 OCR 功能暂时禁用）。
- **PDF 识别**：`pdfplumber` 已内置；若想把 PDF 直接转图再识别，可选安装 Poppler。
- **想干净地停掉服务**：本地方式直接关闭三个终端窗口即可；Docker 方式用 `docker-compose down`。

> 💡 **提示**：即使不配置任何 LLM，系统也能正常运行（仅使用 OCR）。配置 LLM 后可获得 OCR + LLM 双源验证能力，准确率显著提升。

---

## 功能介绍

### 核心亮点

- **🔍 双源验证** —— OCR + LLM 双重识别，字段冲突自动标记待审核
- **🤖 多模型支持** —— 支持 GPT-4o / Claude / Gemini / 通义千问 / DeepSeek / 智谱 GLM
- **🇨🇳 中文发票优化** —— PaddleOCR 深度优化 + 增值税发票专用提示词
- **⚡ 并行处理** —— OCR 和 LLM 同时运行，识别更快
- **🐳 一键部署** —— 运行 setup_env.py 或 docker-compose 自动装好数据库、后端、前端，无需手动配环境
- **🔒 数据安全** —— 本地私有部署，发票数据不离开你的电脑

### 发票处理

| 功能 | 描述 |
|-----|------|
| 📤 批量上传 | 支持 PDF、JPG、PNG，单次多文件上传 |
| 🔍 OCR 识别 | PaddleOCR 中文优化，300DPI 高清识别 |
| 🤖 LLM 解析 | Vision 模型直接理解发票图像 |
| ⚖️ 智能比对 | 自动对比 OCR 和 LLM 结果，标记差异 |
| ✏️ 人工复核 | 冲突字段可视化，一键选择正确值 |
| 📊 统计报表 | 金额汇总、税额统计、状态分布 |

### 提取字段

```
├── 基础信息
│   ├── 发票号码 (invoice_number)
│   ├── 开票日期 (issue_date)
│   └── 项目名称 (item_name)
│
├── 购买方信息
│   ├── 购买方名称 (buyer_name)
│   └── 购买方税号 (buyer_tax_id)
│
├── 销售方信息
│   ├── 销售方名称 (seller_name)
│   └── 销售方税号 (seller_tax_id)
│
└── 金额信息
    ├── 金额 (amount)
    ├── 税率 (tax_rate)
    ├── 税额 (tax_amount)
    └── 价税合计 (total_with_tax)
```

### 状态流转

```
UPLOADED → PROCESSING → REVIEWING → CONFIRMED → REIMBURSED
  上传       识别中       待审核       已确认       已报销
                            ↓
                      NOT_REIMBURSED
                         未报销
```

---

## 技术栈

### 后端 Backend

- **FastAPI 0.109** —— 异步 REST API
- **SQLAlchemy 2.0 + asyncpg** —— 异步 ORM 与数据库驱动
- **PaddleOCR 2.7** —— 中文发票 OCR 识别
- **多 LLM SDK 集成** —— OpenAI / Anthropic / Google / 通义千问 / DeepSeek / 智谱
- **PostgreSQL**（内嵌，通过 `pgserver` 内置，端口 5435）

### 前端 Frontend

- **React 18.2 + TypeScript** —— 界面与类型安全
- **Vite 5.0** —— 开发与构建工具
- **Ant Design 5.13** —— UI 组件库
- **CSS Modules + CSS 变量** —— 主题与样式
- **Space Grotesk + Inter** —— 字体
- **Axios** —— HTTP 客户端
