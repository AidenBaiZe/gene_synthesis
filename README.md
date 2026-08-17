# 合成日志上传工具（一期）

## 初始化数据库

先创建 PostgreSQL 数据库，再从仓库根目录执行：

```powershell
psql -U postgres -d gene_synthesis -f database/log_execution_schema.sql
```

## 启动

```powershell
python -m pip install -r code/upload_tool/requirements.txt
python code/upload_tool/app.py
```

浏览器打开 <http://127.0.0.1:5050>。

## API

- `POST /api/logs`：multipart 字段 `file`；可选 `run_id` 或 `new_run=true`
- `GET /api/runs?limit=50&cursor=<run_id>`：运行列表
- `GET /api/runs/{run_id}/versions`：日志版本
- `GET /api/log-versions/{version_id}`：解析层级和问题记录
- `GET /api/source-files/{file_id}/content`：查看原文
- `GET /api/source-files/{file_id}/download`：下载原文件

## SQL 差异查询

```sql
SELECT *
FROM synth_log.compare_log_versions(左版本ID, 右版本ID)
ORDER BY cycle_no, reagent_code, step_occurrence,
         event_no NULLS FIRST, segment_no NULLS FIRST, field_name;
```

## 测试

```powershell
python -m pytest code/upload_tool/tests -q
```
