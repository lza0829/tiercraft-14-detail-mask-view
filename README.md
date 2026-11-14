# 05-tiercraft（筑层）

筑层 — 数仓分层建设台：贴源入仓 → 明细清洗标准化 → 汇总落表 → 层间行数/键对账。

**不是** 多源 Feed 归并，**不是** 作业依赖编排，**不是** CDC 变更流按序落库，**不是** 质检隔离签发。
**也不是** 报表看板 / CRUD 后台 / 电商订单前台 / OA 工单。

## 启动

```bash
docker compose up --build
```

本地开发：

```bash
# 后端
cd backend
pip install -r requirements.txt
set DATA_DIR=..\data
uvicorn app.main:app --reload --port 10500

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5500 |
| 接口 | http://localhost:10500 |

## 主链

登记主题域 → 贴源层装载夹具 → 明细层清洗标准化 → 汇总层聚合 → 层间对账报告与各层列表数字同源

## 界面约定

页面文案全部中文；分层称「贴源层 / 明细层 / 汇总层」，界面不出现英文缩写站名。
