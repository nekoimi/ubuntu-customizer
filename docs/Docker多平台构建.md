# Docker 多平台构建与代理

Ubuntu 24.04、Debian 13 和原 Ubuntu 默认构建均预装 Buildx、静态 QEMU，并配置 ARM64 binfmt 注册。amd64 是原生执行，arm64 通过模拟执行。注册配置在 `/etc/binfmt.d/customizer-aarch64.conf`，由 `systemd-binfmt.service` 在每次开机时加载，包含容器需要的 `F` 标志。Ubuntu 使用 `qemu-user-static`，Debian 13 使用提供静态模拟器的 `qemu-user`，构建时检查模拟器没有动态解释器依赖。

制作 ISO 时只安装文件，不注册构建宿主的 binfmt、不启动 Docker、不创建构建器或下载 BuildKit 镜像。QEMU 包可能直接调用的 `update-binfmts` 在镜像内安装期间通过临时 dpkg-divert 延迟执行，结束时恢复；交付系统使用正常的启动注册机制，不需要运行特权容器注册模拟器。以下操作在安装后的系统执行。

## 首次配置

1. 打开 `http://服务器IP:2017`，创建 v2rayA 账号、添加并连接节点。
2. 检查代理，然后初始化构建器：

```bash
proxy-check
sudo docker-multiarch-setup
```

初始化命令创建 `customizer-multiarch`，使用本机 rootful Docker、root 的 `/root/.docker` 配置和 `docker-container` 驱动。首次通过 Docker 已有的代理下载默认 BuildKit 镜像，然后启动并显示平台列表。仅检测 `20171` 端口不足以证明节点可用，`proxy-check` 和实际构建用于验证外网访问。

命令可以重复执行：已有配置匹配时复用构建器与缓存；同名构建器的驱动、端点、代理等不匹配时拒绝继续，不自动删除、替换或清理缓存。不会修改其他构建器、Docker daemon 配置、普通容器的环境变量或用户的 Docker 客户端代理配置。初始化会将该构建器选为 root 用户在本地上下文的当前构建器。

构建器状态与缓存保存在本机 Docker 数据中，容器重启策略为 `unless-stopped`。重启系统后 binfmt 由 systemd 恢复；若手动停止过 BuildKit，可再次执行初始化或由下一次构建启动。每次重启仍需 v2rayA 节点可用，命令不会自动添加或连接节点。

## 构建并推送

```bash
# 私有仓库先使用 root 的 Docker 配置登录
sudo docker login 你的镜像仓库

sudo docker-multiarch-build \
  --platform linux/amd64,linux/arm64 \
  -t 你的镜像仓库/应用:latest \
  --push .
```

`docker-multiarch-build` 等价于在 `docker buildx build` 上增加指定构建器、`--network=host`、`--allow=network.host`，以及 HTTP / HTTPS / NO_PROXY 的大小写两组预定义代理参数。其余参数原样交给 Buildx，平台、tag 和导出方式由调用者指定，不自动推送。也可用 `sudo docker-multiarch build ...`、`sudo docker-multiarch setup`。

单平台本机试用可使用 `--platform linux/amd64 --load`。多平台一般使用 `--push`，也可以指定 OCI 文件输出；本机是否支持多平台 `--load` 取决于 Docker 镜像存储配置。本项目不切换镜像存储模式。

| 下载位置 | 预置方式 |
| --- | --- |
| Docker 下载 BuildKit 镜像 | 已有 daemon 代理 |
| BuildKit 解析、拉取基础镜像，以及推送/导入导出远端缓存 | 构建器容器的 HTTP_PROXY / HTTPS_PROXY / NO_PROXY 环境变量 |
| Dockerfile `RUN` 中的 curl、APT 等下载 | 构建命令注入标准代理 build args，配合宿主网络访问本机代理 |

构建器与 `RUN` 均通过 `http://127.0.0.1:20171` 使用已连接节点。内网地址按预置 NO_PROXY 直连。**宿主网络允许构建步骤访问宿主网络服务，适合自己可信的 Dockerfile。** 不新增代理监听地址或开放 `20171` 防火墙端口。

直接使用 `sudo docker buildx build --builder customizer-multiarch ...` 时，BuildKit 自身访问仓库有代理，但不会自动获得这里的 `RUN` 代理参数和宿主网络；需要下载依赖时优先使用预置构建命令。普通 `docker build`、`docker run`、Compose 和其他 builder 的行为不受此命令影响。

Dockerfile 无需写 `ENV HTTP_PROXY=...` 或把代理存入最终镜像。Docker 的预定义代理参数默认不会进入镜像历史和缓存键；Dockerfile 显式声明、输出或保存代理变量时可能改变这一行为。自行覆盖代理、使用 `RUN --network=none` 或不遵循代理环境变量的程序，也会影响实际访问。

## 检查与维护

```bash
cat /proc/sys/fs/binfmt_misc/customizer-aarch64
systemctl status systemd-binfmt --no-pager
sudo docker buildx inspect customizer-multiarch --bootstrap
sudo docker inspect buildx_buildkit_customizer-multiarch0 \
  --format '{{.HostConfig.NetworkMode}} {{json .Config.Env}}'
```

binfmt 输出应为 `enabled` 且 flags 包含 `F`；构建器平台列表应包括 `linux/amd64`、`linux/arm64`。需要重新注册时使用 `sudo systemctl restart systemd-binfmt`；这会重新加载该服务管理的其他 binfmt 规则，QEMU 更新后可用它刷新内核持有的模拟器文件。

跨架构模拟编译通常慢于原生 ARM64。两平台的基础镜像和依赖必须都有对应版本；其他架构支持取决于实际注册的模拟器，本版只保证预置 ARM64 注册。

当前开发环境没有 Linux Docker daemon，已做离线行为测试与脚本检查；仍需在 Ubuntu、Debian 安装系统上分别验证重启后的注册，以及真实的双平台 `FROM`、`RUN` 下载和推送。
