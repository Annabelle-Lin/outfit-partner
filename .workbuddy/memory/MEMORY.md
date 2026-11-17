# wardrobe-agent 项目长期约定

## 前端形态（外包/改动前必读）
- 前端只有 `frontend/index.html` 一个文件：CSS 内联 29,338 字符、JS 内联 112,251 字符，无 package.json / vite / tailwind。
- 后端 `app/main.py` 用 `StaticFiles(directory=FRONTEND_DIR, html=True)` 挂载 `frontend/`，目录结构和 `index.html` 入口不能动。
- 契约基线：`docs/frontend-contract/index-anchors.json`（8 view / 111 id / 83 JS id / 37 onclick 函数 99 处 / 141 JS 函数 / 16 API 路径 27 处 / 3 localStorage 键 / 28 CSS 变量）。校验脚本 `scripts/check_frontend_contract.py`，退出码 0 可合并。
- **校验脚本盲区**（已实测）：onclick 参数被改、重复 id、JS 依赖的 10 个 class 被改名，脚本都放行，必须人工查。10 个 class：chip / msg-bubble / nav-item / opt-chip / outfit-note / rate-btn / rate-extra / rate-extra-input / seg-option / weather-stats。
- CSS 设计令牌只改值不改名（28 个变量）；localStorage 三键 `wardrobe_token` / `wardrobe_uid` / `wardrobe_chat_sid` 不能动。

## 测试环境
- fastapi 装在 `C:/Python311/python.exe`（3.11）；WorkBuddy 内置 python3 是 3.13，没有 fastapi。
- 跑 `scripts/phase1_test.py` 必须 `PYTHONPATH=. C:/Python311/python.exe scripts/phase1_test.py`，19 项全绿。
- `smoke_test.py` 不存在（截至 2026-11-16），清单里"27 项验收"是错误引用。

## 目录结构（2026-11-16 整理后）
- 根目录只留：`app/`、`data/`、`docs/`、`frontend/`、`rules/`、`scripts/`、`衣橱示例数据/`、README.md、requirements.txt、seed_demo.py、.env。
- 文档归档：`docs/项目文档/`（API_CONTRACT.md、启动操作.md）、`docs/外包交付/`（美化说明书、验收清单）、`docs/frontend-contract/`（契约基线）、`docs/design-submission/`（28 份课程文档，内部互相引用，别拆）。
- **代码路径写死不能挪**：`app/`（尤其 config.py 的 BASE_DIR）、`rules/` 四个 JSON、`data/`（含 uploads）、`frontend/`、`衣橱示例数据/`、`docs/frontend-contract/index-anchors.json`。挪了服务起不来或断图。
- `data/s3_kb/` 子目录名可改（kb.py 只 rglob `data/` 下所有 .md），但必须在 `data/` 内。

## 外包文档
- `docs/外包交付/前端外包交付与验收清单.md`：Vivian 自己用，已标注所有错误与正确命令。
- `docs/外包交付/给外包方的前端美化说明.md`：可直接转发给外包方。
- 已发出的 `前端外包交付包.zip` 是整理前打包的，包内路径自洽，无需重打。
- 2026-11-16 状态：工作区有 69 行未提交改动（含 `_rateOutfitId`、`_rateSubmit`），HEAD 是 `5f76cb1`；发包前必须先提交，否则基线对不上。
