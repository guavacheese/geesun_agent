# 内网 PyPI 镜像（devpi）+ 沙箱模板镜像改造 —— 落地操作手册

> 目标：解决沙箱内 `pip install` 失败（根因：13 出口被上网认证网关 AC Portal 管控，http 被 302 认证页、443 被重签伪证书）。  
> 方案：内网 devpi 按需缓存镜像（136 部署，走 136 干净出口回源）+ 沙箱镜像预装常用库兜底。  
> 机器速查：136 = 192.168.10.136（构建/代理机，已登录 Harbor）；13 = 172.16.66.13（CubeSandbox 服务端）；Harbor = 172.16.220.74:8333。

---

## 0. 架构与链路

```
沙箱 pip ──内网──> devpi @136:3141 ──回源──> mirrors.aliyun.com/pypi/simple
    index-url = http://192.168.10.136:3141/root/pypi/+simple/     （136 出口干净已放行）
         │
         └─ 命中缓存：内网直取，零外网流量，完全不碰 AC 认证网关
```

前置条件（已实测确认）：136 与 13 内网互通（ping 0.3ms）；136 出口干净（pypi/https 均 200）；136 Docker 29.4.1 + 已登录 Harbor；Harbor `cubesandbox` 项目存在。

---

## 1. 前置检查清单（每台机器按序执行）

### 1.1 确认 13 能访问 136 的 3141 端口（待 devpi 起来后做，见 2.4）

### 1.2 确认 136 有 Python/pip（用于装 devpi-client，可选）

```bash
# 在 136 上
python3 --version && pip3 --version
# 若没有 pip3：dnf install -y python3-pip
```

---

## 2. 阶段一：136 部署 devpi（约 5 分钟）

> **注：不用 docker 镜像**（实测 `docker pull devpi/devpi` 失败：daocloud 403 / rat.dev / 1ms 均无此冷门镜像）。改为 **Python venv 直接装 devpi-server**，效果等同且更轻。

### 2.1 创建 venv 并安装 devpi-server

```bash
# 在 136 上
mkdir -p /home/data/devpi /opt/devpi-venv
python3 -m venv /opt/devpi-venv
/opt/devpi-venv/bin/pip install -U pip
/opt/devpi-venv/bin/pip install -U devpi-server devpi-web waitress
```

预期：安装成功，无报错（136 出口干净，从 pypi.org 拉包正常）。

> 异常：pip 下载慢/失败 → 重试；或告诉我换国内源装。

### 2.2 初始化并启动 devpi

```bash
# 在 136 上
/opt/devpi-venv/bin/devpi-init --serverdir /home/data/devpi
nohup /opt/devpi-venv/bin/devpi-server \
  --host 0.0.0.0 --port 3141 --serverdir /home/data/devpi \
  --threads 10 > /var/log/devpi.log 2>&1 &
sleep 5
curl -s http://127.0.0.1:3141/ | head -5
tail -5 /var/log/devpi.log
```

预期：curl 返回 HTML（devpi 页面）；日志出现 `serving on http://0.0.0.0:3141`。

> 异常：日志报错贴回来。

### 2.2b （推荐）注册为 systemd 服务，开机自启

```bash
# 在 136 上
cat > /etc/systemd/system/devpi.service <<'EOF'
[Unit]
Description=devpi PyPI server
After=network.target

[Service]
ExecStart=/opt/devpi-venv/bin/devpi-server --host 0.0.0.0 --port 3141 --serverdir /home/data/devpi --threads 10
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
# 若 2.2 已手动起了一个进程，先杀掉：pkill -f devpi-server
systemctl enable --now devpi
systemctl status devpi --no-pager | head -8
```

预期：active (running)。

### 2.3 （可选，加速）把回源指向阿里源

默认 root/pypi 已 mirror 到 pypi.org（136 访问正常，只是慢），**可先跳过本步直接用**。要加速则：

```bash
# 在 136 上
/opt/devpi-venv/bin/pip install -U devpi-client
/opt/devpi-venv/bin/devpi use http://127.0.0.1:3141/root/pypi
# 若提示认证：devpi login root --password=   （回车空密码）
/opt/devpi-venv/bin/devpi index -y root/pypi mirror_url=https://mirrors.aliyun.com/pypi/simple/ bases=
/opt/devpi-venv/bin/devpi index root/pypi   # 查看生效配置
```

预期：`mirror_url` 变为阿里源。

> 异常：devpi 镜像默认 root 密码若不是空，看容器日志或环境变量；此步失败不影响后续（回源 pypi.org 也能用）。

### 2.4 放行 3141（136 的 firewalld）并验证 13 → 136:3141

```bash
# 在 136 上（firewalld 默认只放行 ssh/3000/13000 等，需加 3141）
firewall-cmd --permanent --add-port=3141/tcp
firewall-cmd --reload

# 在 13 上验证
nc -zvw3 192.168.10.136 3141
```

预期：136 返回 success；13 返回 `Connected to 192.168.10.136:3141`。

> 不通：检查 136 `firewall-cmd --list-ports` 是否含 3141；再检查两机路由/ACL，贴错误回来。

---

## 3. 阶段二：136 本机验证 devpi 可用（约 1 分钟）

```bash
# 在 136 上
pip3 download --no-deps -d /tmp/devpi_test PyMuPDF \
  -i http://127.0.0.1:3141/root/pypi/+simple/ \
  --trusted-host 127.0.0.1
ls -lh /tmp/devpi_test/
```

预期：下载到 PyMuPDF wheel（首次会回源阿里/pypi 拉取，稍慢）。

> 异常：报 SSL/连接错 → 贴完整输出。devpi 首次回源若偶发失败，重试一次（缓存会逐步命中）。

---

## 4. 阶段三：现有沙箱直连验证（不重建镜像，先打通链路）

### 4.1 起一个测试沙箱（用现有模板即可）并进入

```bash
# 在 13 上，用 cubecli 或 agent 会话创建沙箱，拿到 sandbox_id
cubecli c exec -i -t <sandbox_id> /bin/bash
```

### 4.2 沙箱内配置并安装测试

```bash
# 在沙箱内
pip config set global.index-url http://192.168.10.136:3141/root/pypi/+simple/
pip config set global.trusted-host 192.168.10.136
pip install --no-cache-dir PyMuPDF
python3 -c "import fitz; print('fitz OK', fitz.__doc__[:20])"
```

预期：安装成功，`fitz OK`。

> 这一步通过 = 链路全通，进入阶段四；失败贴完整输出。

---

## 5. 阶段四：inspect 现有模板镜像（决定 Dockerfile 细节，约 1 分钟）

```bash
# 在 136 上
docker pull 172.16.220.74:8333/cubesandbox/sandbox-code:latest
docker run --rm 172.16.220.74:8333/cubesandbox/sandbox-code:latest \
  sh -c 'cat /etc/os-release | head -3; python3 --version; which pip; pip list 2>/dev/null | head -30; which apt-get dnf yum 2>/dev/null'
```

预期：拿到 ① 基础 OS 类型 ② Python 版本 ③ 已有包清单 ④ 包管理器。

> 结果贴给我，我据此定稿 Dockerfile（第 6 节给出的是通用模板，含 apt 分支，若模板是 RHEL/alpine 需调整）。

---

## 6. 阶段五：构建新模板镜像并推送 Harbor（约 10~30 分钟，视预装包量）

### 6.1 在 136 上创建构建目录和 Dockerfile

```bash
mkdir -p /home/data/cube-img && cd /home/data/cube-img
```

`Dockerfile` 内容（**待阶段四 inspect 结果确认后定稿**，以下为基准版，适用 debian/ubuntu 系）：

```dockerfile
FROM 172.16.220.74:8333/cubesandbox/sandbox-code:latest

# pip 指向内网 devpi
RUN mkdir -p /etc/pip && \
    printf '[global]\nindex-url = http://192.168.10.136:3141/root/pypi/+simple/\ntrusted-host = 192.168.10.136\ntimeout = 60\n' > /etc/pip/pip.conf

# 预装常用库（高频 PDF/Office/数据处理/图片/工具）
RUN pip install --no-cache-dir \
    PyMuPDF pdfplumber pypdf pdfminer.six pdf2image \
    python-docx openpyxl \
    numpy pandas requests httpx \
    Pillow \
    beautifulsoup4 lxml tqdm

# 系统依赖：pdftotext 命令 + 中文字体（PDF 渲染）
RUN apt-get update && apt-get install -y poppler-utils fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
```

### 6.2 构建并推送

```bash
# 在 136 上
cd /home/data/cube-img
docker build -t 172.16.220.74:8333/cubesandbox/sandbox-code:agent-pipfix .
docker push 172.16.220.74:8333/cubesandbox/sandbox-code:agent-pipfix
```

预期：构建各层成功（预装包走 devpi 拉取），push 完成。

> 异常：① 构建时 pip 报错 → 检查 devpi 日志；② 构建耗时正常（下载依赖）；③ push 失败 → 确认 docker login 状态（136 已登录）。

---

## 7. 阶段六：13 上创建新模板（约 5~15 分钟，rootfs 构建）

```bash
# 在 13 上
cubemastercli tpl create-from-image \
  --name dev-pipfix \
  --image 172.16.220.74:8333/cubesandbox/sandbox-code:agent-pipfix
# 记录输出中的 JOB_ID
cubemastercli tpl list
```

预期：新模板出现，STATUS 从创建中变 `READY`（`tpl watch <JOB_ID>` 可跟进度）。

> 异常：STATUS 非 READY → `cubemastercli tpl status <JOB_ID>` 看失败原因贴回来（常见：镜像拉取失败/rootfs 构建失败）。

---

## 8. 阶段七：新模板沙箱终验（约 2 分钟）

### 8.1 用新模板创建沙箱（agent 会话选 dev-pipfix 模板，或手动创建）

### 8.2 沙箱内终验

```bash
# 在沙箱内
pip install --no-cache-dir pandas   # 未预装的包，验证现场安装
python3 -c "import fitz, pdfplumber, pandas, docx, openpyxl, PIL; print('ALL_IMPORTS_OK')"
pdftotext -v 2>&1 | head -1
```

预期：`ALL_IMPORTS_OK`，`pdftotext` 有版本输出。

### 8.3 回归：真实 PDF 解析任务

在 agent 里上传一个 PDF 做"解析 PDF"任务，确认能正常完成（不再触发 M3 终止）。

---

## 9. 回滚方案

| 场景         | 操作                                                                                |
| ---------- | --------------------------------------------------------------------------------- |
| 新模板有问题     | 模板保留旧 `tpl-e4829af7cc8e4eb7ac708560`（基于 sandbox-code:latest），agent 切换回旧模板即可；旧镜像未动 |
| devpi 挂了   | `systemctl restart devpi`（systemd 方式）或重新 nohup 启动；沙箱预装库不受影响（离线可用），未预装的包才依赖 devpi  |
| 彻底不要 devpi | `systemctl disable --now devpi` 并删除 /opt/devpi-venv（数据在 /home/data/devpi，可删可留）    |



---

## 10. 运维备忘

- devpi 数据目录：`/home/data/devpi`（磁盘不足时先扩这里）；虚拟环境：`/opt/devpi-venv`
- devpi 日志：`journalctl -u devpi -f`（systemd 方式）或 `tail -f /var/log/devpi.log`
- 镜像更新流程（以后改预装清单）：改 Dockerfile → build → push 新 tag → 13 上 `tpl create-from-image` 或 `tpl redo`
- 常用库清单可按需增删（预装只做高频兜底，其余靠 devpi 现场装）
- 13 的 AC 认证网关问题长期看应找 IT 给 172.16.66.13 开上网白名单，但 devpi 方案下不依赖它
