/**
 * Stockroom panel — 仓管面板 (stockroom-panel).
 *
 * Zero-build vanilla Web Component loaded by the HA frontend as a
 * panel_custom module. Data flows:
 *   - overview (aggregated snapshot + slot config + FLT discovery) comes
 *     from GET /api/stockroom/panel/overview via hass.fetchWithAuth;
 *   - all item mutations go through the stockroom.* domain services via
 *     hass.callService — the panel invents no mutation endpoints;
 *   - FLT-bound slot changes press the FLT reset button and let the
 *     reset event drive deduction; manual slots call log_filter_change.
 *
 * All user-supplied strings are rendered through textContent (see el()).
 */

const API_OVERVIEW = "/api/stockroom/panel/overview";
const API_SLOTS = "/api/stockroom/panel/slots";
const API_HISTORY = "/api/stockroom/panel/history";
const API_SCAN = "/api/stockroom/panel/scan";
const API_BAMBU_SYNC = "/api/stockroom/panel/bambu_sync";
const STATIC_BASE = "/stockroom";

const POLL_MS = 5000;

// Spool states that mean "nothing usable loaded" — no bind/create offered.
const EMPTY_SPOOL_NAMES = new Set(["", "?", "empty", "unknown", "unavailable"]);

const I18N = {
  "zh-Hans": {
    panelTitle: "仓管面板",
    tabInventory: "库存",
    tabSlots: "滤芯槽位",
    tabHistory: "更换历史",
    addItem: "新增条目",
    editItem: "编辑条目",
    consume: "消耗",
    restock: "补货",
    stocktake: "盘点",
    remove: "删除",
    edit: "编辑",
    change: "换芯",
    total: "条目总数",
    lowStock: "低库存",
    lowStockBadge: "低库存",
    name: "名称",
    category: "分类",
    catFilament: "打印耗材",
    catScrew: "螺丝/紧固件",
    catOther: "其它",
    unit: "单位",
    lowThreshold: "低库存线",
    location: "存放位置",
    material: "材质",
    color: "颜色",
    fullWeight: "满卷净重 (g)",
    note: "备注",
    amount: "数量",
    actual: "实际数量",
    cancel: "取消",
    confirm: "确认",
    deleteItemTitle: "删除条目",
    deleteItemText: "确定删除条目「{name}」？对应实体将一并消失，历史不受影响。",
    emptyWarehouse: "该仓库暂无条目，点右上角「新增条目」开始。",
    noWarehouse: "尚无仓库。请先在 HA 中添加 Stockroom 集成并创建仓库。",
    lastStocktake: "最近盘点",
    neverStocktake: "未盘点",
    addGroup: "新增槽位组",
    defaultGroupName: "滤芯组",
    editGroup: "编辑槽位组",
    groupName: "组名",
    levelN: "{n} 级",
    slotBindNone: "不绑定（仅记录更换）",
    deleteGroup: "删除组",
    deleteGroupText: "确定删除槽位组「{name}」？更换历史保留。",
    changeFilterTitle: "更换滤芯",
    changeFilterManual: "条目「{item}」扣减 1 并写入更换历史。",
    tabScan: "图片识别",
    manualGroup: "纯手动",
    sourceEntity: "水源实体（寿命追踪源，可留空=纯手动）",
    sourceHint: "搜索或输入实体 ID",
    sourceType: "积分类型",
    sourceDuration: "按时长（用时积分）",
    sourceCount: "按次数（启用计数）",
    targetState: "目标状态",
    targetStateRequired: "绑定水源实体时必须填写目标状态",
    entityNotFound: "实体不存在：请从列表选择",
    debounce: "去抖秒数",
    levels: "滤芯级（额定参数决定寿命算法）",
    ratedDays: "额定天数",
    ratedUsage: "额定用量",
    warnThreshold: "预警阈值%",
    cascadeHint: "级联系数",
    cascade: "级联系数",
    addLevel: "加一级",
    scanNotEnabled: "拍照扫描未启用：请到集成配置项的选项里开启「拍照扫描入库」并填写识别 API key（默认智谱 glm-4.5v，兼容任意 OpenAI 接口）。",
    scanCamera: "拍照",
    scanUpload: "上传图片",
    scanRecognize: "识别",
    scanTooLarge: "图片超过 11MB 上限",
    scanQueued: "识别完成，{n} 条建议已入待确认队列",
    scanFormatHint: "支持 JPEG/PNG/GIF/WebP，最大 11MB；拍标签/包装最可靠",
    scanPendingTitle: "待确认建议",
    scanPendingEmpty: "队列为空",
    scanSuggestionMeta: "{q}{u} · {cat} · {loc}",
    scanConfirm: "入库",
    scanDismiss: "忽略",
    spoolTitle: "打印机料盘",
    spoolSync: "同步参数",
    spoolStaleOther: "参数已变（其他仓库）",
    spoolCreate: "新建并绑定",
    spoolBind: "绑定已有",
    spoolCreateTitle: "从{label}新建条目",
    spoolBindTitle: "把{label}绑定到已有条目",
    spoolAutoNote: "材质与颜色将自动写入条目。",
    itemLabel: "条目",
    spoolLabel: "AMS {ams} 料盘 {n}",
    spoolBindNote: "仅建立映射；绑定后可用「同步参数」把料盘参数写入条目。",
    noBoundItem: "该槽位未绑定条目，先编辑组补上绑定才能换芯。",
    changedOk: "更换指令已发出，扣件与历史稍后自动完成。",
    slotsEmpty: "还没有槽位组。槽位组把一台设备的多级滤芯映射到仓库条目：寿命到点换芯时自动扣件、留更换记录。",
    historyEmpty: "暂无更换记录。换芯（无论自动或手动）后这里会留档。",
    hTime: "时间",
    hWarehouse: "仓库",
    hGroup: "槽位组",
    hLevel: "级",
    hItem: "条目",
    hLife: "换下时寿命",
    hMode: "方式",
    modeFlt: "自动联动",
    modeManual: "手动",
    loading: "加载中…",
    allWarehouses: "全部仓库",
    allGroups: "全部槽位组",
    slotStock: "库存 {q}{u}",
    slotUnbound: "未绑定条目",
    stocktakeTitle: "盘点「{name}」",
    qtyDialogTitle: "{action}「{name}」",
    saved: "已保存",
  },
  en: {
    panelTitle: "Stockroom",
    tabInventory: "Inventory",
    tabSlots: "Filter slots",
    tabHistory: "History",
    addItem: "Add item",
    editItem: "Edit item",
    consume: "Consume",
    restock: "Restock",
    stocktake: "Stocktake",
    remove: "Delete",
    edit: "Edit",
    change: "Change",
    total: "Items",
    lowStock: "Low stock",
    lowStockBadge: "LOW",
    name: "Name",
    category: "Category",
    catFilament: "Filament",
    catScrew: "Screws",
    catOther: "Other",
    unit: "Unit",
    lowThreshold: "Low threshold",
    location: "Location",
    material: "Material",
    color: "Color",
    fullWeight: "Full weight (g)",
    note: "Note",
    amount: "Amount",
    actual: "Actual quantity",
    cancel: "Cancel",
    confirm: "OK",
    deleteItemTitle: "Delete item",
    deleteItemText: "Delete item \"{name}\"? Its entities disappear; history is kept.",
    emptyWarehouse: "No items yet. Use \"Add item\" to start.",
    noWarehouse: "No warehouse yet. Add the Stockroom integration first.",
    lastStocktake: "Last stocktake",
    neverStocktake: "never",
    addGroup: "Add slot group",
    defaultGroupName: "Filter group",
    editGroup: "Edit slot group",
    groupName: "Group name",
    levelN: "Level {n}",
    slotBindNone: "None (record only)",
    deleteGroup: "Delete group",
    deleteGroupText: "Delete slot group \"{name}\"? History is kept.",
    changeFilterTitle: "Change filter",
    changeFilterManual: "Deducts 1 from \"{item}\" and writes a history record.",
    tabScan: "Image scan",
    manualGroup: "manual",
    sourceEntity: "Source entity (life tracking source; empty = manual)",
    sourceHint: "Search or type an entity ID",
    sourceType: "Integration type",
    sourceDuration: "Duration (time in state)",
    sourceCount: "Count (activations)",
    targetState: "Target state",
    targetStateRequired: "Target state is required when a source entity is set",
    entityNotFound: "Unknown entity: pick one from the list",
    debounce: "Debounce seconds",
    levels: "Filter levels (ratings drive the life math)",
    ratedDays: "Rated days",
    ratedUsage: "Rated usage",
    warnThreshold: "Warn threshold %",
    cascadeHint: "cascade",
    cascade: "Cascade factor",
    addLevel: "Add level",
    scanNotEnabled: "Photo scanning is not enabled: turn it on in the integration options and fill in the vision API key (Zhipu glm-4.5v by default; any OpenAI-compatible endpoint works).",
    scanCamera: "Camera",
    scanUpload: "Upload image",
    scanRecognize: "Recognize",
    scanTooLarge: "Image exceeds the 11MB limit",
    scanQueued: "Recognized — {n} suggestion(s) queued",
    scanFormatHint: "JPEG/PNG/GIF/WebP up to 11MB; labels and packaging work best",
    scanPendingTitle: "Pending suggestions",
    scanPendingEmpty: "Queue is empty",
    scanSuggestionMeta: "{q}{u} · {cat} · {loc}",
    scanConfirm: "Add",
    scanDismiss: "Dismiss",
    spoolTitle: "Printer spools",
    spoolSync: "Sync params",
    spoolStaleOther: "stale (other warehouse)",
    spoolCreate: "Create & bind",
    spoolBind: "Bind",
    spoolCreateTitle: "Create item from {label}",
    spoolBindTitle: "Bind {label} to an item",
    spoolAutoNote: "Material and color are written to the item automatically.",
    itemLabel: "Item",
    spoolLabel: "AMS {ams} Spool {n}",
    spoolBindNote: "Maps only; use \"Sync params\" afterwards to write the spool parameters into the item.",
    noBoundItem: "This slot has no bound item; edit the group first.",
    changedOk: "Change issued; deduction and history will follow in seconds.",
    slotsEmpty: "No slot groups yet. A group maps each filter level of a device to a warehouse item: changing a filter deducts stock and logs history.",
    historyEmpty: "No records yet. Filter changes (auto or manual) will appear here.",
    hTime: "Time",
    hWarehouse: "Warehouse",
    hGroup: "Group",
    hLevel: "Lvl",
    hItem: "Item",
    hLife: "Life at change",
    hMode: "Mode",
    modeFlt: "Auto",
    modeManual: "Manual",
    loading: "Loading…",
    allWarehouses: "All warehouses",
    allGroups: "All groups",
    slotStock: "stock {q}{u}",
    slotUnbound: "unbound",
    stocktakeTitle: "Stocktake \"{name}\"",
    qtyDialogTitle: "{action} \"{name}\"",
    saved: "Saved",
  },
};

const CATEGORY_META = {
  filament: { icon: "mdi:printer-3d-nozzle-nozzle", catKey: "catFilament" },
  screw: { icon: "mdi:screw-larger", catKey: "catScrew" },
  other: { icon: "mdi:package-variant-closed", catKey: "catOther" },
};

function t(key, params) {
  const lang =
    (document.querySelector("home-assistant")?.hass?.language || "zh-Hans");
  const dict = I18N[lang] && I18N[lang][key] !== undefined ? I18N[lang] : I18N["zh-Hans"];
  let text = dict[key] !== undefined ? dict[key] : I18N["zh-Hans"][key] || key;
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      text = text.split(`{${k}}`).join(String(v));
    }
  }
  return text;
}

/** DOM builder: every string child becomes a text node (XSS-safe). */
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") {
        node.addEventListener(key.slice(2), value);
      } else node.setAttribute(key, value);
    }
  }
  for (const child of children) {
    if (child === undefined || child === null) continue;
    node.append(child);
  }
  return node;
}

function fmtQty(value) {
  const num = Number(value) || 0;
  return Number.isInteger(num) ? String(num) : String(Math.round(num * 1000) / 1000);
}

function lifeClass(pct) {
  if (pct === null || pct === undefined || Number.isNaN(pct)) return "none";
  if (pct <= 20) return "bad";
  if (pct <= 40) return "warn";
  return "good";
}

function fmtLife(pct) {
  if (pct === null || pct === undefined || Number.isNaN(pct)) return "—";
  return `${Math.round(pct)}%`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString();
}

function uuid() {
  return (crypto.randomUUID ? crypto.randomUUID() : `g${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`);
}

class StockroomPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._data = null; // overview payload
    this._history = null; // history payload
    this._view = "inventory";
    this._warehouse = null; // selected warehouse entry_id
    this._histFilter = { warehouse: "", group_id: "" };
    this._timer = null;
    this._loading = true;
    this._error = null;
    this._dialog = null;
  }

  set hass(value) {
    const firstAssignment = !this._hass;
    this._hass = value;
    // HA may inject hass after the element is already connected; the first
    // poll (started in connectedCallback) would have failed with "hass not
    // ready" — refresh right away instead of waiting out the 5 s timer.
    if (firstAssignment && !this._data && this.isConnected) {
      this._refresh();
      return;
    }
    // Later hass pushes arrive for entity churn; our own data is poll-driven.
    if (this._data && this._dialog === null) this._render();
  }

  get hass() {
    return this._hass;
  }

  set route(value) {
    // Deep-link support: /stockroom-panel/slots opens the slots tab.
    const path = value && value.path ? value.path : "";
    if (path.includes("slots")) this._view = "slots";
    else if (path.includes("history")) this._view = "history";
    else if (path.includes("scan")) this._view = "scan";
  }

  connectedCallback() {
    const root = this.shadowRoot;
    root.innerHTML = "";
    root.append(
      el("link", { rel: "stylesheet", href: `${STATIC_BASE}/style.css` }),
      el("div", { class: "srp-root" })
    );
    this._refresh();
    this._timer = setInterval(() => {
      if (!document.hidden && this.isConnected) this._refresh();
    }, POLL_MS);
  }

  disconnectedCallback() {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
    this._closeDialog();
  }

  // ---------------------------------------------------------------
  // Data access
  // ---------------------------------------------------------------

  async _fetch(path, options) {
    if (!this._hass) throw new Error("hass not ready");
    const response = await this._hass.fetchWithAuth(path, options);
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload || payload.ok === false) {
      throw new Error((payload && payload.error) || `HTTP ${response.status}`);
    }
    return payload;
  }

  async _refresh() {
    try {
      this._data = await this._fetch(API_OVERVIEW);
      this._error = null;
      if (this._warehouse === null || !this._warehouseExists(this._warehouse)) {
        this._warehouse = this._data.warehouses[0]?.entry_id ?? null;
      }
      if (this._view === "history") await this._loadHistory();
    } catch (err) {
      this._error = String(err.message || err);
    }
    this._loading = false;
    // 扫描视图有未处理的选图时跳过轮询重绘，否则 5s 一刷会把预览和
    // 识别按钮抹掉（SR-F-029）——但仅在页面上确实已渲染过扫描视图时：
    // 元素断开重连后 shadowRoot 是空壳，跳过会留白（R2 待确认②）。
    if (
      this._view === "scan" &&
      this._scanImage &&
      this.shadowRoot.querySelector(".srp-scan-upload")
    ) {
      return;
    }
    this._render();
  }

  _warehouseExists(entryId) {
    return (this._data?.warehouses || []).some((w) => w.entry_id === entryId);
  }

  _currentWarehouse() {
    return (this._data?.warehouses || []).find((w) => w.entry_id === this._warehouse) || null;
  }

  _slotGroups(warehouseId) {
    return (this._data?.filter_slots || {})[warehouseId] || [];
  }

  async _loadHistory() {
    const params = new URLSearchParams({ limit: "500" });
    if (this._histFilter.warehouse) params.set("warehouse", this._histFilter.warehouse);
    if (this._histFilter.group_id) params.set("group_id", this._histFilter.group_id);
    this._history = await this._fetch(`${API_HISTORY}?${params.toString()}`);
  }

  async _svc(domain, service, data) {
    await this._hass.callService(domain, service, data);
  }

  // ---------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------

  _render() {
    const root = this.shadowRoot;
    const container = root.querySelector(".srp-root");
    if (!container) return;
    container.innerHTML = "";
    container.append(this._renderTopbar());
    if (this._error) {
      container.append(el("div", { class: "srp-error-banner", text: this._error }));
    }
    if (this._loading) {
      container.append(el("div", { class: "srp-hint", text: t("loading") }));
      return;
    }
    if (!this._data) return;
    if (!this._data.warehouses.length) {
      container.append(el("div", { class: "srp-hint", text: t("noWarehouse") }));
      return;
    }
    if (this._view === "inventory") this._renderInventory(container);
    else if (this._view === "slots") this._renderSlots(container);
    else if (this._view === "scan") this._renderScan(container);
    else this._renderHistory(container);
  }

  _renderTopbar() {
    const warehouses = this._data?.warehouses || [];
    const chipRow = el("div", { class: "srp-warehouse-row" });
    if (warehouses.length > 1) {
      for (const wh of warehouses) {
        chipRow.append(
          el(
            "button",
            {
              class: `srp-chip${wh.entry_id === this._warehouse ? " active" : ""}`,
              onclick: () => {
                this._warehouse = wh.entry_id;
                this._render();
              },
            },
            wh.name
          )
        );
      }
    } else if (warehouses.length === 1) {
      chipRow.append(el("span", { class: "srp-hint", text: warehouses[0].name }));
    }

    const tab = (view, key) =>
      el(
        "button",
        {
          class: `srp-tab${this._view === view ? " active" : ""}`,
          onclick: async () => {
            this._view = view;
            if (view === "history" && !this._history) await this._loadHistory().catch(() => {});
            this._render();
          },
        },
        t(key)
      );

    return el(
      "div",
      { class: "srp-topbar" },
      el(
        "div",
        { class: "srp-title-row" },
        el(
          "h1",
          { class: "srp-title" },
          el("ha-icon", { icon: "mdi:warehouse" }),
          t("panelTitle")
        ),
        el(
          "div",
          { class: "srp-tabs" },
          tab("inventory", "tabInventory"),
          tab("slots", "tabSlots"),
          tab("scan", "tabScan"),
          tab("history", "tabHistory")
        )
      ),
      chipRow
    );
  }

  // ---------------- inventory view ----------------

  _renderInventory(container) {
    const wh = this._currentWarehouse();
    if (!wh) return;
    const summary = wh.summary || {};
    const summaryRow = el(
      "div",
      { class: "srp-summary" },
      el("span", { text: `${t("total")}: ${summary.total ?? 0}` }),
      el("span", {
        class: summary.low_stock ? "srp-low-flag" : undefined,
        text: `${t("lowStock")}: ${summary.low_stock ?? 0}`,
      })
    );

    const head = el(
      "div",
      { class: "srp-section-head" },
      el("h2", { text: wh.name }),
      el("span", { style: "flex:1" }),
      el(
        "button",
        {
          class: "srp-btn primary",
          onclick: () => this._openItemDialog(null),
        },
        el("ha-icon", { icon: "mdi:plus" }),
        t("addItem")
      )
    );

    container.append(summaryRow, head);

    const bambuBlock = this._renderBambuBlock(wh);
    if (bambuBlock) container.append(bambuBlock);

    const itemIds = Object.keys(wh.items || {});
    if (!itemIds.length) {
      container.append(el("div", { class: "srp-hint", text: t("emptyWarehouse") }));
      return;
    }
    const grid = el("div", { class: "srp-grid" });
    for (const id of itemIds) grid.append(this._renderItemCard(wh, wh.items[id]));
    container.append(grid);
  }

  // ---------------- bambu spool sync block (打印机料盘) ----------------

  _spoolSwatch(tray) {
    // Fresh node per row: el() appends (moves) nodes, so a shared swatch
    // would vanish from all but the last row (SR-F-062).
    const color = (tray.color || "").slice(0, 7);
    return tray.color_trusted && tray.color
      ? el("span", { class: "srp-spool-swatch", style: `background:${color}` })
      : el("span", { class: "srp-spool-swatch empty" });
  }

  _renderBambuBlock(wh) {
    const trays = this._data?.bambu_trays || [];
    if (!trays.length) return null;
    const rows = [];
    for (const tray of trays) {
      const color = (tray.color || "").slice(0, 7);
      const label = t("spoolLabel", { ams: tray.ams_no, n: tray.tray_no });
      if (!tray.owners.length) {
        const usable = tray.name && !EMPTY_SPOOL_NAMES.has(tray.name.toLowerCase());
        const row = el(
          "div",
          { class: "srp-spool-row" },
          this._spoolSwatch(tray),
          el("span", { class: "srp-spool-label", text: label }),
          el("span", { class: "srp-spool-name", text: tray.name || "—" }),
          tray.type ? el("span", { class: "srp-hint", text: tray.type }) : null,
          el("span", { style: "flex:1" })
        );
        if (usable) {
          row.append(
            el(
              "button",
              {
                class: "srp-btn small primary",
                onclick: () => this._bindSpool(wh, tray, "create"),
              },
              t("spoolCreate")
            ),
            el(
              "button",
              { class: "srp-btn small", onclick: () => this._bindSpool(wh, tray, "bind") },
              t("spoolBind")
            )
          );
        }
        rows.push(row);
        continue;
      }
      // One row per binding: a spool CAN be mapped in several warehouses —
      // hiding the non-first owners was the SR-F-055 defect.
      for (const owner of tray.owners) {
        const row = el(
          "div",
          { class: "srp-spool-row" },
          this._spoolSwatch(tray),
          el("span", { class: "srp-spool-label", text: label }),
          el("span", { class: "srp-spool-name", text: tray.name || "—" }),
          tray.type ? el("span", { class: "srp-hint", text: tray.type }) : null,
          el("span", { style: "flex:1" }),
          el("span", {
            class: "srp-spool-bound",
            text: `→ ${owner.item_name || owner.item_id}`,
          })
        );
        if (owner.needs_update) {
          if (owner.entry_id === wh.entry_id) {
            row.append(
              el(
                "button",
                {
                  class: "srp-btn small",
                  onclick: async () => {
                    try {
                      // type/color only when the spool actually carries
                      // them — update_item clears on empty strings
                      // (SR-F-058); untrusted colors are never written
                      // (SR-F-056).
                      const data = { entry_id: wh.entry_id, item: owner.item_id };
                      if (tray.type) data.material = tray.type;
                      if (tray.color_trusted && color) data.color = color;
                      await this._svc("stockroom", "update_item", data);
                      this._toast(t("saved"));
                    } catch (err) {
                      this._toast(String(err.message || err), true);
                    }
                    await this._refresh();
                  },
                },
                t("spoolSync")
              )
            );
          } else {
            row.append(el("span", { class: "srp-hint", text: t("spoolStaleOther") }));
          }
        }
        rows.push(row);
      }
    }
    return el(
      "div",
      { class: "srp-card srp-spool-block" },
      el(
        "div",
        { class: "srp-group-head" },
        el("span", { class: "srp-group-name", text: t("spoolTitle") })
      ),
      ...rows
    );
  }

  _bindSpool(wh, tray, action) {
    const isCreate = action === "create";
    // 中文色名进名称，hex 只进条目数据（用户要求，SR 批 V0.4.1）
    const nameInput = isCreate
      ? this._input(`${tray.name} ${tray.color_name || ""}`.trim())
      : null;
    const itemSelect = isCreate ? null : (() => {
      const select = el("select");
      for (const item of Object.values(wh.items || {})) {
        select.append(
          el("option", { value: item.id, text: `${item.name} (${item.id})` })
        );
      }
      return select;
    })();
    const children = isCreate
      ? [
          el("div", { class: "srp-hint", text: t("spoolAutoNote") }),
          this._field(t("name"), nameInput),
        ]
      : [el("div", { class: "srp-hint", text: t("spoolBindNote") }), this._field(t("itemLabel"), itemSelect)];
    const label = t("spoolLabel", { ams: tray.ams_no, n: tray.tray_no });
    this._openDialog((dialog) => {
      this._dialogFrame(
        dialog,
        isCreate ? t("spoolCreateTitle", { label }) : t("spoolBindTitle", { label }),
        children,
        async () => {
          await this._fetch(API_BAMBU_SYNC, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              entry_id: wh.entry_id,
              tray_entity: tray.entity_id,
              action,
              item_id: isCreate ? undefined : itemSelect.value,
              // The user-edited name must reach the server (SR-F-053).
              name: isCreate ? nameInput.value.trim() : undefined,
            }),
          });
          // The mapping write reloads the integration; refresh lazily so
          // the overview comes back with the new binding.
          this._toast(t("saved"));
          setTimeout(() => this._refresh(), 2500);
        }
      );
    });
  }

  _renderItemCard(wh, item) {
    const meta = CATEGORY_META[item.category] || CATEGORY_META.other;
    const extraBits = [];
    if (item.extra?.material) extraBits.push(item.extra.material);
    if (item.extra?.color) extraBits.push(item.extra.color);
    if (item.location) extraBits.push(`📍 ${item.location}`);
    if (item.last_stocktake) {
      extraBits.push(`${t("lastStocktake")} ${fmtTime(item.last_stocktake)}`);
    } else {
      extraBits.push(`${t("lastStocktake")}: ${t("neverStocktake")}`);
    }

    const actionBtn = (icon, key, handler) =>
      el(
        "button",
        { class: "srp-btn small", onclick: handler },
        el("ha-icon", { icon }),
        t(key)
      );

    return el(
      "div",
      { class: `srp-card srp-item-card${this._isLow(item) ? " low" : ""}` },
      this._isLow(item) ? el("span", { class: "srp-low-badge", text: t("lowStockBadge") }) : null,
      el(
        "div",
        { class: "srp-item-head" },
        el("ha-icon", { icon: meta.icon }),
        el("span", { class: "srp-item-name", text: item.name, title: item.name })
      ),
      el(
        "div",
        { class: "srp-item-qty" },
        fmtQty(item.quantity),
        el("small", { text: item.unit || "" })
      ),
      el("div", {
        class: "srp-item-meta",
        text: extraBits.join(" · "),
        title: extraBits.join(" · "),
      }),
      el(
        "div",
        { class: "srp-item-actions" },
        actionBtn("mdi:arrow-down-bold-box-outline", "consume", () =>
          this._openQtyDialog("consume", item)
        ),
        actionBtn("mdi:arrow-up-bold-box-outline", "restock", () =>
          this._openQtyDialog("restock", item)
        ),
        actionBtn("mdi:clipboard-check-outline", "stocktake", () =>
          this._openQtyDialog("stocktake", item)
        ),
        actionBtn("mdi:pencil", "edit", () => this._openItemDialog(item)),
        actionBtn("mdi:delete-outline", "remove", () => this._confirmRemoveItem(item))
      )
    );
  }

  _isLow(item) {
    const qty = Number(item.quantity) || 0;
    const threshold = Number(item.low_threshold);
    return Number.isFinite(threshold) && qty <= threshold;
  }

  // ---------------- slots view ----------------

  _renderSlots(container) {
    const wh = this._currentWarehouse();
    if (!wh) return;
    const groups = this._slotGroups(wh.entry_id);
    const head = el(
      "div",
      { class: "srp-section-head" },
      el("h2", { text: t("tabSlots") }),
      el("span", { style: "flex:1" }),
      el(
        "button",
        {
          class: "srp-btn primary",
          onclick: () => this._openGroupDialog(null),
        },
        el("ha-icon", { icon: "mdi:plus" }),
        t("addGroup")
      )
    );
    container.append(head);
    if (!groups.length) {
      container.append(el("div", { class: "srp-hint", text: t("slotsEmpty") }));
      return;
    }
    for (const group of groups) container.append(this._renderGroupCard(wh, group));
  }

  _renderGroupCard(wh, group) {
    const tracked = !!group.source_entity;

    const chain = el("div", { class: "srp-slot-chain" });
    const levels = [...(group.levels || [])].sort((a, b) => a.level - b.level);
    levels.forEach((slot, index) => {
      if (index > 0) chain.append(el("span", { class: "srp-slot-arrow", text: "→" }));
      chain.append(this._renderSlot(wh, group, slot));
    });

    return el(
      "div",
      { class: "srp-card srp-group-card" },
      el(
        "div",
        { class: "srp-group-head" },
        el("span", { class: "srp-group-name", text: group.name }),
        el("span", {
          class: `srp-mode-badge${tracked ? "" : " manual"}`,
          text: tracked ? group.source_entity : t("manualGroup"),
          title: tracked ? group.source_entity : "",
        }),
        el(
          "span",
          { class: "srp-group-actions" },
          el(
            "button",
            { class: "srp-btn small", onclick: () => this._openGroupDialog(group) },
            el("ha-icon", { icon: "mdi:pencil" }),
            t("edit")
          ),
          el(
            "button",
            { class: "srp-btn small danger", onclick: () => this._confirmRemoveGroup(group) },
            el("ha-icon", { icon: "mdi:delete-outline" }),
            t("remove")
          )
        )
      ),
      chain
    );
  }

  _renderSlot(wh, group, slot) {
    const life = (group.life || {})[slot.level] || null;
    const lifePct = life ? life.pct : null;
    const item = slot.item_id ? wh.items[slot.item_id] : null;
    const low = item && this._isLow(item);

    const changeBtn = el(
      "button",
      {
        class: "srp-btn small primary",
        disabled: item ? undefined : "disabled",
        title: item ? undefined : t("noBoundItem"),
        onclick: () => this._changeFilter(group, slot, item),
      },
      el("ha-icon", { icon: "mdi:filter-refresh" }),
      t("change")
    );

    return el(
      "div",
      { class: "srp-slot" },
      el(
        "div",
        { class: "srp-slot-head" },
        el("span", { class: "srp-level-badge", text: t("levelN", { n: slot.level }) }),
        life?.expired
          ? el("ha-icon", { icon: "mdi:alert-circle", style: "color:var(--error-color,#db4437)" })
          : null
      ),
      el("div", {
        class: `srp-life ${life ? lifeClass(lifePct) : "none"}`,
        text: life ? fmtLife(lifePct) : "—",
      }),
      el("div", {
        class: "srp-slot-item",
        text: item ? item.name : t("slotUnbound"),
        title: item ? item.name : undefined,
      }),
      el("div", {
        class: `srp-slot-stock${low ? " danger" : ""}`,
        text: item
          ? t("slotStock", { q: fmtQty(item.quantity), u: item.unit || "" })
          : "",
      }),
      el("div", { class: "srp-slot-actions" }, changeBtn)
    );
  }

  _changeFilter(group, slot, item) {
    if (!item) return;
    // 换芯 = 一个动作三件事：重置该级寿命记账 + 扣绑定条目 + 记更换历史
    // （服务端 log_filter_change 原子完成）。
    const text = t("changeFilterManual", { item: item.name });
    const warehouseId = this._warehouse;
    this._openConfirm(t("changeFilterTitle"), text, async () => {
      try {
        await this._svc("stockroom", "log_filter_change", {
          entry_id: warehouseId,
          group_id: group.group_id,
          level: slot.level,
          item: item.id,
        });
        this._toast(t("changedOk"));
        setTimeout(() => this._refresh(), 2500);
      } catch (err) {
        this._toast(String(err.message || err), true);
      }
    });
  }

  // ---------------- scan view (拍照识别) ----------------

  _renderScan(container) {
    const wh = this._currentWarehouse();
    if (!wh) return;
    const scan = wh.scan || { enabled: false, pending: [] };
    container.append(
      el(
        "div",
        { class: "srp-section-head" },
        el("h2", { text: t("tabScan") }),
        el("span", { style: "flex:1" })
      )
    );

    if (!scan.enabled) {
      container.append(
        el(
          "div",
          { class: "srp-card srp-scan-setup" },
          el("div", { class: "srp-hint", text: t("scanNotEnabled") })
        )
      );
      return;
    }

    // Upload + recognize: two entry points (camera capture / file upload)
    // feeding the same handler.
    const handleFile = () => {
      const file = this._pendingFile;
      if (!file) return;
      if (file.size > 11 * 1024 * 1024) {
        this._toast(t("scanTooLarge"), true);
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        // State first, then rebuild: the declarative restore in
        // _renderScan shows the preview on the CURRENT dom (a poll
        // re-render during the async read may have rebuilt it already).
        this._scanImage = String(reader.result);
        this._render();
      };
      reader.readAsDataURL(file);
    };
    const cameraInput = el("input", {
      type: "file",
      accept: "image/*",
      capture: "environment",
      style: "display:none",
      onchange: () => {
        this._pendingFile = cameraInput.files && cameraInput.files[0];
        handleFile();
      },
    });
    const uploadInput = el("input", {
      type: "file",
      accept: "image/*",
      style: "display:none",
      onchange: () => {
        this._pendingFile = uploadInput.files && uploadInput.files[0];
        handleFile();
      },
    });
    const pickBtn = el(
      "button",
      { class: "srp-btn primary", onclick: () => cameraInput.click() },
      el("ha-icon", { icon: "mdi:camera" }),
      t("scanCamera")
    );
    const uploadBtn = el(
      "button",
      { class: "srp-btn primary", onclick: () => uploadInput.click() },
      el("ha-icon", { icon: "mdi:tray-arrow-up" }),
      t("scanUpload")
    );
    const recognizeBtn = el(
      "button",
      {
        class: "srp-btn primary",
        style: "display:none",
        onclick: async () => {
          if (!this._scanImage) return;
          recognizeBtn.disabled = "disabled";
          try {
            const payload = await this._fetch(API_SCAN, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                entry_id: wh.entry_id,
                image_base64: this._scanImage.split(",")[1] || "",
              }),
            });
            this._toast(
              t("scanQueued", { n: (payload.suggestions || []).length })
            );
          } catch (err) {
            this._toast(String(err.message || err), true);
          } finally {
            recognizeBtn.disabled = undefined;
            this._scanImage = null;
            await this._refresh();
          }
        },
      },
      el("ha-icon", { icon: "mdi:auto-fix" }),
      t("scanRecognize")
    );
    const preview = el("img", { class: "srp-scan-preview", alt: "" });
    const previewWrap = el(
      "div",
      { class: "srp-scan-preview-row", style: "display:none" },
      preview,
      recognizeBtn
    );
    // Declarative restore: a poll re-render between picking a file and the
    // FileReader finishing would otherwise rebuild this subtree and strand
    // the preview inside detached nodes.
    if (this._scanImage) {
      preview.src = this._scanImage;
      previewWrap.style.display = "";
      recognizeBtn.style.display = "";
    }
    container.append(
      el(
        "div",
        { class: "srp-card srp-scan-upload" },
        el("div", { class: "srp-scan-actions" }, pickBtn, uploadBtn, cameraInput, uploadInput),
        previewWrap,
        el("div", { class: "srp-hint", text: t("scanFormatHint") })
      )
    );

    // Pending suggestions queue
    container.append(el("h3", { text: t("scanPendingTitle") }));
    const pending = scan.pending || [];
    if (!pending.length) {
      container.append(el("div", { class: "srp-hint", text: t("scanPendingEmpty") }));
    } else {
      const list = el("div", { class: "srp-scan-pending" });
      for (const suggestion of pending) {
        list.append(
          el(
            "div",
            { class: "srp-card srp-scan-item" },
            el(
              "div",
              { class: "srp-scan-item-main" },
              el("span", { class: "srp-scan-name", text: suggestion.name }),
              el("span", {
                class: "srp-hint",
                text: t("scanSuggestionMeta", {
                  q: fmtQty(suggestion.quantity || 0),
                  u: suggestion.unit || "",
                  cat: suggestion.category || "",
                  loc: suggestion.location || "",
                }),
              })
            ),
            el(
              "div",
              { class: "srp-scan-item-actions" },
              el(
                "button",
                {
                  class: "srp-btn small primary",
                  onclick: async () => {
                    try {
                      await this._svc("stockroom", "scan_confirm", {
                        entry_id: wh.entry_id,
                        suggestion: suggestion.id,
                      });
                      this._toast(t("saved"));
                    } catch (err) {
                      this._toast(String(err.message || err), true);
                    }
                    this._scanImage = null;
                    await this._refresh();
                  },
                },
                t("scanConfirm")
              ),
              el(
                "button",
                {
                  class: "srp-btn small danger",
                  onclick: async () => {
                    try {
                      await this._svc("stockroom", "scan_dismiss", {
                        entry_id: wh.entry_id,
                        suggestion: suggestion.id,
                      });
                    } catch (err) {
                      this._toast(String(err.message || err), true);
                    }
                    this._scanImage = null;
                    await this._refresh();
                  },
                },
                t("scanDismiss")
              )
            )
          )
        );
      }
      container.append(list);
    }
  }

  // ---------------- history view ----------------

  _renderHistory(container) {
    const filters = el("div", { class: "srp-warehouse-row" });

    const whSelect = el("select", {
      class: "srp-chip",
      onchange: async (event) => {
        this._histFilter.warehouse = event.target.value;
        this._histFilter.group_id = "";
        await this._loadHistory().catch(() => {});
        this._render();
      },
    });
    whSelect.append(el("option", { value: "", text: t("allWarehouses") }));
    for (const wh of this._data?.warehouses || []) {
      whSelect.append(el("option", { value: wh.entry_id, text: wh.name }));
    }
    whSelect.value = this._histFilter.warehouse || "";
    filters.append(whSelect);

    const groups = this._histFilter.warehouse
      ? this._slotGroups(this._histFilter.warehouse)
      : [];
    if (this._histFilter.warehouse && groups.length) {
      const groupSelect = el("select", {
        class: "srp-chip",
        onchange: async (event) => {
          this._histFilter.group_id = event.target.value;
          await this._loadHistory().catch(() => {});
          this._render();
        },
      });
      groupSelect.append(el("option", { value: "", text: t("allGroups") }));
      for (const group of groups) {
        groupSelect.append(el("option", { value: group.group_id, text: group.name }));
      }
      groupSelect.value = this._histFilter.group_id || "";
      filters.append(groupSelect);
    }

    container.append(filters);

    const records = this._history?.records || [];
    if (!records.length) {
      container.append(el("div", { class: "srp-hint", text: t("historyEmpty") }));
      return;
    }

    const tbody = el("tbody");
    for (const record of records) {
      tbody.append(
        el(
          "tr",
          {},
          el("td", { text: fmtTime(record.ts) }),
          el("td", { text: record.warehouse_name || record.warehouse || "" }),
          el("td", { text: record.group_name || record.group_id || "" }),
          el("td", { text: String(record.level ?? "") }),
          el("td", { text: record.item_name || record.item_id || "—" }),
          el("td", {
            text:
              record.life_pct === null || record.life_pct === undefined
                ? "—"
                : `${Math.round(record.life_pct)}%`,
          }),
          el(
            "td",
            {},
            el("span", {
              class: `srp-mode-tag ${record.mode === "flt" ? "flt" : "manual"}`,
              text: record.mode === "flt" ? t("modeFlt") : t("modeManual"),
            })
          )
        )
      );
    }
    container.append(
      el(
        "table",
        { class: "srp-table" },
        el(
          "thead",
          {},
          el(
            "tr",
            {},
            el("th", { text: t("hTime") }),
            el("th", { text: t("hWarehouse") }),
            el("th", { text: t("hGroup") }),
            el("th", { text: t("hLevel") }),
            el("th", { text: t("hItem") }),
            el("th", { text: t("hLife") }),
            el("th", { text: t("hMode") })
          )
        ),
        tbody
      )
    );
  }

  // ---------------- dialogs ----------------

  _openDialog(build) {
    this._closeDialog();
    const dialog = el("dialog", { class: "srp-dialog" });
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      this._closeDialog();
    });
    this.shadowRoot.append(dialog);
    build(dialog);
    if (!dialog.open) dialog.showModal();
    this._dialog = dialog;
  }

  _closeDialog() {
    if (this._dialog) {
      this._dialog.close();
      this._dialog.remove();
      this._dialog = null;
    }
  }

  _dialogFrame(dialog, title, bodyChildren, onOk, okLabel) {
    const body = el("div", { class: "srp-dialog-body" }, el("h3", { text: title }), ...bodyChildren);
    const cancelBtn = el(
      "button",
      { class: "srp-btn", onclick: () => this._closeDialog() },
      t("cancel")
    );
    const okBtn = el(
      "button",
      {
        class: "srp-btn primary",
        onclick: async () => {
          try {
            const done = await onOk();
            if (done !== false) this._closeDialog();
          } catch (err) {
            this._toast(String(err.message || err), true);
          }
        },
      },
      okLabel || t("confirm")
    );
    dialog.append(
      body,
      el("div", { class: "srp-dialog-foot" }, cancelBtn, okBtn)
    );
  }

  _field(labelText, input) {
    return el("div", { class: "srp-field" }, el("label", { text: labelText }), input);
  }

  _input(value, type = "text", step) {
    const input = el("input", { type, step, value: value === null || value === undefined ? "" : String(value) });
    return input;
  }

  _openItemDialog(item) {
    const wh = this._currentWarehouse();
    const isEdit = !!item;
    const name = this._input(item?.name || "");
    const category = el("select");
    for (const [value, meta] of Object.entries(CATEGORY_META)) {
      category.append(el("option", { value, text: t(meta.catKey) }));
    }
    category.value = item?.category || "other";
    const unit = this._input(item?.unit || "");
    const threshold = this._input(item?.low_threshold ?? 1, "number", "0.01");
    const location = this._input(item?.location || "");
    const material = this._input(item?.extra?.material || "");
    const color = this._input(item?.extra?.color || "");
    const fullWeight = this._input(item?.extra?.full_weight_g || "", "number", "0.1");

    const materialFields = () => (category.value === "filament"
      ? [
          this._field(t("material"), material),
          this._field(t("color"), color),
          this._field(t("fullWeight"), fullWeight),
        ]
      : []);
    const materialWrap = el("div");
    const refreshMaterial = () => {
      materialWrap.innerHTML = "";
      materialWrap.append(...materialFields());
    };
    category.addEventListener("change", refreshMaterial);
    refreshMaterial();

    this._openDialog((dialog) => {
      this._dialogFrame(
        dialog,
        isEdit ? t("editItem") : t("addItem"),
        [
          this._field(t("name"), name),
          el(
            "div",
            { class: "srp-field-row" },
            this._field(t("category"), category),
            this._field(t("unit"), unit)
          ),
          this._field(t("lowThreshold"), threshold),
          this._field(t("location"), location),
          materialWrap,
        ],
        async () => {
          const itemName = name.value.trim();
          if (!itemName) {
            this._toast(t("name"), true);
            return false;
          }
          if (isEdit) {
            const updates = {
              name: itemName,
              category: category.value,
              unit: unit.value.trim(),
              location: location.value.trim(),
              material: material.value.trim(),
              color: color.value.trim(),
              full_weight_g: parseFloat(fullWeight.value) || 0,
            };
            // Cleared threshold = "no change": sending 0 would pin the
            // threshold and silence the low-stock sensor (the add path
            // guards the same way below).
            if (threshold.value !== "") updates.low_threshold = parseFloat(threshold.value) || 0;
            await this._svc("stockroom", "update_item", {
              entry_id: wh.entry_id,
              item: item.id,
              ...updates,
            });
          } else {
            const data = {
              entry_id: wh.entry_id,
              name: itemName,
              category: category.value,
            };
            if (unit.value.trim()) data.unit = unit.value.trim();
            if (threshold.value !== "") data.low_threshold = parseFloat(threshold.value) || 0;
            if (location.value.trim()) data.location = location.value.trim();
            if (category.value === "filament") {
              if (material.value.trim()) data.material = material.value.trim();
              if (color.value.trim()) data.color = color.value.trim();
              if (parseFloat(fullWeight.value) > 0) data.full_weight_g = parseFloat(fullWeight.value);
            }
            await this._svc("stockroom", "add_item", data);
          }
          this._toast(t("saved"));
          await this._refresh();
        }
      );
    });
  }

  _openQtyDialog(kind, item) {
    const wh = this._currentWarehouse();
    const amount = this._input("", "number", "0.01");
    const note = this._input("");
    const isStocktake = kind === "stocktake";
    this._openDialog((dialog) => {
      this._dialogFrame(
        dialog,
        isStocktake
          ? t("stocktakeTitle", { name: item.name })
          : t("qtyDialogTitle", { action: t(kind), name: item.name }),
        [
          this._field(isStocktake ? t("actual") : t("amount"), amount),
          this._field(t("note"), note),
        ],
        async () => {
          const value = parseFloat(amount.value);
          if (!Number.isFinite(value) || (isStocktake ? value < 0 : value <= 0)) {
            this._toast(t("amount"), true);
            return false;
          }
          const data = { entry_id: wh.entry_id, item: item.id };
          data[isStocktake ? "actual" : "quantity"] = value;
          if (note.value.trim()) data.note = note.value.trim();
          await this._svc("stockroom", kind, data);
          this._toast(t("saved"));
          await this._refresh();
        }
      );
    });
  }

  _confirmRemoveItem(item) {
    const wh = this._currentWarehouse();
    this._openConfirm(
      t("deleteItemTitle"),
      t("deleteItemText", { name: item.name }),
      async () => {
        await this._svc("stockroom", "remove_item", {
          entry_id: wh.entry_id,
          item: item.id,
        });
        await this._refresh();
      }
    );
  }

  _confirmRemoveGroup(group) {
    this._openConfirm(
      t("deleteGroup"),
      t("deleteGroupText", { name: group.name }),
      async () => {
        await this._saveSlots(
          this._slotGroups(this._warehouse).filter(
            (g) => g.group_id !== group.group_id
          )
        );
      }
    );
  }

  async _saveSlots(groups) {
    // No local try/catch: _fetch throws on failure and the dialog frame's
    // onOk handler turns that into a toast while keeping the dialog open,
    // so the user's edits survive a failed save.
    await this._fetch(API_SLOTS, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entry_id: this._warehouse, groups }),
    });
    this._toast(t("saved"));
    await this._refresh();
  }

  _openGroupDialog(group) {
    const wh = this._currentWarehouse();
    const isEdit = !!group;
    const state = {
      group_id: group?.group_id || uuid(),
      name: this._input(group?.name || ""),
      source_entity: group?.source_entity || "",
      source_type: group?.source_type || "duration",
      target_state: group?.target_state || "",
      debounce: group?.debounce ?? 10,
      levels: group
        ? group.levels.map((s) => ({
            level: s.level,
            item_id: s.item_id || null,
            rated_time_days: s.rated_time_days ?? "",
            rated_usage: s.rated_usage ?? "",
            warn_threshold: s.warn_threshold ?? 20,
            cascade_factor: s.cascade_factor ?? "",
          }))
        : [],
    };

    // Entity picker: searchable input over the live HA state machine, so the
    // picker's data source is the system entity list (not an integration's
    // config entries — the retired FLT design that left this menu empty).
    const entityList = el("datalist", { id: "srp-entity-list" });
    const entityOptions = [];
    const states = (this._hass && this._hass.states) || {};
    for (const [entityId, stateObj] of Object.entries(states)) {
      if (!/^(sensor|binary_sensor|input_boolean|switch)\./.test(entityId)) continue;
      entityOptions.push({
        id: entityId,
        label: `${stateObj.attributes?.friendly_name || entityId} (${entityId})`,
      });
    }
    entityOptions.sort((a, b) => a.label.localeCompare(b.label));
    for (const option of entityOptions) {
      entityList.append(el("option", { value: option.id, label: option.label }));
    }

    const sourceInput = this._input(state.source_entity);
    sourceInput.setAttribute("list", "srp-entity-list");
    sourceInput.placeholder = t("sourceHint");
    const typeSelect = el("select");
    typeSelect.append(
      el("option", { value: "duration", text: t("sourceDuration") }),
      el("option", { value: "count", text: t("sourceCount") })
    );
    typeSelect.value = state.source_type;
    const targetInput = this._input(state.target_state);
    targetInput.placeholder = "running / heating / …";
    const debounceInput = this._input(String(state.debounce), "number", "1");

    const sourceWrap = el("div", { class: "srp-source-fields" });
    const rebuildSourceFields = () => {
      sourceWrap.innerHTML = "";
      if (!sourceInput.value.trim()) return;
      sourceWrap.append(
        el("div", { class: "srp-field-row" },
          this._field(t("sourceType"), typeSelect),
          this._field(t("targetState"), targetInput)
        ),
        typeSelect.value === "count"
          ? this._field(t("debounce"), debounceInput)
          : null
      );
    };
    typeSelect.addEventListener("change", rebuildSourceFields);
    // 水源实体输入是字段区的触发源：不监听 input 的话，新建组时字段区
    // 永不出现（SR-F-026）。
    sourceInput.addEventListener("input", rebuildSourceFields);
    rebuildSourceFields();

    const levelsWrap = el("div");
    const itemOptions = (selected) => {
      const select = el("select");
      select.append(el("option", { value: "", text: t("slotBindNone") }));
      for (const item of Object.values(wh.items || {})) {
        select.append(
          el("option", {
            value: item.id,
            text: `${item.name} (${fmtQty(item.quantity)}${item.unit || ""})`,
          })
        );
      }
      select.value = selected || "";
      return select;
    };

    const rebuildLevels = () => {
      levelsWrap.innerHTML = "";
      if (!state.levels.length) state.levels = [1, 2, 3].map((level) => ({
        level, item_id: null, rated_time_days: "", rated_usage: "",
        warn_threshold: 20, cascade_factor: "",
      }));
      state.levels.forEach((slot, index) => {
        const item = itemOptions(slot.item_id);
        item.addEventListener("change", () => {
          slot.item_id = item.value || null;
        });
        const ratedTime = this._input(String(slot.rated_time_days ?? ""), "number", "1");
        ratedTime.addEventListener("input", () => {
          slot.rated_time_days = ratedTime.value === "" ? "" : parseFloat(ratedTime.value);
        });
        const ratedUsage = this._input(String(slot.rated_usage ?? ""), "number", "1");
        ratedUsage.addEventListener("input", () => {
          slot.rated_usage = ratedUsage.value === "" ? "" : parseFloat(ratedUsage.value);
        });
        const warn = this._input(String(slot.warn_threshold ?? 20), "number", "1");
        warn.addEventListener("input", () => {
          slot.warn_threshold = warn.value === "" ? 20 : parseFloat(warn.value);
        });
        const cascade = this._input(String(slot.cascade_factor ?? ""), "number", "0.1");
        cascade.placeholder = t("cascadeHint");
        cascade.addEventListener("input", () => {
          slot.cascade_factor = cascade.value === "" ? "" : parseFloat(cascade.value);
        });
        const removeBtn = el(
          "button",
          {
            class: "srp-btn small danger",
            onclick: () => {
              state.levels.splice(index, 1);
              state.levels.forEach((s, i) => {
                s.level = i + 1;
              });
              rebuildLevels();
            },
          },
          el("ha-icon", { icon: "mdi:close" })
        );
        levelsWrap.append(
          el(
            "div",
            { class: "srp-level-editor" },
            el(
              "div",
              { class: "srp-slot-bind-row" },
              el("span", { class: "srp-level-badge", text: t("levelN", { n: slot.level }) }),
              item
            ),
            el(
              "div",
              { class: "srp-field-row" },
              this._field(t("ratedDays"), ratedTime),
              this._field(t("ratedUsage"), ratedUsage)
            ),
            el(
              "div",
              { class: "srp-field-row" },
              this._field(t("warnThreshold"), warn),
              index > 0 ? this._field(t("cascade"), cascade) : el("span")
            ),
            el("div", { class: "srp-level-remove" }, removeBtn)
          )
        );
      });
    };
    rebuildLevels();

    const addLevelBtn = el(
      "button",
      {
        class: "srp-btn small",
        onclick: () => {
          if (state.levels.length >= 8) return;
          state.levels.push({
            level: state.levels.length + 1,
            item_id: null,
            rated_time_days: "",
            rated_usage: "",
            warn_threshold: 20,
            cascade_factor: "",
          });
          rebuildLevels();
        },
      },
      el("ha-icon", { icon: "mdi:plus" }),
      t("addLevel")
    );

    this._openDialog((dialog) => {
      dialog.append(entityList);
      this._dialogFrame(
        dialog,
        isEdit ? t("editGroup") : t("addGroup"),
        [
          this._field(t("groupName"), state.name),
          this._field(t("sourceEntity"), sourceInput),
          sourceWrap,
          el("div", { class: "srp-field" }, el("label", { text: t("levels") }), levelsWrap),
          el("div", { class: "srp-field" }, addLevelBtn),
        ],
        async () => {
          const name = state.name.value.trim() || t("defaultGroupName");
          const source = sourceInput.value.trim();
          if (source && !targetInput.value.trim()) {
            this._toast(t("targetStateRequired"), true);
            return false;
          }
          if (source && !this._hass.states[source]) {
            this._toast(t("entityNotFound"), true);
            return false;
          }
          const groups = this._slotGroups(this._warehouse).filter(
            (g) => g.group_id !== state.group_id
          );
          groups.push({
            group_id: state.group_id,
            name,
            source_entity: source || null,
            source_type: source ? typeSelect.value : null,
            target_state: source ? targetInput.value.trim() : null,
            debounce: source ? parseInt(debounceInput.value, 10) || 10 : null,
            levels: state.levels.map((slot, index) => {
              const data = {
                level: index + 1,
                item_id: slot.item_id || null,
                rated_time_days: slot.rated_time_days === "" ? null : slot.rated_time_days,
                rated_usage: slot.rated_usage === "" ? null : slot.rated_usage,
                warn_threshold: slot.warn_threshold ?? 20,
              };
              if (index > 0 && slot.cascade_factor !== "") {
                data.cascade_factor = slot.cascade_factor;
              }
              return data;
            }),
          });
          await this._saveSlots(groups);
        }
      );
    });
  }

  _openConfirm(title, text, onOk) {
    this._openDialog((dialog) => {
      this._dialogFrame(dialog, title, [el("div", { text, style: "font-size:14px;line-height:1.5" })], onOk);
    });
  }

  // ---------------- toast ----------------

  _toast(message, isError = false) {
    const existing = this.shadowRoot.querySelector(".srp-toast");
    if (existing) existing.remove();
    const toast = el("div", { class: `srp-toast${isError ? " error" : ""}`, text: message });
    this.shadowRoot.append(toast);
    setTimeout(() => toast.remove(), isError ? 5000 : 3000);
  }
}

customElements.define("stockroom-panel", StockroomPanel);
