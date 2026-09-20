# 启动与部署

## 1. 环境准备

- Python：建议使用 3.12，项目声明的最低版本为 3.10。
- Node.js：22.13.0 或更高版本，并安装随附的 npm。
- Windows 使用 PowerShell；Linux 以下以 Ubuntu 24.04、Bash 和 systemd 为例。
- 首次安装依赖需要联网。请在目标机器重新安装依赖，不要复制其他系统的 `.venv`、`node_modules` 或构建产物。

以下 Windows 路径 `C:\path\to\CNPCPROJ`、Linux 路径 `/srv/cnpcproj`、部署账户 `deploy`、IP 和域名均需按实际环境替换。除明确切换目录的步骤外，命令在项目根目录执行。

## 2. Windows 本机启动

### 2.1 首次安装

安装 Python 和 Node.js 后，打开 PowerShell：

```powershell
Set-Location 'C:\path\to\CNPCPROJ'
py -3.12 --version
node --version
npm.cmd --version

py -3.12 -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install --upgrade pip
& '.\.venv\Scripts\python.exe' -m pip install -e .

Set-Location '.\web\frontend'
npm.cmd ci
```

没有 `py` 启动器时，可将 `py -3.12` 替换为已安装的 Python 3.12 命令，例如 `python`。以下步骤直接调用虚拟环境中的程序，无需激活环境或修改 PowerShell 执行策略。

### 2.2 启动后端

在第一个 PowerShell 窗口执行，并保持窗口开启：

```powershell
Set-Location 'C:\path\to\CNPCPROJ'
& '.\.venv\Scripts\oilfield-web.exe'
```

该入口同时启动仿真 API 和设备模拟服务，监听 `127.0.0.1:8000`。默认模拟输入会自动生成，无需先运行命令行算例。

### 2.3 启动前端

另开一个 PowerShell 窗口：

```powershell
Set-Location 'C:\path\to\CNPCPROJ\web\frontend'
$env:NEXT_PUBLIC_API_BASE_URL = 'http://127.0.0.1:8000'
npm.cmd run dev -- --hostname 127.0.0.1 --port 3000
```

浏览器访问 `http://127.0.0.1:3000`。

### 2.4 检查与停止

在第三个 PowerShell 窗口检查后端：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/health'
Invoke-RestMethod 'http://127.0.0.1:8000/api/demo/catalog'
```

健康检查应返回 `status: ok`，`solvers` 应包含 `SCIP`；设备目录接口应正常返回数据。接口文档地址为 `http://127.0.0.1:8000/docs`。进入网页后确认服务在线，再运行一次短时段仿真检查前后端连接。

停止时，在前后端各自的窗口按 `Ctrl+C`。以后启动只需重复 2.2 和 2.3；依赖变化时重新执行安装命令。

## 3. Linux 服务器部署

使用一个访问地址同时提供网页和 `/api/` 接口：Nginx 将网页转发到 `127.0.0.1:3000`，将 API 和 WebSocket 转发到 `127.0.0.1:8000`。局域网使用服务器 IP，公网使用域名和 HTTPS。

### 3.1 安装环境与依赖

准备一个具有 sudo 权限的普通部署账户，以下以 `deploy` 为例。使用该账户登录并安装系统依赖。另行安装满足版本要求的 Node.js，记录其绝对路径供 systemd 使用。

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv nginx curl
python3.12 --version
node --version
npm --version
command -v node

sudo install -d -o deploy -g deploy /srv/cnpcproj
```

将完整源码上传或检出到 `/srv/cnpcproj`，确保部署账户拥有该目录的读写权限，然后安装依赖：

```bash
cd /srv/cnpcproj
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .

cd web/frontend
npm ci
```

构建前不要省略 npm 的开发依赖，Vite 和构建插件也在其中。运行账户需要能够写入项目下的 `artifacts/` 和 `results/`。

### 3.2 配置 Linux 自托管构建

当前默认前端配置面向 Cloudflare Workers，`npm start` 使用 Wrangler 本地预览。Linux 自托管需在**服务器部署副本**中切换为 [vinext 的 Node.js standalone 构建](https://github.com/cloudflare/vinext#cli-reference)。

在 `/srv/cnpcproj/web/frontend` 中先备份配置：

```bash
cp -n vite.config.ts vite.config.ts.bak
cp -n next.config.ts next.config.ts.bak
```

将该副本的 `vite.config.ts` 替换为：

```typescript
import tailwindcss from '@tailwindcss/postcss';
import vinext from 'vinext';
import { defineConfig } from 'vite';

export default defineConfig({
  css: { postcss: { plugins: [tailwindcss()] } },
  plugins: [vinext()],
});
```

将该副本的 `next.config.ts` 替换为：

```typescript
export default { output: 'standalone' };
```

选择用户最终访问的地址，在**构建前**设置 API 地址。以下二选一，IP 或域名需替换为实际值：

```bash
# 局域网：访问 http://192.168.1.100
export NEXT_PUBLIC_API_BASE_URL='http://192.168.1.100'

# 公网：改用最终 HTTPS 域名
# export NEXT_PUBLIC_API_BASE_URL='https://energy.example.com'

npm run build
test -f dist/standalone/server.js
```

地址必须包含 `http://` 或 `https://`，不带末尾 `/`，也不附加 `/api`。它会写入浏览器脚本，不能填写服务器的 `127.0.0.1`，也不能留空或使用相对路径，因为 WebSocket 连接需要完整 URL。换域名、IP、端口或切换 HTTPS 后，都要重新构建。

### 3.3 配置后台服务

创建 `/etc/systemd/system/cnpc-api.service`，写入：

```ini
[Unit]
Description=CNPC simulation API
After=network.target

[Service]
Type=simple
User=deploy
Group=deploy
WorkingDirectory=/srv/cnpcproj
Environment=PYTHONUNBUFFERED=1
Environment=MPLBACKEND=Agg
ExecStart=/srv/cnpcproj/.venv/bin/oilfield-web
Restart=on-failure
RestartSec=5
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

保留 `oilfield-web` 入口和单实例运行方式。直接改成 `uvicorn oilfield_energy.web_api:app` 会缺少设备模拟服务；当前任务管理和模拟状态也未配置为多个进程共享。

创建 `/etc/systemd/system/cnpc-frontend.service`，写入：

```ini
[Unit]
Description=CNPC Web frontend
After=network.target cnpc-api.service

[Service]
Type=simple
User=deploy
Group=deploy
WorkingDirectory=/srv/cnpcproj/web/frontend
Environment=NODE_ENV=production
Environment=HOST=127.0.0.1
Environment=PORT=3000
ExecStart=/usr/bin/node /srv/cnpcproj/web/frontend/dist/standalone/server.js
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

将 `/usr/bin/node` 换成 3.1 中 `command -v node` 返回的实际路径。systemd 不会自动加载交互式 Shell 中的 Node.js 版本管理器。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cnpc-api cnpc-frontend
sudo systemctl status cnpc-api cnpc-frontend --no-pager
curl --fail http://127.0.0.1:8000/api/health
curl --fail http://127.0.0.1:8000/api/demo/catalog
curl --fail -I http://127.0.0.1:3000
```

### 3.4 配置局域网访问

创建 `/etc/nginx/sites-available/cnpcproj`，将 `server_name` 换成服务器 IP；公网部署则填写实际域名。

```nginx
map $http_upgrade $cnpc_connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    listen [::]:80;
    server_name 192.168.1.100;

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $cnpc_connection_upgrade;
        proxy_read_timeout 3600s;
        proxy_buffering off;
    }

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`/api/` 的 `proxy_pass` 后不添加 `/`，以保留接口路径；`Upgrade` 和 `Connection` 用于 [Nginx WebSocket 转发](https://nginx.org/en/docs/http/websocket.html)。网页与接口同域访问，无需扩大后端跨域白名单。

```bash
sudo ln -s /etc/nginx/sites-available/cnpcproj /etc/nginx/sites-enabled/cnpcproj
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
```

符号链接只需创建一次。若启用了主机防火墙，允许局域网客户端访问 TCP 80；后端 8000 和前端 3000 保持仅本机可访问。在另一台电脑访问 `http://192.168.1.100`，确认健康检查、设备目录和网页仿真均可用。

### 3.5 配置公网域名与 HTTPS

1. 将域名解析到服务器公网 IP；若配置 AAAA 记录，IPv6 也必须可达。需要路由器端口映射时，将 TCP 80、443 转发到服务器。
2. 将 Nginx 的 `server_name` 改为实际域名，执行 `sudo nginx -t` 和 `sudo systemctl reload nginx`。确认云安全组和主机防火墙允许 TCP 80、443。
3. 按 3.2 将 API 地址设为最终的 `https://` 域名，重新构建后执行 `sudo systemctl restart cnpc-frontend`。
4. 配置访问认证和证书，再通过最终域名验证访问。

当前服务没有内置登录认证。公网入口可在 Nginx 增加 HTTP Basic 认证，保护网页和全部 API：

```bash
sudo apt install -y apache2-utils certbot python3-certbot-nginx
sudo htpasswd -c /etc/nginx/cnpcproj.htpasswd operator
sudo chown root:www-data /etc/nginx/cnpcproj.htpasswd
sudo chmod 640 /etc/nginx/cnpcproj.htpasswd
```

密码由终端提示输入；以后新增账户时去掉 `-c`，避免覆盖原文件。在 Nginx 的 `server` 块中、两个 `location` 块之外增加：

```nginx
auth_basic "CNPC";
auth_basic_user_file /etc/nginx/cnpcproj.htpasswd;
```

申请证书并启用 HTTP 跳转 HTTPS：

```bash
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d energy.example.com --redirect
sudo certbot renew --dry-run
```

按提示完成证书申请，确认 Certbot 的续期定时任务已启用。也可使用已有证书；[Certbot 的 Nginx 配置说明](https://certbot.eff.org/instructions?ws=nginx&os=pip)提供了申请和续期检查步骤。启用 HTTPS 后再在浏览器输入登录凭据。

在客户端验证，`curl` 会提示输入密码：

```bash
curl --fail -u operator https://energy.example.com/api/health
curl --fail -u operator https://energy.example.com/api/demo/catalog
```

打开 `https://energy.example.com`，运行一次短时段仿真，确认结果返回、任务状态更新和设备模拟页面正常。

### 3.6 更新、日志与停止

更新前等待运行中的仿真结束，备份需要保留的 `artifacts/`、`results/`、服务器构建配置及 Nginx 配置，然后停止服务：

```bash
sudo systemctl stop cnpc-frontend cnpc-api
```

更新源码后，保留或重新应用 3.2 的自托管配置，再执行：

```bash
cd /srv/cnpcproj
.venv/bin/python -m pip install -e .

cd web/frontend
npm ci
export NEXT_PUBLIC_API_BASE_URL='https://energy.example.com'
npm run build
sudo systemctl start cnpc-api cnpc-frontend
```

局域网部署应将 API 地址换回实际的 `http://服务器IP`。启动成功后重新执行健康检查和网页验证。

```bash
# 查看日志；Ctrl+C 退出日志跟踪，不会停止服务
sudo journalctl -u cnpc-api -u cnpc-frontend -n 100 -f

# 停止应用服务
sudo systemctl stop cnpc-frontend cnpc-api

# 如需取消开机自启
sudo systemctl disable cnpc-frontend cnpc-api
```

## 4. 命令行运行

命令行计算只需安装 Python 依赖，无需启动 Web 服务或安装前端依赖。以下命令从项目根目录执行，按需要选择。

### 4.1 Windows PowerShell

```powershell
# 查看全部命令和参数
& '.\.venv\Scripts\python.exe' run_model.py --help

# 默认运行，96 个时段
& '.\.venv\Scripts\python.exe' run_model.py

# 24 时段小算例
& '.\.venv\Scripts\python.exe' run_model.py --steps 24 --output results/quick

# 多层级控制
& '.\.venv\Scripts\python.exe' run_model.py hierarchical --output results/hierarchical

# 群调群控场景
& '.\.venv\Scripts\python.exe' run_model.py group-scenarios --output results/group_control_scenarios

# 生成并运行山城完整模拟；每次使用新的输入、输出目录
& '.\.venv\Scripts\oilfield-sim.exe' generate --output artifacts/shancheng-inputs-001
& '.\.venv\Scripts\oilfield-sim.exe' run --manifest artifacts/shancheng-inputs-001/manifest.json --output artifacts/shancheng-run-001
```

### 4.2 Linux Bash

```bash
.venv/bin/python run_model.py --help
.venv/bin/python run_model.py
.venv/bin/python run_model.py --steps 24 --output results/quick
.venv/bin/python run_model.py hierarchical --output results/hierarchical
.venv/bin/python run_model.py group-scenarios --output results/group_control_scenarios

.venv/bin/oilfield-sim generate --output artifacts/shancheng-inputs-001
.venv/bin/oilfield-sim run --manifest artifacts/shancheng-inputs-001/manifest.json --output artifacts/shancheng-run-001
```

`oilfield-opt` 与 `run_model.py` 使用相同入口。计算完成后，以终端显示的结果路径为准。完整模拟耗时较长，保持进程运行，勿将启动成功当作计算完成。

### 4.3 使用自备输入运行

审计 Excel 或执行固定设备潮流对照时，需要先准备对应的输入文件。以下为 PowerShell 示例，替换路径后执行：

```powershell
& '.\.venv\Scripts\python.exe' run_model.py field-audit `
  --short-circuit-workbook 'C:\data\母线容量、阻抗.xlsx' `
  --line-load-workbook 'C:\data\线路负荷统计0901.xlsx' `
  --output results/field_data

& '.\.venv\Scripts\python.exe' run_model.py compare-power-flow `
  --input 'C:\data\fixed_device_comparison.json' `
  --output results/fixed_device_comparison
```

Linux 使用 `.venv/bin/python`，将输入路径替换为服务器路径；多行命令使用反斜杠 `\` 续行，也可将整条命令写成一行。

## 5. 启动与部署故障处理

| 现象 | 处理步骤 |
| --- | --- |
| 找不到 `oilfield-web`、`oilfield-sim` 或 Python 模块 | 回到项目根目录，使用目标虚拟环境的 Python 重新执行 `-m pip install -e .`。 |
| 健康检查没有 `SCIP` | 使用同一虚拟环境执行 `python -m pip show pyscipopt`，检查安装错误，再重新安装项目依赖。 |
| 本机正常，远程网页显示服务离线 | 检查构建时的 API 地址是否为客户端可访问的完整地址；修正后重新构建并重启前端。 |
| HTTPS 页面请求失败 | API 地址也必须使用 `https://`，并与访问域名一致；重新构建后刷新页面。 |
| 设备接口返回 404 或 503 | 检查是否使用当前虚拟环境的 `oilfield-web` 启动，排除旧服务占用 8000，并查看后端日志。 |
| 任务状态无法实时更新 | 检查 Nginx WebSocket 转发配置，以及连接地址是否为正确的 `ws://` 或 `wss://`。 |
| Nginx 返回 502 | 检查两个 systemd 服务状态，分别请求服务器本机的 3000、8000 端口，再查看日志。 |
| `npm start` 无法运行独立构建 | Linux 自托管使用 `node dist/standalone/server.js` 或上述 systemd 服务。仓库现有 `npm start` 指向 Wrangler。 |
| 修改配置后仍访问旧地址 | 修改 API 地址需重新构建；修改 systemd 单元需执行 `daemon-reload` 并重启；修改 Nginx 配置需检查后 reload。 |
