# 后端裁决测试

三个脚本不依赖 pytest，直接用 venv 里的 Python 运行，但需要一个可连的 PostgreSQL，
地址由环境变量 `DATABASE_URL` 指定（`support.py` 里有本地默认值）：

```bash
# 建测试库后依次运行
python tests/test_dedup.py        # 数据层：拒绝/覆盖/不符/空时刻/争用/重复列表
python tests/test_http.py         # HTTP 层：登录、权限、409、设置与详情接口
python tests/test_concurrency.py  # 并发：共享门闩、策略固化、设置 busy、时刻争用
```

每个脚本开头会 DROP 并重建本项目的五张表，不要指向生产库。
