# 启动与部署

## 1. 环境准备

- Python：使用 3.12 执行以下命令。
- Node.js：22.13.0 或更高版本，并安装随附的 npm。
- Windows 使用 PowerShell；Linux 以下以 Ubuntu 24.04、Bash 和 systemd 为例。
- 首次安装依赖需要联网。请在目标机器重新安装依赖，不要复制其他系统的 `.venv`、`node_modules` 或构建产物。
- 上传源码时保留 `web/frontend/.openai/hosting.json`、`web/frontend/package-lock.json` 和 `src/oilfield_energy/bootstrap/assets/`，避免漏传隐藏目录及运行数据。

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
& '.\.venv\Scripts\python.exe' -m pip check

Set-Location '.\web\frontend'
npm.cmd ci --include=dev
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

按以下顺序确认启动成功：

1. 健康检查返回 `status: ok`，且 `solvers` 包含 `SCIP`；设备目录接口正常返回数据。
2. 访问 `http://127.0.0.1:3000`，确认页面加载正常、后端服务在线。
3. 访问 `http://127.0.0.1:3000/demo-lab`，确认设备数据持续更新。
4. 在网页中选择“光伏群控策略验证”（`/group-control`），点击“运行群控场景”，确认任务状态更新且结果返回。完整优化仿真可在连通性检查通过后运行。

后端接口文档地址为 `http://127.0.0.1:8000/docs`。减少优化算例的计划点数仍然覆盖 24 小时，不等于只模拟较短时长，也不保证很快完成。

停止时，在前后端各自的窗口按 `Ctrl+C`。以后启动只需重复 2.2 和 2.3；依赖变化时重新执行安装命令。

## 3. Linux 服务器部署

按以下步骤部署为同域访问：网页和 `/api/` 共用服务器 IP 或域名，由 Nginx 转发。前后端分别绑定服务器本机的 3000、8000 端口；客户端访问 Nginx 的 80 或 443 端口。

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
.venv/bin/python -m pip check

cd web/frontend
npm ci --include=dev
```

保留 `--include=dev`，构建所需的 Vite 和插件也在开发依赖中。运行账户需要能够写入项目下的 `artifacts/` 和 `results/`；默认设备记录位于 `artifacts/system-demo-live/`，网页任务结果位于 `results/web_sessions/`。

### 3.2 配置 Linux 自托管构建

在**服务器部署副本**中切换为 [vinext 的 Node.js standalone 构建](https://github.com/cloudflare/vinext#cli-reference)。本节使用 `node dist/standalone/server.js` 启动；仓库的 `npm start` 执行 Wrangler 本地预览，不用于本节部署。

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

确认构建成功并生成 `dist/standalone/server.js` 后，再配置后台服务；构建失败时不要启动旧产物。

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

使用 `oilfield-web` 启动，并只运行一个后端实例。不要替换为 `uvicorn oilfield_energy.web_api:app`，该方式不会装配设备模拟服务；不要为后端增加多个 worker。

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
前端绑定地址使用 `HOST`，不要替换成 `HOSTNAME`。

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

符号链接只需创建一次。若启用了主机防火墙，允许局域网客户端访问 TCP 80；后端 8000 和前端 3000 保持仅本机可访问。在另一台电脑检查：

```bash
curl --fail http://192.168.1.100/api/health
curl --fail http://192.168.1.100/api/demo/catalog
```

访问 `http://192.168.1.100` 和 `http://192.168.1.100/demo-lab`，按 2.4 检查页面、设备数据和任务结果。需要检查实时推送时，在浏览器开发者工具的网络面板查看 `/api/simulations/.../stream`，确认 WebSocket 握手返回 `101` 并持续收到状态。

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

打开 `https://energy.example.com`，按 2.4 检查页面、设备数据和任务结果，并确认 WebSocket 使用 `wss://` 地址且握手成功。

### 3.6 更新、日志与停止

更新前等待运行中的仿真结束，然后停止服务：

```bash
sudo systemctl stop cnpc-frontend cnpc-api
```

服务停止后，备份需要保留的 `artifacts/`、`results/`、服务器构建配置及 Nginx 配置。

更新源码后，保留或重新应用 3.2 的自托管配置，再执行：

```bash
cd /srv/cnpcproj
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip check

cd web/frontend
npm ci --include=dev
export NEXT_PUBLIC_API_BASE_URL='https://energy.example.com'
npm run build
test -f dist/standalone/server.js
```

确认安装及构建成功后，启动服务：

```bash
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

## 4. 命令行启动（可选）

命令行计算只需安装 Python 依赖，无需启动 Web 服务或安装前端依赖。以下命令从项目根目录执行，按需要选择。

### 4.1 Windows PowerShell

```powershell
# 查看全部命令和参数
& '.\.venv\Scripts\python.exe' run_model.py --help

# 默认完整运行，包含单微电网和集群计算，全天 96 个计划点
& '.\.venv\Scripts\python.exe' run_model.py --steps 96 --output results/run-001

# 多层级滚动控制（每次使用新的输出目录）
& '.\.venv\Scripts\python.exe' run_model.py cluster --output results/cluster-001

# 群调群控场景
& '.\.venv\Scripts\python.exe' run_model.py group-scenarios --output results/group_control_scenarios
```

### 4.2 Linux Bash

```bash
.venv/bin/python run_model.py --help
.venv/bin/python run_model.py --steps 96 --output results/run-001
.venv/bin/python run_model.py cluster --output results/cluster-001
.venv/bin/python run_model.py group-scenarios --output results/group_control_scenarios
```

`oilfield-opt` 与 `run_model.py` 使用相同入口。计算完成后，以终端显示的结果路径为准。完整模拟耗时较长，保持进程运行，勿将启动成功当作计算完成。

重新执行默认完整运行或集群运行时，更换 `--output`，不要复用已存在的研究输出目录。其他输入选项通过 `--help` 查看。
