# 数据洞察分析工作台

面向多源数据接入、交互式查询分析、可视化看板搭建、分析报告生成与数据服务编排的数据洞察分析工作台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   ├── app/store.py          数据仓库：内存表 + JSON 落盘 + 事务
│   ├── app/seed.py           首次启动的示例数据
│   └── tests/                训练定义 / 评估留存 / 冲突裁决测试
├── .gitignore
└── docker-compose.yml
```

## 运行期数据

后端默认把数据快照写到 `backend/data/store.json`（已被 gitignore 忽略）。进程重启后
从这个文件恢复，不会重新用示例数据覆盖。可以用 `APP_DATA_FILE` 环境变量指定其他路径。

`backend/data/store.json` 保存了全部业务表、数据集版本快照、训练轮次与评估记录；
写入采用临时文件 + `os.replace`，一次事务只提交一份完整快照。

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

测试：`cd backend && .venv/bin/python -m pytest tests`（需要 `pytest`）

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 交互查询 | `query` | 查询语句 | 语句编号、查询表达式、目标数据集 |
| 可视化图表 | `chart` | 图表配置 | 图表编号、图表名称、图表类型 |
| 数据源接入 | `source` | 接入配置 | 配置编号、连接类型、接入地址 |
| 聚合计算 | `aggregate` | 聚合方案 | 方案编号、聚合函数、目标字段 |
| 数据筛选 | `filter` | 筛选规则 | 规则编号、目标数据集、筛选字段 |
| 报告生成 | `reportgen` | 报告模板 | 模板编号、模板名称、包含图表 |
| 异常检测 | `anomaly` | 检测规则 | 规则编号、监控指标、基线均值 |
| 多维下钻 | `drill` | 下钻路径 | 路径编号、起点维度、下钻层数 |
| 对比分析 | `compare` | 对比方案 | 方案编号、基准数据集、对照数据集 |
| 分析流水线 | `pipeline` | 流水线节点 | 节点编号、节点类型、输入源 |
| 数据集管理 | `datastore` | 数据集定义 | 数据集编号、数据集名称、来源表名 |
| 趋势预测 | `forecast` | 预测模型 | 模型编号、模型类型、训练数据集 |
| 数据分片 | `segment` | 分片规则 | 规则编号、目标数据集、分片字段 |
| 调度管理 | `scheduler` | 调度配置 | 配置编号、关联任务、调度表达式 |
| 通知分发 | `notify` | 通知规则 | 规则编号、触发条件、通知渠道 |
| 查询缓存 | `cache` | 缓存策略 | 策略编号、目标查询、缓存键规则 |
| 数据标签 | `tag` | 标签定义 | 标签编号、标签名称、计算表达式 |
| 数据分享 | `share` | 分享链接 | 链接编号、目标数据集、访问密码 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 趋势预测与数据集版本的联动

趋势预测的「提交训练 / 保存评估并生效」在服务层把下面几件事绑在一起：

1. **训练定义固定**：每次提交训练创建一个独立轮次，冻结训练数据集
   （`dataset_id + version_no + 定义快照`）、目标字段、特征字段、预测步长、置信区间。
   之后跨模块改名或改字段只在数据集管理里追加新版本，不会倒改旧轮次和旧评估。
2. **评估留存**：评估记录挂在轮次上，模型明细同时返回生效轮次与生效评估，
   重启后仍能回答「这条评估来自哪一版训练、用哪一版数据集」。
3. **评估 + 生效原子化**：保存评估与推进模型生效在同一个 `store.transaction()` 里，
   落库失败整体回滚，模型保持原来的生效版本，评估也不会留下半条记录。
4. **冲突裁决**：两个轮次同时完成时，序号最大的已完成轮次是「有效轮次」；
   迟到的低序号结果不能顶掉用户已经确认的新结果，被拒绝的轮次会留下裁决说明。
5. **存量迁移**：存量模型缺项标成 `未确认`，没有证据的「已生效」降回「待训练」，
   不补样例文案、不伪造轮次和评估；补全定义并重新训练后才能再次生效。

接口一览：

| 方法与路径 | 用途 |
| --- | --- |
| `POST /api/forecast` | 登记预测模型（写训练定义草稿） |
| `PATCH /api/forecast/{id}` | 修改训练定义，不影响历史轮次 |
| `POST /api/forecast/{id}/rounds` | 提交训练，创建并冻结独立轮次 |
| `POST /api/forecast/{id}/rounds/{轮次}/complete` | 标记训练完成 |
| `POST /api/forecast/{id}/evaluations` | 保存评估并推进生效（一个事务） |
| `GET /api/forecast/{id}` | 明细：定义、轮次、评估、生效归属 |
| `GET /api/forecast/{id}/rounds` | 轮次列表与当前有效轮次 |
| `PATCH /api/datastore/{id}` | 修改数据集定义，追加新版本 |
| `GET /api/datastore/{id}/versions` | 数据集历史版本快照 |

未接入外部机器学习服务：轮次只承载训练定义与本地评估录入，训练完成由操作人确认。

领域：数据工程
子方向：数据分析与数据应用
