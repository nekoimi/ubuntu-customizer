# 构建环境与 GitHub Actions 方案

状态：GitHub workflow 接入暂缓，保留仅手动触发的配置。当前使用根目录 `build.sh` 在 Ubuntu 构建机器直接执行，不需要部署 Runner。本文保留 GitHub 方案供以后恢复；当前操作说明见 [构建与运行](构建与运行.md)。

## 1. 已采用的执行方式

使用现有 Ubuntu Server 22.04 x86_64 服务器作为 self-hosted Runner，标签为 `ubuntu-customizer`。GitHub 负责手动触发、静态检查、任务状态和小文件报告，服务器负责完整构建和保存 ISO。

部署核对补充：当前服务器上的 Runner 实际通过 Docker Compose 运行，未开启特权或映射 loop 设备，且没有本项目标签和产物映射，不能直接执行当前完整构建。现有部署的逐项差异和专用构建虚拟机建议见 [构建与运行第 4.1 节](构建与运行.md#41-现有-docker-compose-runner-的适用情况)。尚未修改该 Compose 或部署专用构建环境。

现有机器为 i5-1240P、16 个逻辑 CPU、31 GiB 内存，当时约 27 GiB available、根分区可用约 143 GiB。当前实现采用文件型 raw 磁盘 + debootstrap / chroot，不依赖 KVM 或宿主 Docker。此前讨论的 QEMU / autoinstall 构建方式未采用；用户仍可选择虚拟机完成最终验收。

## 2. 触发与调度

`.github/workflows/build.yml` 的事件只有 `workflow_dispatch`：

- 不设置 push、pull_request、schedule、release 或标签触发。
- 只在仓库默认分支执行手动构建；选择其他分支会跳过任务。
- GitHub 托管 `ubuntu-24.04` 任务执行 Python 安全测试、ShellCheck 和 Shell 语法检查。
- 检查成功后，在 `[self-hosted, linux, x64, ubuntu-customizer]` 上执行完整构建。
- 使用 concurrency 排队，`cancel-in-progress: false`，不因新构建取消旧任务；构建入口还有本机文件锁。
- 构建任务时限 360 分钟，Artifact 保留 14 天。

本地和 workflow 使用同一入口 `sudo bash scripts/build.sh`，不依赖必须存在的 GitHub 环境变量。workflow 通过 `--build-id gha-<run_id>-<run_attempt>` 区分产物。

## 3. 资源与隔离

默认模板磁盘为 24 GiB、压缩并行度 4。构建前检查当前可用磁盘至少 80 GiB、MemAvailable 至少 4 GiB、必要工具以及 BIOS / UEFI 的 GRUB 模块。模板容量和资源阈值通过 `config/build.json` 调整，现有服务器的首次峰值仍需实际测量。

构建脚本创建 raw 文件并分配 loop 设备，所有母镜像挂载位于私有挂载命名空间；不接受宿主物理磁盘参数。构建期间禁止 guest 包自动启动服务。换源、创建账号、移除 Snap、安装 Docker / Tailscale 均作用于 guest 根目录。

宿主仅需要运行 `scripts/setup-host.sh` 安装构建工具，不更换宿主软件源、不创建交付账号，也不卸载宿主软件。

## 4. 产物保留

完整 ISO 保存到 `/srv/ubuntu-customizer/releases/<build-id>/ubuntu-custom-server.iso`，与 Runner `_work` 和工作目录分开。构建结果保存并校验后再上传小文件；上传失败不会删除本地 ISO。用户使用 SFTP / SCP 获取文件。

| 内容 | 构建服务器 | GitHub Artifact |
|---|---|---|
| 完整 ISO | 保存 | 不上传 |
| raw、压缩磁盘、Live 根文件系统 | 保留在 work 目录 | 不上传 |
| ISO SHA256、manifest、Ubuntu 包版本清单 | 保存 | 上传固定文件 |
| 工具原始输出 | 私有 work 目录；另存脱敏副本到 releases/logs | 不上传 |
| 阶段摘要和构建检查结果 | 保存 | 上传固定文件 |

上传路径只有 `reports/build-report.json`、`reports/build.log`、`reports/manifest.json`、`reports/packages.txt`、`reports/SHA256SUMS`。不使用目录通配打包，不把 ISO 通过 Artifact、Release、Cache、Git LFS、分片等方式传到 GitHub。

原始工具日志与磁盘文件不进入报告暂存目录。初始密码和哈希不进入公共 manifest，报告不包含异常中的任意命令输出。完整工作目录和历史产物不自动删除，需按实际磁盘占用管理。

## 5. 凭证与 Runner 接入

GitHub 使用 `UBUNTU_INITIAL_PASSWORD` Actions Secret；本地构建可以使用不入库的 `config/local.env`。Secret 通过环境变量和标准输入使用，不作为命令行参数，不执行 `set -x`。

Runner 安装注册、标签、服务账号 sudo 配置和 Secret 设置需要在实际仓库及服务器部署，具体步骤见 [构建与运行](构建与运行.md)。当前没有代为注册或访问远端服务器，也没有推送代码。

Runner 能执行 root 构建脚本，因此只为可信仓库、默认分支和维护者提供执行环境。交付系统中的 `ubuntu` 用户使用正常的密码 sudo，这与 Runner 权限配置无关。

## 6. 验收与当前验证边界

构建中执行 Ubuntu 软件和配置检查、压缩流校验、双模式 El Torito 目录检查及 ISO 读取检查。当前没有在构建时真正启动目标 Ubuntu，Docker 守护进程、Tailscale 联网、BIOS / UEFI 引导及实体机器兼容性需要最终验收。

报告区分构建检查和人工验收：未实际运行的启动与安装测试写 `not_run`，人工验收写 `pending`。用户按主方案第 14 节使用虚拟机和实体 U 盘安装进行验收；不会把静态检查成功当作硬件验收通过。

## 7. 官方参考

- [GitHub 手动触发工作流](https://docs.github.com/en/actions/managing-workflow-runs-and-deployments/managing-workflow-runs/manually-running-a-workflow)
- [GitHub 自托管 Runner](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners)
- [GitHub Actions 使用限制](https://docs.github.com/en/actions/reference/limits)
