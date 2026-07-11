<p align="center">
  <img src="packages/frontend/public/favicon.svg" width="80" height="80" alt="OpenEcon logo" />
</p>

<h1 align="center">OpenEcon Data</h1>

<p align="center">
  <strong>为你的 AI 智能体提供准确的经济数据。</strong><br/>
  来自 FRED、World Bank、IMF、Eurostat 等 10 个来源的 33 万条指标 —— 一条 MCP 命令即可接入。
</p>

<p align="center">
  <a href="https://data.openecon.ai/chat"><img src="https://img.shields.io/badge/在线体验-Live_Demo-blue?style=flat-square" alt="在线体验" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue?style=flat-square" alt="AGPL-3.0 License" /></a>
  <a href="https://github.com/hanlulong/openecon-data/stargazers"><img src="https://img.shields.io/github/stars/hanlulong/openecon-data?style=flat-square" alt="Stars" /></a>
  <a href="https://github.com/hanlulong/openecon-data/issues"><img src="https://img.shields.io/github/issues/hanlulong/openecon-data?style=flat-square" alt="Issues" /></a>
  <img src="https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/MCP-Server-purple?style=flat-square" alt="MCP Server" />
</p>

<p align="center">
  <a href="README.md">English</a> &middot;
  <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <a href="https://openecon.ai">官网</a> &middot;
  <a href="https://data.openecon.ai/chat">在线应用</a> &middot;
  <a href="docs/README.md">文档</a> &middot;
  <a href="docs/reference/api.md">API 参考</a> &middot;
  <a href="docs/development/DEVELOPER_CONTRIBUTOR_GUIDE.md">参与贡献</a>
</p>

---

## 安装（一行命令，然后直接对话）

```bash
curl -fsSL https://raw.githubusercontent.com/hanlulong/openecon-data/main/scripts/install.sh | bash
```

就这么简单。脚本会自动识别 Claude Code 和 Codex 并完成全部配置。之后直接提问即可：

```
你："美国 GDP 增长率是多少？"          → 智能体从 FRED 获取真实数据
你："对比 G7 国家的通货膨胀"            → World Bank 提供 7 国数据
你："比特币近 30 天价格"                → CoinGecko 实时数据
```

无需特殊语法，无需"调用 query_data"。用自然语言提问，剩下的交给智能体。

<details>
<summary><b>手动安装（如果你更习惯）</b></summary>

**Claude Code：**
```bash
claude mcp add --transport sse openecon-data https://data.openecon.ai/mcp --scope user
```

**Codex：**
```bash
codex mcp add openecon-data --url https://data.openecon.ai/mcp
```

**任意 MCP 智能体：** 端点 `https://data.openecon.ai/mcp`（SSE 传输）

更多斜杠命令与自动触发选项见 [skills/README.md](skills/README.md)。
</details>

---

<p align="center">
  <img src="docs/assets/demo.gif" width="800" alt="OpenEcon Data —— 用自然语言输入查询，得到带有 FRED、World Bank 等数据的图表" />
</p>

## 为什么你的智能体需要它

AI 智能体会"编造"经济数据。当你问大模型"美国 GDP 是多少？"，它给出的往往是听起来合理、实则过时或错误的数字。OpenEcon 解决了这一问题：

| | 不用 OpenEcon | 使用 OpenEcon |
|---|---|---|
| **数据来源** | 大模型的训练数据（陈旧） | 官方 API（FRED、World Bank、IMF） |
| **准确性** | 近似、经常出错 | 经过核验，附来源出处 |
| **覆盖范围** | 大模型记得多少算多少 | 33 万+ 指标，200+ 国家 |
| **时效性** | 落后数月甚至数年 | 接近实时（FRED、ExchangeRate） |
| **可核验** | 无来源链接 | 每条结果都附来源 URL |

## 支持中文查询

OpenEcon 原生支持中文提问。解析器会识别你使用的语言，把指标概念翻译成规范英文用于检索，再从正确的数据源取回官方数据。你可以直接问：

```
"北京的 GDP"
"中国近十年的失业率"
"对比中国、美国、日本的通货膨胀"
"浙江的 GDP 增长"
```

关键的用户提示信息（如"无数据""仅有全国级数据"等）也会以中文返回。除中文外，还支持英语、西班牙语、法语、德语、日语等。

通过 HTTP API 直接发起中文查询：

```bash
curl -X POST https://data.openecon.ai/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "中国近十年的失业率"}'
```

## 你可以问什么

```
"美国近 10 年 GDP 增长"                 → FRED，季度图表
"对比中国、印度、巴西 2018-2024 GDP"     → World Bank，多国对比
"2019-2023 金砖国家通胀率"               → World Bank，自动展开为 5 国
"近 24 个月欧元/美元汇率"                → ExchangeRate-API，货币对图表
"2010 年至今美国失业率与 CPI 对照"        → FRED，双轴叠加
"2020-2024 中国对美出口"                 → UN Comtrade，双边贸易流
"美国、英国、日本的信贷/GDP 比（来自 BIS）" → BIS，金融稳定数据
"比特币去年的价格"                       → CoinGecko，加密货币图表
"FRED 有哪些通胀相关指标？"              → 指标发现，文本回复
```

**多轮对话自然衔接：**
```
你："美国近 5 年 GDP"          → 显示美国 GDP 图表
你："加上德国和日本"           → 更新为 3 个国家
你："换成人均呢？"             → 切换为人均 GDP
你："只看 2020-2023"           → 收窄时间范围
```

## 快速上手

### 使用网页应用（无需安装）

**[data.openecon.ai/chat](https://data.openecon.ai/chat)** —— 打开浏览器即可体验，无需安装。前 20 次查询无需注册。用邮箱或 Google 免费注册后可继续使用、保存历史记录并解锁 Pro 模式。

### 自托管

```bash
git clone https://github.com/hanlulong/openecon-data.git
cd openecon-data
./scripts/setup.sh            # 安装 npm 与 Python 依赖，创建 backend/.venv，复制 .env.example → .env
```

然后编辑 `.env`，设置后端启动所需的两个值：

```bash
OPENROUTER_API_KEY=sk-or-...                  # 必填（LLM 解析）—— https://openrouter.ai/keys
JWT_SECRET=...                                # 必填 —— 用 openssl rand -hex 32 生成
```

启动前后端：

```bash
python3 scripts/restart_dev.py
# 后端：http://localhost:3001  |  前端：http://localhost:5173
```

然后发起第一个查询 —— 自然语言输入，带来源的数据输出：

```bash
curl -X POST http://localhost:3001/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "2023 年以来的美国失业率"}'
```

```jsonc
{
  "data": [{
    "metadata": {
      "source": "FRED",
      "indicator": "Unemployment Rate",
      "unit": "Percent",
      "sourceUrl": "https://fred.stlouisfed.org/series/UNRATE"  // 每条结果都链接到来源
    },
    "data": [{ "date": "2023-01", "value": 3.4 }, /* ... */]
  }]
}
```

<details>
<summary><b>手动配置（如果你不想用 setup.sh）</b></summary>

```bash
npm install
python3 -m venv backend/.venv
source backend/.venv/bin/activate            # Windows: backend\.venv\Scripts\activate
pip install --upgrade pip
pip install -r backend/requirements.txt
cp .env.example .env                         # 然后设置 OPENROUTER_API_KEY 与 JWT_SECRET
python3 scripts/restart_dev.py
```
</details>

<details>
<summary><b>环境要求</b></summary>

- Python 3.10+
- Node.js 18+
- **启动后端所需：**
  - `OPENROUTER_API_KEY` —— 用于 LLM 解析的 [OpenRouter API 密钥](https://openrouter.ai/keys)（若将 `LLM_PROVIDER` 设为 `vllm`/`ollama`/`lm-studio` 等本地模型则非必需）
  - `JWT_SECRET` —— 任意随机密钥；用 `openssl rand -hex 32` 生成
- 可选：FRED、Comtrade、CoinGecko 的 API 密钥
- 可选：Supabase 凭据（启用真实鉴权、Google 登录与持久化历史记录；缺省时开发环境使用 mock 鉴权）

完整安装说明见[快速上手指南](docs/guides/getting-started.md)。
</details>

### 使用 HTTP API

想直接调用服务？同样的自然语言查询也可通过 HTTP 使用。基础地址：`https://data.openecon.ai`。

```bash
curl -X POST https://data.openecon.ai/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "美国失业率"}'
```

还有一个流式端点 —— `POST /api/query/stream` 以服务器发送事件（SSE）返回实时进度。

完整端点列表与请求/响应结构见 [API 参考](docs/reference/api.md)。

## 工作原理

```
  "对比美国和            ┌──────────────┐        ┌────────────────┐
   日本的通胀"     ───▶ │  LLM 解析器  │  ───▶  │  LLM 路由器    │
                        │ （意图、     │        │ （语义路由 +    │
                        │   国家、     │        │   33 万索引）   │
                        │   日期）     │        │                 │
                        └──────────────┘        └───────┬────────┘
                                                        │
                        ┌────────────┐          ┌───────▼────────┐
                        │ 图表 +     │  ◀────── │  从最佳来源    │
                        │ CSV/JSON/  │          │  获取数据      │
                        │ DTA/Python │          │ （FRED、WB、   │
                        └────────────┘          │   IMF …）      │
                                                └────────────────┘
```

1. **解析** —— LLM 从自然语言中提取意图、国家、指标与时间范围
2. **路由** —— 语义路由从 33 万+ 指标中选出最佳数据源与序列
3. **获取** —— 从官方 API 取数，某来源不可用时自动回退
4. **返回** —— 交互式图表，或通过 MCP 向你的智能体返回结构化数据

## 功能特性

**MCP 服务器** —— 一流的 [Model Context Protocol](https://modelcontextprotocol.io) 支持。让 Claude Code、Codex 或任意 MCP 兼容智能体接入经核验的经济数据。

**中文及多语言** —— 用中文、英文、西班牙文、法文等提问。系统会识别语言、找到正确指标，并（对关键信息）以对应语言回复。

**自然语言** —— 无需查 API 文档、无需国家代码、无需序列 ID，描述你想要什么即可。

**33 万指标发现** —— 跨 FRED、World Bank、IMF、Eurostat、BIS 等的全文检索。问"Comtrade 有哪些贸易数据？"即可得到可浏览的清单。

**多轮对话** —— 自然追问：加国家、改时间范围、换指标。上下文跨轮保留，"再加上德国"即刻生效。

**智能路由** —— 系统理解你的意图，而非仅匹配字面。它按查询含义选出正确来源（美国数据用 FRED、全球对比用 World Bank、贸易流用 Comtrade）。

**多国对比** —— 说"G7""金砖""欧盟""东盟""北欧"或列出具体国家，自动展开为全部成员。

**快速** —— 重复查询约 0.1 秒返回。首次查询通常几秒；首个未缓存查询可能更久。

**高可用** —— 某个来源宕机时，系统自动回退到次优来源，无需手动重试。

**歧义澄清** —— 当查询存在多义（"通胀"可指 CPI、PCE 或 GDP 平减指数）时，系统会请你选择，而非贸然猜测。

**多格式导出** —— CSV、JSON、DTA（Stata）与 Python 代码。每次导出都含来源出处。

**Pro 模式** —— AI 生成 Python 进行高级分析：自定义变换、派生指标、定制图表。托管应用向注册用户开放。自托管时默认关闭；仅在具备适当沙箱隔离时设 `PROMODE_ENABLED=true`，因为它会执行生成的代码。

**流式输出** —— 通过服务器发送事件（SSE）实时反馈进度。

**账户与鉴权** —— 前 20 次查询无需注册。用邮箱+密码或 Google 登录以保存历史并解锁 Pro 模式。支持邮箱验证、密码重置与基于 JWT 的会话。鉴权由 Supabase 提供（开发环境有本地 mock 鉴权回退）。

**可自托管** —— 采用 AGPL-3.0 许可。实现一个基类即可接入新数据源。

## 数据来源

10 个数据源，33 万+ 已索引指标：

| 数据源 | 覆盖范围 | 指标数 | API 密钥 |
|--------|----------|--------|----------|
| **FRED** | 美国宏观数据（GDP、CPI、就业、利率） | 90,000+ 序列 | 免费 |
| **World Bank** | 全球发展（200+ 国家、贫困、健康） | 16,000+ 指标 | 无需 |
| **IMF** | 国际收支、汇率、财政数据 | 大量 | 无需 |
| **Eurostat** | 欧盟成员国（HICP、劳动力、贸易） | 大量 | 无需 |
| **UN Comtrade** | 按 HS 商品编码的双边贸易流 | 全部 HS 编码 | 免费 |
| **BIS** | 信贷/GDP、房价、债务证券 | 精选 | 无需 |
| **Statistics Canada** | 加拿大经济表（劳动力、贸易、价格） | 40,000+ 表 | 无需 |
| **OECD** | 经合组织成员国统计 | 大量 | 无需 |
| **ExchangeRate-API** | 160+ 货币对，实时与历史 | 实时与历史 | 免费 |
| **CoinGecko** | 加密货币价格与市场数据 | 10,000+ 币种 | 免费 |

## 适合谁用

| 角色 | 使用方式 |
|------|----------|
| **AI 智能体开发者** | 为任意 MCP 兼容智能体增加经济数据能力 —— 用经核验的数据，而非幻觉 |
| **经济学者与研究人员** | 无需编写 API 代码即可快速取数写论文 |
| **政策分析师** | 一句话完成跨国对比（G7、金砖、欧盟） |
| **学生** | 在探索中学习 —— 提问、看数据、导出用于作业 |
| **记者** | 数秒内用官方来源核查经济说法 |

## 架构

```
┌─────────────────┐     ┌──────────────────┐     ┌──────────────────────────┐
│  用户 / 智能体  │────▶│  FastAPI 后端     │────▶│  数据源                  │
│                 │     │                  │     │                          │
│  "美国通胀"     │     │  LLM 解析器      │     │  FRED · World Bank · IMF │
│                 │◀────│  LLM 路由器      │◀────│  Eurostat · BIS · …      │
│  图表 + 数据    │     │  33 万索引       │     │                          │
└─────────────────┘     └──────────────────┘     └──────────────────────────┘
        │                        │
   React 前端               MCP 端点
   (Vite + Recharts)       (SSE 传输)
```

**技术栈：** Python · FastAPI · React · TypeScript · Vite · Recharts · Redis · OpenRouter

## 参与贡献

欢迎贡献！详见[开发者与贡献者指南](docs/development/DEVELOPER_CONTRIBUTOR_GUIDE.md)。

- [问题列表](https://github.com/hanlulong/openecon-data/issues) —— 缺陷报告与功能建议
- [文档](docs/README.md) —— 完整文档索引
- [安全策略](.github/SECURITY.md) —— 负责任的漏洞披露

如果它对你有帮助，点一个 star 能帮助更多人发现本项目。

## 许可证

[AGPL-3.0](LICENSE) —— 可自由使用、修改与自托管。若你以服务形式运行修改后的版本，须公开你的修改。商业授权请[联系我们](mailto:security@openecon.ai)。
