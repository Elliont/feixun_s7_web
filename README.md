# 韭菜称重 · feixun_s7_web

斐讯 **S7 / S7PE** 体脂秤的本地化服务：秤的数据直连你自己的服务器，存进你自己的 SQLite，用浏览器看。

不是云端账号、不上传第三方、不依赖厂商 App 停不停服。

```
斐讯 S7 秤  ──TCP 30101──▶  ingest  ──▶  SQLite  ──▶  web (8080)  ──▶  浏览器 / PWA
（Wi-Fi 直连你的服务器）                    /data/s7.db
```

- **Python 3.12 + FastAPI + Jinja2**，无前端构建链，首屏 gzip 后 ≤ 80 KB
- 一套代码两个进程：`s7.ingest` 收秤的帧，`s7.web` 出网页
- 单文件数据库，备份就是拷一个 `.db`

> ⚠️ 协议格式与体成分算法参考了公开的社区资料与实现，仅供个人自建自用与学习，请勿用于商业分发。见文末[免责声明](#免责声明)。

---

## 功能

### 已实现

| 模块 | 说明 |
|---|---|
| **自动归属** | 称重后按「最近 5 次体重中位数」自动匹配家庭成员，阈值可调（默认 1 kg）；拿不准就落进「待分配」等你认领 |
| **家庭成员** | 每人身高 / 出生日期 / 性别 / 目标体重，成员页带目标环与趋势 |
| **体成分** | 12 项指标：BMI、体脂率、身体评分、代谢年龄、肌肉量、骨骼肌、内脏脂肪、水分率、蛋白质、脂肪量、去脂体重、体型 |
| **体脂判定标准** | 内置 3 套可切换：**ACE**（5 档，不分年龄）、**Gallagher**（4 档，按年龄段和性别）、**小米**（4 档，取自 bodymiscale 的 `_FAT_SCALES`） |
| **称重记录** | 三态筛选（自动 / 待分配 / 已忽略）、Excel 式列筛选、多列排序、10/20/50 每页、行可点进详情、CSV 导入导出 |
| **回收站** | 删除是搬进回收站，14 天内可还原，到期自动清理 |
| **多设备** | 每台秤可绑定成员池（三态：全部 / 指定 / 不限），先绑定池再回退全局 |
| **备份恢复** | 一键下载 `.db`，服务器 `backup/` 同时留档；上传恢复前自动存一份 `before-restore-*.db` |
| **登录** | 首次访问自己建号（用户名不可改），argon2 哈希，会话 30 天，右上角退出登录 |
| **PWA** | 加到主屏幕、离线壳、桌面图标 |

### 待办（TODO）

- [ ] **通知推送（pushplus）**：已有一份基础实现，待真机验证与完善；订阅维度 `measure.any` / `measure.member.<id>` / `measure.unassigned`
- [ ] **Home Assistant 集成**：MQTT 设备发现与测量数据上报，尚未开始

---

## 快速开始（Docker）

```bash
git clone https://github.com/Elliont/feixun_s7_web.git
cd feixun_s7_web
cp .env.example .env        # 可选，全部有默认值
docker compose up -d        # 首次会用本目录 Dockerfile 构建
```

或者直接用 Actions 构建好的镜像：

```bash
docker compose pull && docker compose up -d
```

起来之后：

- 网页：`http://<服务器IP>:8080`
- 第一次打开会跳到 **首次设置**，设一个用户名 + 密码（这就是管理员，用户名之后改不了）
- 秤连：`<服务器IP>:30101`

```bash
docker compose ps
docker compose logs -f web
```

镜像地址：`ghcr.io/elliont/feixun_s7_web:latest`（`main` 分支每次推送自动构建，多架构 `amd64/arm64`）。

### 想改成「启动即建号」而不是首次访问建号

公网机器建议这么做，避免第一个访问的人抢注：

```ini
# .env
S7_ADMIN_USERNAME=admin
S7_ADMIN_PASSWORD=换成强密码
```

---

## 秤怎么配网

秤要连到 `你的服务器IP:30101`，这一步在秤上手动做（长按 RESET 进配网模式，屏显 `@` 闪烁）。

配网工具是配套的另一个项目：**[feixun_s7_wifi_setup](https://github.com/Elliont/feixun_s7_wifi_setup)** —— 用手机 App 把 Wi-Fi 账号密码 + 服务器地址写进秤。

配网成功后踩一脚秤，`docker compose logs -f ingest` 能看到：

```
login -> 称重 78.50 kg -> 体脂 24.2% ...
```

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `S7_DATA_DIR` | `/data` | 数据目录（`s7.db` 和 `backup/` 都在这） |
| `S7_DB_PATH` | `$S7_DATA_DIR/s7.db` | 想拆开时单独指定 |
| `S7_WEB_PORT` | `8080` | 网页端口 |
| `S7_INGEST_PORT` | `30101` | 秤连的端口 |
| `S7_WEB_BIND` / `S7_INGEST_BIND` | `0.0.0.0` | compose 里的宿主绑定地址 |
| `S7_IMAGE` | `ghcr.io/elliont/feixun_s7_web:latest` | compose 用哪个镜像 |
| `S7_ADMIN_USERNAME` / `S7_ADMIN_PASSWORD` | 空 | 填了就在启动时建号；留空走 `/setup` |
| `S7_SECRET_KEY` | 空 | 会话签名密钥；留空自动生成并写库（重启不掉登录） |
| `S7_DEV_NOAUTH` | `0` | **调试用**：置 1 完全跳过登录，别在公网开 |
| `S7_SESSION_DAYS` | `30` | 登录有效期 |
| `S7_DEBUG` | `0` | 日志级别 |
| `TZ` | `Asia/Shanghai` | 时区，影响称重时间显示 |

---

## 数据与备份

- 全部数据在卷 `s7_s7data` 的 `/data/s7.db`（SQLite WAL 模式，同目录还有 `-wal`/`-shm`）
- 设置页「立即备份并下载」拿到的就是可直接使用的 `.db`
- 服务器上手动备份：

```bash
docker run --rm -v s7_s7data:/data -v "$PWD":/backup alpine \
  tar czf "/backup/s7-$(date +%F).tgz" -C /data .
```

- 恢复：设置页上传 `.db`，或

```bash
docker compose down
docker run --rm -v s7_s7data:/data -v "$PWD":/backup alpine \
  sh -c 'rm -rf /data/* && tar xzf /backup/s7-YYYY-MM-DD.tgz -C /data'
docker compose up -d
```

> 升级只会补新表/新列（`ensure_schema`），老库直接能用。**但升级前先备份**，这是唯一的好习惯。

---

## 本地开发

不用 Docker 也行（3 个依赖 + 一个 venv）：

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export S7_DATA_DIR=./data
export S7_DEV_NOAUTH=1        # 调试跳过登录
python -m s7.web              # http://127.0.0.1:8080
python -m s7.ingest           # 另开一个终端，收秤的数据
```

想改代码立即生效：

```bash
S7_DEV_RELOAD=1 python -m s7.web
```

---

## 目录结构

```
s7/
├── web.py            FastAPI 应用、路由挂载、启动钩子
├── pages.py          记录/设置/设备/成员/登录 路由 + 认证中间件
├── ingest.py         30101 监听：收帧 → 解析 → 归属 → 落库 → 通知
├── protocol.py       S7 帧协议（XOR 0x55 + CRC16，登录/时间/上报/历史）
├── bodycomp.py       体成分计算（芯海 50kHz 公式）
├── fatstandards.py   体脂判定标准（ACE / Gallagher / 小米）
├── grading.py        指标分级与卡片描述
├── attribution.py    自动归属规则
├── db.py             SQLite 封装、schema 迁移、审计
├── ui.py             模板上下文、格式化、成员卡
├── notify.py         通知推送基础模块（pushplus，见 TODO）
├── config.py         全部配置来自环境变量
├── schema.sql        建表语句（幂等，启动时执行）
├── templates/        14 个 Jinja2 模板
└── static/           app.css / app.js / PWA 图标
```

---

## 常见问题

**秤连不上？**
1. 秤必须连 **2.4G** Wi-Fi（5G 不行）
2. 手机和秤在同一网段，关掉代理 / VPN
3. 防火墙放行 `30101/tcp`
4. `docker compose logs -f ingest` 看有没有 `login` 记录

**忘记密码了？**

```bash
docker compose exec web python -c "
import sqlite3; c=sqlite3.connect('/data/s7.db'); c.execute('DELETE FROM admin'); c.commit()"
# 重启后打开网页会回到 /setup 重新建号
```

**公网访问？**

前面套一层反向代理，别把 8080 直接开公网：

```caddyfile
s7.example.com {
    reverse_proxy 127.0.0.1:8080
    encode gzip
}
```

**`30101` 要不要开公网？** 尽量别。协议没有 TLS 也没有认证，扫描器可以往里灌假数据。推荐把服务放在家里的机器上，公网只反代 Web。

**内存占用多少？** 两个进程各 ~60–80 MB，compose 里限制 256 MB/容器。

---

## 致谢

协议解析、体成分算法与体脂判定标准参考了以下社区项目，在此致谢：

- [Praepes/phicomm-s7-ha](https://github.com/Praepes/phicomm-s7-ha) — 斐讯体脂秤接入 Home Assistant 的协议实现
- [dckiller51/bodymiscale](https://github.com/dckiller51/bodymiscale) — 体脂秤数据处理与体成分算法参考

## 免责声明

- 本项目与斐讯、芯海、小米均无关联，未获任何授权
- 协议格式、体成分公式、体脂判定阈值参考公开的社区资料与实现（bodymiscale 等），**仅供个人学习、自建自用**
- 体脂率是**估算值**，不能作为医疗诊断依据；同一身体不同算法可能差 2–4 个百分点
- 请勿用于商业分发或提供服务；由此产生的一切后果由使用者自负

## 许可

MIT，见 [LICENSE](LICENSE)。
