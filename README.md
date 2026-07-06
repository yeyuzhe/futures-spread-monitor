# futures-spread-monitor

可转抛价差监控页面。页面部署到 GitHub Pages，行情数据由 GitHub Actions 定时调用 rqdata 生成静态 JSON。

## GitHub Pages 发布

1. 进入仓库 `Settings -> Secrets and variables -> Actions`
2. 添加仓库 Secret：
   - `RQDATA_API_KEY`
3. 进入仓库 `Settings -> Pages`
4. `Source` 选择 `GitHub Actions`
5. 进入 `Actions`，手动运行 `Deploy Monitor To GitHub Pages`

发布成功后访问：

`https://yeyuzhe.github.io/futures-spread-monitor/`

## 本地运行

```powershell
python monitor_server.py --host 127.0.0.1 --port 8890
```

然后打开：

`http://127.0.0.1:8890/`

## 静态导出测试

```powershell
python monitor_server.py --export-static --source excel --funding-rate 0.03
```

生成目录：

`dist/`

## 安全提醒

不要提交 `rqdata_config.local.json`、`.env`、`data/`、`dist/`。这些已经写入 `.gitignore`。
