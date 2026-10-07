# Stockroom 仓储盘点

![Validate](https://github.com/SagisawaTsubasa/Stockroom/actions/workflows/validate.yml/badge.svg)

Home Assistant 自定义集成：面向 **FDM 3D 打印耗材（PLA/PETG/ABS/TPU…）与螺丝等五金件** 的虚拟库存盘点。库存数据持久化在集成自己的 JSON 存储里（不占 config entry），提供动态实体表、六个库存服务、低库存二进制传感器与事件，并支持**拓竹（Bambu Lab）打印完成后按 AMS 分盘克数自动扣料**。

- [功能](#功能)
- [实体](#实体)
- [安装（HACS）](#安装hacs)
- [配置](#配置)
- [服务](#服务)
- [低库存自动化示例](#低库存自动化示例)
- [AMS 自动扣料说明](#ams-自动扣料说明)
- [FAQ](#faq)
- [Changelog](#changelog)

## 功能

- 每个配置项 = 一个虚拟仓库设备，条目（item）数量不限，动态增删实体
- 三类条目：`filament`（打印耗材）/ `screw`（螺丝紧固件）/ `other`（其它五金件）
- 条目字段：名称、分类、数量、单位（颗/个/卷/kg/g…）、低库存线、存放位置、耗材附加属性（材质/颜色/满卷净重）、最近盘点时间、最近变动时间
- 六个领域级服务：`add_item` / `remove_item` / `consume` / `restock` / `stocktake` / `set_threshold`，另有编辑条目 `update_item`、手动换芯 `log_filter_change` 与扫描确认 `scan_confirm` / `scan_dismiss`
- 每次变动：实体即时刷新 + 持久化 + 向事件总线发 `stockroom_item_changed`（自动化可订阅；集成自身**从不主动发通知**）
- 扣减到负数自动钳为 0 并记日志；`stocktake` 校准为实际清点值并记录时间戳
- 拓竹 AMS 自动扣料：打印完成（`finish`）后按 `print_weight` 的分盘克数逐盘扣减对应条目，带任务级幂等去重
- **拍照扫描入库**（可选）：手机拍照 POST 到集成 webhook → 视觉模型识别 → 通知按钮一键确认入库
- **仓管面板**（V0.3.0）：侧边栏全功能管理面板 + 图形化滤芯槽位（可联动 Filter-Life-Tracker，换芯自动扣件并留更换历史）

## 实体

每个仓库（config entry）一个设备，下面挂：

| 实体 | 说明 |
|---|---|
| `sensor.<仓库>_<条目>_stock` | 条目库存量，单位为条目自身单位，属性含分类/名称/位置/低库存线/最近盘点时间/extra 展开 |
| `binary_sensor.<仓库>_<条目>_low_stock` | 低库存标记（device_class: problem），数量 ≤ 低库存线时 on |
| `sensor.<仓库>_total_items` | 条目总数 |
| `sensor.<仓库>_low_stock_items` | 低库存条目数 |

选项里开启「汇总按分类拆分」后，两个汇总传感器会以 `by_category` / `low_by_category` 属性给出分类明细；开启「拍照扫描入库」后，条目总数传感器还会给出 `scan_webhook_url` 属性（手机捷径要填的完整地址）。

## 安装（HACS）

1. HACS → 右上角菜单 → 自定义存储库 → 仓库填 `https://github.com/SagisawaTsubasa/Stockroom`，类别选 **Integration**
2. 安装 **Stockroom 仓储盘点**
3. 重启 Home Assistant
4. 设置 → 设备与服务 → 添加集成 → 搜索 **Stockroom 仓储盘点**

也可以手动把 `custom_components/stockroom/` 拷入 HA 的 `custom_components` 目录后重启。

## 配置

- **创建仓库**：只需填仓库名（默认「仓库」）。允许多个仓库；与现有仓库同名会提醒一次，再提交一次即确认创建。
- **选项**：
  - 默认低库存阈值：`add_item` 未指定 `low_threshold` 时使用（默认 1.0）
  - 汇总按分类拆分：默认关
  - Bambu Lab 料盘实体：选择要自动扣料的拓竹 AMS 料盘传感器（可多选；AMS HT 与外挂盘暂不支持），下一步为每个料盘指定对应的库存条目；留空则不启用 AMS 扣料

## 服务

所有服务都有可选 `entry_id` 字段：配置了多个仓库时用它指定目标仓库，单仓库可省略。`item` 字段优先按条目 id 精确匹配，其次按名称唯一匹配（歧义会报错提示改用 id）。

```yaml
# 新增一卷耗材（id 自动生成为 pla-bai-se-1kg 之类）
service: stockroom.add_item
data:
  name: "PLA-白色 1kg"
  category: filament
  quantity: 1000
  unit: g
  low_threshold: 100
  location: "仓库-柜2"
  material: PLA
  color: "#FFFFFF"
  full_weight_g: 1000

# 新增螺丝
service: stockroom.add_item
data:
  name: "M3×8 内六角"
  category: screw
  quantity: 500
  unit: 颗
  low_threshold: 50

# 用掉 50 颗
service: stockroom.consume
data:
  item: "M3×8"
  quantity: 50
  note: "装了 6 个打印件"

# 补货
service: stockroom.restock
data:
  item: m3x8
  quantity: 200

# 盘点校准（写入 last_stocktake 时间戳）
service: stockroom.stocktake
data:
  item: "PLA-白色"
  actual: 850
  note: "开箱清点"

# 调整低库存线
service: stockroom.set_threshold
data:
  item: pla-bai-se-1kg
  threshold: 150

# 删除条目
service: stockroom.remove_item
data:
  item: m3x8
```

## 低库存自动化示例

集成只出二进制传感器与事件，不主动通知。低库存通知交给你自己的自动化：

```yaml
automation:
  - alias: "耗材低库存提醒"
    triggers:
      - trigger: state
        entity_id:
          - binary_sensor.cangku_pla_bai_se_1kg_low_stock
        to: "on"
    actions:
      - action: notify.mobile_app
        data:
          title: "库存告急"
          message: "{{ state_attr(trigger.entity_id, 'friendly_name') }} 该补货了"

  - alias: "库存变动流水"
    triggers:
      - trigger: event
        event_type: stockroom_item_changed
    actions:
      - action: logbook.log
        data:
          name: "Stockroom"
          message: "{{ trigger.event.data.item_id }} {{ trigger.event.data.action }} → {{ trigger.event.data.new_quantity }}{% if trigger.event.data.note %}（{{ trigger.event.data.note }}）{% endif %}"
```

`stockroom_item_changed` 事件数据：`entry_id`、`item_id`、`action`（add/remove/consume/restock/stocktake/set_threshold）、`new_quantity`、可选 `note`。

## 仓管面板与滤芯槽位（V0.3.0）

侧边栏新增 **仓管面板**（图标 🏭，仅管理员可见），零构建原生 Web Component，认证复用 HA 登录会话。三个视图：

- **库存**：条目卡片网格（低库存红色高亮）、汇总统计、快捷消耗/补货/盘点、新增/编辑/删除条目（全字段）
- **滤芯槽位**：把一台设备的多级滤芯图形化为一排槽位（1→2→3 级），每个槽位绑定一个库存条目
- **更换历史**：每次换芯留档（时间/仓库/组/级/条目/换下时寿命/方式），全局最多保留 500 条

### 滤芯槽位的两种模式

| 模式 | 寿命显示 | 换芯操作 | 扣件方式 |
|---|---|---|---|
| **绑定 FLT 设备** | 实时寿命%（读 Filter-Life-Tracker 传感器，绿/黄/红分级） | 按压 FLT 的重置按钮 | FLT 发出 `filter_life_tracker_filter_reset` 事件 → Stockroom 自动扣减绑定条目 1 个并记历史 |
| **纯手动** | 无（显示 —） | 面板确认后扣件 | 调用 `log_filter_change` 服务：扣 1 个 + 记历史一次完成 |

两种模式可以在同一个仓库混用；未装 Filter-Life-Tracker 时一切照常，只是没有寿命显示。绑定的 FLT 条目被删除时槽位显示「设备未在线或未装集成」，可在组编辑器里清除或改绑。

### 面板相关服务

- `stockroom.update_item`：编辑条目元数据（名称/分类/单位/低库存线/位置/材质/颜色/满卷重），只改传入字段：位置/材质/颜色传空字符串清除，名称不可清除（空串报错），单位传空字符串恢复分类默认，低库存线/满卷重不受空串影响——自动化也能用
- `stockroom.log_filter_change`：手动槽位换芯一条龙（`group_id` + `level` + `item`，可选 `life_pct` 备注），供自动化或面板外触发

离线开发座架：`dev/index.html`（stub hass + fixture），`python dev/serve.py` 后浏览器打开 `http://localhost:8766` 即可在游戏外调整面板 UI。

## 拍照扫描入库（V0.2.0）

选项里开启「拍照扫描入库」后，把照片 POST 到 webhook 即可获得条目建议：

1. **配置**：集成选项 → 开启扫描 → 填 OpenAI 兼容端点（默认智谱 `https://open.bigmodel.cn/api/paas/v4` + 模型 `glm-4.5v`）、API Key；可选选一台装了 HA 伴侣 App 的手机作为「确认手机」
2. **webhook 地址**：两个汇总传感器的 `scan_webhook_url` 属性（永远给内网地址），形如 `http://<HA内网地址>:8123/api/webhook/stockroom-<entry_id>`；webhook 仅限局域网访问（`local_only` 为内置行为，手机不在家时需走回家网络/VPN）
3. **手机端**（iOS 快捷指令示例）：
   - 拍照（或选相册）→ 「Base64 编码」
   - 「获取 URL 内容」：POST，JSON 体 `{"image_base64": "<上一步输出>"}`
   - 返回值就是识别建议 JSON；同时集成会发带「✓ 入库 / ✕ 忽略」按钮的通知到确认手机
4. **确认**：点通知按钮（按建议值直接入库），或调服务确认并可覆盖字段：

```yaml
action: stockroom.scan_confirm
data:
  suggestion: "<建议ID>"
  quantity: 250      # 可选，覆盖识别值
  location: 柜2      # 可选
```

**识别边界**（诚实预期）：拍**标签/包装/收纳盒**（品牌、规格、色号、净重）最可靠；散装螺丝的规格视觉上无法可靠分辨——prompt 已要求模型把不确定的规格猜测写进名称并把数量置 0，确认时记得核对。图片格式支持 JPEG/PNG/GIF/WebP（iPhone 捷径里先加一步「转换图像」为 JPEG）；照片仅用于本次识别，不落盘不持久化。待确认队列每仓库上限 50 条（超出淘汰最旧），图片上限 11MB（base64 膨胀 ×4/3 后须低于 HA 16MB 请求上限，超限时调整快捷指令的图像压缩质量）。

## AMS 自动扣料说明

针对 greghesp/ha-bambulab 集成（domain `bambu_lab`），在 HA 2026.1.3 + P1S 上实测：

1. 选项里选择打印机的 AMS 料盘传感器（如 `sensor.p1s_xxx_ams_1_tray_2`），并映射到按 **g** 记账的耗材条目
2. 集成监听同前缀的 `..._print_status`；打印完成变为 `finish` 时，读取 `..._print_weight` 属性里形如 `"AMS 1 Tray 2": 65.28` 的分盘克数，与映射里料盘实体 id 的尾段（`ams_1_tray_2`）匹配后逐盘扣减
3. **只在真实完成时扣**：只有 `running/pause/prepare/slicing/init → finish` 的转移才扣料；HA 重启重放（`None → finish`）与打印机掉线重连（`offline → finish`）一律忽略
4. **幂等**：每次扣减按「仓库 | 打印机」记录 `task_name` + `start_time`，同样的任务重复出现（HA 重载、实体状态重放）不会重复扣；任务身份读不到时**宁漏勿重**（记 WARNING 不扣减，之后可用 `stocktake` 校准）
5. AMS HT（键形如 `AMS HT 1`）与外挂料盘（externalspool）V1 不支持自动扣减，遇到时记 WARNING 提示
6. 扣减同样走 `stockroom_item_changed` 事件（note 形如 "AMS 自动扣料：xxx.gcode.3mf"），异常只记日志、绝不影响打印机集成

> 建议：需要 AMS 自动扣料的耗材条目单位用 `g`、初始数量用克数（如满卷 1000），低库存线也按克设置。映射了非 g 单位的条目会在日志里提醒。

## FAQ

**Q：数据存在哪里？会不会进 config entry？**
A：存在 `homeassistant` 配置目录的 `.storage/stockroom.storage`，config entry 只存仓库名与选项。10 分钟批量落盘 + HA 停止时强刷，服务变动后会立即安排落盘。

**Q：条目 id 怎么来的？**
A：按名称 ASCII slugify 自动生成（`M3×8` → `m3x8`，`PLA-白色` → `pla-bai-se`），同仓库内冲突自动加 `-2`、`-3`；`add_item` 也可显式传 `item_id`。中文未收录的字会跳过，结果为空则退化为 `item-<哈希>`。

**Q：能不能低库存时让集成发通知？**
A：不发。请订阅 `binary_sensor` 或 `stockroom_item_changed` 事件，示例见上。

**Q：删掉仓库配置会怎样？**
A：实体随之移除，该仓库的条目表也从存储中清理。

**Q：多台拓竹打印机怎么办？**
A：每台打印机的料盘各自映射即可；幂等记录按「仓库 | 打印机」分开，多仓库映射同一台打印机也互不干扰（各自条目各自扣）。

**Q：映射的条目被删了会怎样？**
A：自动扣料时记 WARNING 提示映射悬空，不会误扣其他条目；重新打印前请到集成选项里更新映射。选项里手输的条目 id 会在保存时校验，不存在的 id 直接报错。

## Changelog

### 0.3.1（2026-10-07）

- `remove_item` 事件/广播收口进 `_after_mutation`，与 add/update/restock 同序同路径；`stockroom_item_changed` 的 remove 事件 `new_quantity` 由 `null` 改为携带被删时数量（审查台账 SR-P3-1，复验确认无破坏性消费方）
- `schedule_store_flush` 分支判据改为 `get_running_loop() is hass.loop`（identity），非 loop 线程的协程创建与任务调度全部移交 loop 侧，消除异 loop 线程 RuntimeError 边界与 never-awaited 泄漏（SR-P3-2）
- 两条均为存量 P3 按新规「P3 一律修复不延后」返修；wb 复验 0 新发现收口

### 0.3.0（2026-10-06）

- **仓管面板**：侧边栏 panel_custom 全功能管理面板（库存卡片/条目 CRUD/快捷消耗补货盘点），零构建 Web Component，`/api/stockroom/panel/*` 三视图聚合接口（`requires_auth` + 管理员）
- **图形化滤芯槽位**：多级槽位映射库存条目，双模式——绑定 Filter-Life-Tracker 设备（寿命% 展示 + reset 事件自动扣件）或纯手动（`log_filter_change` 扣件+记历史一条龙）
- **更换历史**：`meta.filter_history` append-only 记录（换下时寿命尽力采集），FIFO 500 条上限
- 新服务 `update_item`（条目元数据编辑）与 `log_filter_change`；槽位配置存 `meta.filter_slots`，不升存储版本
- 事件联动的 handler 用同步 `@callback`：`async_fire` 内联执行保证读到的是换下前寿命
- FLT 为可选软依赖：未装/条目悬空一律优雅降级，不 import 其代码

### 0.2.3（2026-10-02）

- 根因修复：实体广播回调补 `@callback`（dispatcher 会把非 callback 目标丢进线程池，HA 2026 对非循环线程写状态硬性报错——条目实体不出现/状态不刷新的真凶）
- 回退上一版的广播延迟，恢复阻塞式服务调用「返回即已写入」语义
- 识别建议数值负数钳 0；data URL MIME 按实际图片容器声明；webhook URL 无内网地址时防护性降级

### 0.2.1（2026-10-02）

- 修复 SSL 实例无 internal_url 时集成无法加载；识别负数钳制；AMS HT/外挂盘键告警

### 0.2.0（2026-10-02）

- **拍照扫描入库**：webhook 收照片 → OpenAI 兼容视觉模型识别 → 待确认队列 → 伴侣 App 通知按钮 / `scan_confirm`、`scan_dismiss` 服务确认；照片即用即弃
- 条目总数传感器新增 `scan_webhook_url` 属性
- 修复：动态实体 diff 未登记 known 导致的重复添加日志与删除失效（两轮跨模型审查收口）

### 0.1.0（2026-10-02）

- 首个版本：虚拟仓库设备、动态条目实体（数量 sensor + 低库存 binary_sensor + 汇总×2）
- 六个库存服务（add_item / remove_item / consume / restock / stocktake / set_threshold）
- `stockroom_item_changed` 事件与低库存判定（数量 ≤ 阈值）
- 拓竹 AMS 打印完成按分盘克数自动扣料（任务级幂等）
- JSON 存储版本迁移钩子、10 分钟批量落盘 + 停机强刷
