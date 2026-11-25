# 斐讯 S7 称重服务

本服务用于接收斐讯 S7 / S7P 体脂秤的称重数据，保存在自己的数据库中，并通过浏览器查看。数据不经第三方服务器，无需厂商 App，也不依赖其云服务是否停运。

## 准备工作

| 项目 | 要求 |
| --- | --- |
| 服务器 | 一台可长期开机的电脑或 NAS，已安装 Docker |
| 网络 | 服务器与体脂秤处于同一局域网 |
| 体脂秤 | 已通过 [配网工具](https://github.com/Elliont/feixun_s7_wifi_setup) 指向本服务器（`服务器IP:30101`） |

## 安装部署

1. 在服务器上执行：

   ```bash
   git clone https://github.com/Elliont/feixun_s7_web.git
   cd feixun_s7_web
   docker compose pull && docker compose up -d    # 用官方镜像
   ```

   如需改用本目录代码构建，把最后一行换成 `docker compose up -d --build`。

2. 浏览器访问 `http://<服务器IP>:8080`。
3. 首次打开会进入设置页面，创建用户名和密码（即管理员账号，用户名此后不可修改）。
4. 执行 `docker compose ps` 确认容器正常运行。

部署完成后让秤称重一次，执行 `docker compose logs -f ingest`，看到 `login -> 称重 ...` 即表示数据已正常上报。

## 功能

已实现：

- 自动按体重识别家庭成员，识别不准时落入「待分配」手动认领
- 12 项体成分指标（体脂率、BMI、肌肉量、基础代谢等），内置三套体脂判定标准可切换
- 称重记录筛选、排序、CSV 导入导出，删除后进回收站，14 天内可还原
- 每人身高、出生日期、性别、目标体重，成员页带目标进度与趋势
- 一键备份数据库、上传恢复，恢复前自动留存当前数据
- 账号登录（密码加密存储）、PWA 支持（可添加到手机主屏幕）

待办（TODO）：

- 通知推送（pushplus）：已有基础实现，待真机验证
- Home Assistant 集成：尚未开始

## 配置说明

### 环境变量（`.env`）

复制 `.env.example` 为 `.env` 后按需修改。全部变量都有默认值，最小可用配置就是一个空的 `.env`。

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `S7_WEB_BIND` | `8080` | 网页映射到宿主机的端口，改后从 `http://<服务器IP>:<此端口>` 访问 |
| `S7_INGEST_PORT` | `30101` | 收秤端口。改这里即可整体调整（容器监听 + 端口映射 + 健康检查同步）；**改完必须把配网 App 里的服务器端口也改成同一个值**，否则秤连不上 |
| `S7_INGEST_BIND` | `0.0.0.0` | 收秤端口的宿主机绑定地址；只想让内网访问可填服务器内网 IP |
| `S7_IMAGE` | `ghcr.io/elliont/feixun_s7_web:latest` | 使用的容器镜像；本地构建填 `feixun_s7_web:local` |
| `TZ` | `Asia/Shanghai` | 时区，影响称重时间显示 |
| `S7_ADMIN_USERNAME` | 空 | 启动时直接创建的管理员用户名；留空则首次访问网页时自助建号 |
| `S7_ADMIN_PASSWORD` | 空 | 上述管理员的密码；公网部署建议填强密码，避免被抢先注册 |
| `S7_SECRET_KEY` | 空 | 会话签名密钥；留空自动生成并写入数据库，重启不掉登录 |

高级项（一般无需修改，均已写入 `.env.example`）：

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `S7_SESSION_DAYS` | `30` | 登录有效期（天） |
| `S7_COOKIE_SECURE` | `0` | HTTPS 反向代理后设为 `1`，给会话 cookie 加 Secure 标志；直接 http 访问保持 `0` |
| `S7_TRUST_PROXY` | `0` | 仅在可信反向代理后设为 `1`，使用代理传入的客户端地址做登录限流 |
| `S7_DEBUG` | `0` | 设为 `1` 打开详细调试日志 |
| `S7_DB_PATH` | 空 | 单独指定数据库文件路径，留空则用 `$S7_DATA_DIR/s7.db`；Compose 部署时应指定容器内 `/data` 下的文件路径 |
| `S7_DEV_NOAUTH` | `0` | 调试用，置 `1` 完全跳过登录；**切勿在公网开启** |

### 把数据库映射到宿主机

默认用 Docker 命名卷，数据库不在宿主机目录里。若想直接看到数据库文件，编辑 `compose.yml`，把 `ingest` 和 `web` 两个服务的 `volumes` 都改成：

```yaml
      - ./data:/data    # 原为 s7data:/data，两处都要改
```

再执行 `docker compose up -d`，数据库文件就位于项目目录下的 `data/s7.db`。

## 数据与备份

- 数据库为 SQLite（WAL 模式），同目录还有 `-wal` / `-shm` 文件，三者要一起看
- 网页「设置」页可一键下载备份；恢复时上传 `.db` 文件即可，系统会自动先留存一份当前数据
- 忘记密码时执行以下命令后重启服务，网页会回到设置页重新建号：

```bash
docker compose exec web python -c "
import sqlite3; c=sqlite3.connect('/data/s7.db'); c.execute('DELETE FROM admin'); c.commit()"
```

## 常见问题

**秤连不上？** 确认秤连接的是 2.4GHz Wi-Fi（不支持 5GHz），与服务器在同一网段，且防火墙已放行 `30101/tcp`；再用 `docker compose logs -f ingest` 查看是否收到 `login` 记录。

**公网访问？** 请在前面加一层反向代理，不要把 `8080` 端口直接暴露到公网。数据接收端口 `30101` 没有加密和认证，不建议开放到公网。

**内存占用？** 两个进程各约 60–80 MB。

## 致谢

本项目的协议解析、体成分算法与体脂判定标准参考了以下开源项目：

- [Praepes/phicomm-s7-ha](https://github.com/Praepes/phicomm-s7-ha)
- [dckiller51/bodymiscale](https://github.com/dckiller51/bodymiscale)

## 免责声明

本项目与斐讯无关，仅供个人学习与自建使用。体脂率为估算值，不能作为医疗诊断依据，由此产生的后果由使用者自行承担。请勿用于商业用途。
