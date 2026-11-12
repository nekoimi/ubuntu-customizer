# Linux Image Builder

通过 Alpine Live 安装环境部署预先初始化完成的 Ubuntu / Debian 磁盘镜像，制作可离线安装的启动 ISO。

首版目标为 Ubuntu 24.04 和 Debian 13，入口与目录划分见 [多发行版构建](docs/多发行版构建.md)。本次改造尚需分别执行 Linux 构建和安装验收。

```bash
bash build.sh setup
bash build.sh build --profile profiles/ubuntu-server.json
bash build.sh build --profile profiles/debian-server.json
```

显式 Profile 使用 `config/host.json` 和各发行版默认配置；不带 `--profile` 的原命令继续读取 `config/build.json`、`config/packages.txt` 构建 Ubuntu。以下原有说明主要对应 Ubuntu 默认方案。

已实现 Ubuntu 母镜像构建、Alpine Live ISO 打包和整盘安装器，当前以 **Ubuntu 构建机器直接执行命令行** 为主要方式。GitHub workflow 暂缓接入，保留代码且仍只允许手动触发。

不需要 GitHub Runner、Actions Secret 或 Docker。详细步骤见 [构建与运行](docs/构建与运行.md)。此前 Ubuntu ISO 已由用户完成实机安装；重构后的 Profile 构建仍需重新验收，Debian 尚无安装成功记录。

```bash
# Ubuntu 22.04 / 24.04 x86_64 构建服务器
[ -f config/local.env ] || cp config/local.env.example config/local.env
chmod 600 config/local.env
nano config/local.env  # 填写初始密码和可选的构建代理；已有配置可跳过编辑
bash build.sh setup
bash build.sh check
bash build.sh build
```

GitHub 下载失败时，可先运行 `bash build.sh check-downloads`，使用相同的 `.env` / 环境代理实际下载并核验 uv、v2rayA；不构建磁盘镜像，也不发送通知。日志会显示代理配置是否启用、GitHub 的配置路由及失败 HTTP 状态码，不显示代理凭据。

重复构建会复用 `<cache_dir>/<发行版>/<suite>/amd64/<仓库配置摘要>/apt/archives` 中的系统 / Docker / Tailscale `.deb` 软件包，减少下载量；仓库索引仍刷新并验证签名。新 Profile 构建在 `config/host.json` 设置 `cache_dir`，旧命令仍在 `config/build.json` 设置；设为 `null` 可禁用。原缓存不自动迁移，首次下载速度仍取决于镜像站和构建代理。

可在 Linux 服务器执行 `bash build.sh vm-test --iso <ISO路径>`，使用 KVM、独立虚拟 NVMe 和本机 VNC 测试安装；安装后用 `--boot installed` 启动同一测试盘。依赖安装、Windows SSH 隧道和完整日志导出见 [Linux 虚拟机安装测试](docs/Linux虚拟机安装测试.md)。

入口在需要时自动调用 sudo。构建编号默认自动生成，ISO 保存到 `/srv/ubuntu-customizer/releases/<build-id>/ubuntu-custom-server.iso`，终端会显示实际路径。`bash build.sh test` 可运行安全测试和静态检查，`bash build.sh --help` 查看用法。

终端显示主要步骤的开始、完成和耗时；长时间运行时每 30 秒提示当前步骤仍在执行。详细下载、安装输出保存在终端提示的 `tools.log`，可在另一个终端用 `sudo tail -f <日志路径>` 查看。

构建入口使用独立的挂载、PID、UTS 和 IPC 命名空间，隔离镜像内包脚本与宿主进程，`/run` 使用独立 tmpfs；下载网络仍与宿主共用。这是以 root 执行的构建流程，并不等同于虚拟机级隔离；建议使用专用构建机器或虚拟机。

uv 和 v2rayA 从 GitHub API / Releases 下载，Tailscale 使用官方 `pkgs.tailscale.com` 源；这些站点访问较慢时，可在 `config/local.env` 配置现有 v2rayA 的 **HTTP 代理**（地址和端口按实际部署填写）：

```bash
HTTP_PROXY='http://192.168.1.10:20171'
HTTPS_PROXY='http://192.168.1.10:20171'
NO_PROXY='localhost,127.0.0.1,::1,.aliyun.com,.tsinghua.edu.cn'
```

此处的代理地址与凭据仅对 `setup` 和 `build` 生效，覆盖宿主下载、debootstrap、镜像内 APT / curl 和 Alpine APK，不会写入交付系统。Ubuntu / Docker / Alpine 国内源按 `NO_PROXY` 直连。交付系统另行预置本机 v2rayA 和 Docker 代理，与构建代理独立；具体规则见 [构建代理说明](docs/构建与运行.md#构建代理可选)。

详细设计见 [Ubuntu Server 定制镜像与整盘安装方案](docs/Ubuntu-Server-定制镜像方案.md)。

待确定的配置与建议默认值见 [待确认事项](docs/待确认事项.md)。

已确认的基础预装集合及可选工具见 [基础工具清单](docs/基础工具候选清单.md)。

编译工具、Python、uv 和国内包源配置见 [开发环境预装清单](docs/开发环境预装清单.md)。

暂缓实施的 GitHub 调度方式见 [构建与 GitHub Actions 方案](docs/构建与GitHub-Actions方案.md)。

已明确的要求：

- 目标系统为 Ubuntu Server 24.04 LTS，从已签名的 noble APT 仓库构建，记录实际软件版本和来源摘要。
- 仅制作 x86_64 / amd64，安装 ISO 与目标系统均支持 UEFI 和传统 BIOS；首版不支持 Secure Boot，使用时关闭该功能。
- 面向常见 PC、服务器及普通 SATA / NVMe SSD、HDD，无特殊硬件驱动需求。
- 在构建镜像时安装系统工具、开发依赖和 Docker，移除 Snap。
- 基础工具清单第 1 节全部纳入预装，第 2 节基础系统能力保留并验收，第 3 节可选工具不自动纳入。
- 预装编译工具、发行版 Python、venv / pip / 开发头文件与 uv；pip 使用国内源。
- 不预装 Node.js、Java、Go、Rust，以及数据库与 Web 服务。
- 预装 Docker Engine、CLI、containerd、Buildx 与 Compose 插件，使用构建时国内 Docker APT 仓库可用的最新兼容稳定版本，并记录实际版本。
- 预装 Tailscale，`tailscaled` 开机自启；镜像保持未认证状态，安装后由用户执行 `sudo tailscale up` 手动认证，后续重启沿用本机认证状态。
- 预装构建时最新稳定版 v2rayA 官方 Debian 包（含代理核心和规则数据），开机自启；访问 `http://服务器IP:2017` 创建管理账号、导入并连接节点后，Docker 即可经预置的 `127.0.0.1:20171` HTTP 代理拉取镜像。无节点时外网拉取会失败；不预置节点、订阅或 Web 账号，不启用透明代理及 DNS 接管。详见 [v2rayA 与 Docker 代理](docs/构建与运行.md#v2raya-与-docker-代理)。
- 提供 Bash 终端代理开关 `proxy-on` / `proxy-off`、环境状态 `proxy-status` 和连通检查 `proxy-check`；新终端默认不启用代理，Docker 保持独立配置。
- 可在 `config/local.env` 填写 `NTFY_URL`、`NTFY_TOPIC`、`NTFY_TOKEN`，预置开机和内网 IPv4 变化通知，内容包含主机名、网卡地址和 SSH 命令；发送失败自动重试，不阻塞启动。三项留空则禁用。Token 会进入私人 ISO，配置与日志规则见 [ntfy 通知](docs/构建与运行.md#ntfy-内网-ip-通知)。
- Ubuntu APT 与 Docker 的 APT 安装仓库均使用国内镜像源，并在安装后保留；不配置 Docker 镜像加速地址。
- Alpine 发布信息、minirootfs 和 APK 软件包统一使用阿里云国内镜像 `https://mirrors.aliyun.com/alpine`。
- 采用阿里云 Ubuntu / Docker APT 源、清华 pip / uv 源；SSH 使用 22 端口和 `ubuntu` 密码登录，网络使用 DHCP，主机名由安装器自动生成。
- SSH 登录成功后直接进入命令行，不显示 MOTD、系统状态、Ubuntu Pro 广告、更新或重启提醒及上次登录信息。
- 预置 journald 日志限额、上海时区与国内 NTP、每周 `fstrim`、SSH 保活、tmux 操作体验和 core dump 限制；提供手动执行的 `server-info` 与 `server-update` 命令。
- 预装并启用 UFW，IPv4 / IPv6 默认拒绝宿主入站、允许出站，预先放行 SSH TCP 22 和 v2rayA Web TCP 2017；Docker 发布端口需单独管理，不能依赖 UFW 普通入站规则限制。
- 安装器先显示 ASCII Logo 和欢迎页，等待输入 `y` 回车后才开始镜像校验及选盘；交互使用独立提示框和步骤编号，黄色选盘倒计时、红色清盘确认；无颜色终端仍显示完整文字提示。
- 采用 2 GiB 交换文件，`ubuntu` 通过 sudo 管理 Docker；上海时区、默认英文环境并预留中文 locale，安装结束后手动重启。
- 保留系统后台更新检查，关闭 SSH 登录欢迎信息和更新提示，关闭自动安装及自动重启，由用户手动维护补丁。
- 安装结束后的第一次启动即可使用，不依赖首次开机初始化脚本。
- 镜像预设首个普通用户 `ubuntu`（UID/GID 均为 `1000`）与已经指定的初始密码，安装时不询问；进入系统后自行修改密码，不强制首次登录改密。初始密码保存在本地不入库的 `config/local.env`。
- 沿用 Ubuntu Server 的 sudo 管理方式，root 保持锁定，不设置 root 密码，不开放 root 直接登录。
- 选盘界面倒计时 30 秒，无操作时选择容量最大的合格硬盘。
- 选盘后等待一次明确清盘确认，确认步骤不设倒计时，未确认前不清盘或写入。
- 自动选盘排除安装介质、只读盘、容量不足和被占用的设备。
- 完整 ISO、日志、清单和报告保存到构建机器 `/srv/ubuntu-customizer/releases/<build-id>/`；手动构建不上传 GitHub。将来启用 workflow 时仅上传固定的小文件报告。
- 最终验收由用户自行使用虚拟机及实体机器 U 盘安装完成；构建流程提供自动检查结果和待验收 ISO。
- 首版支持 ISO 直接整盘写入 U 盘及虚拟机直接挂载 ISO，暂不适配 Ventoy。
- 首版不制定最小设备规格；安装器仍检查目标盘容量等必要条件。

构建脚本读取本地不入库的密码配置或 `UBUNTU_INITIAL_PASSWORD` 环境变量。当前只需在 Ubuntu 构建机器安装依赖、准备密码配置并执行本地入口；不需要注册 Runner。构建不依赖 KVM，最终虚拟机和实体机器验收由用户完成。
