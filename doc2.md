# 扩展架构 v2 — Figma 对比 + 自主巡检

这两个需求把 agent 从"可被指令驱动的工具"升级成"**有判断能力的 QA 自动化系统**"。需要在原架构上**新增 3 层 + 扩展 Core/Actions/Intents**。下面给完整增量设计。

---

## 一、能力分析（先把问题拆透）

### 需求 1：Figma 设计稿对比
**核心难点**不是"截屏 vs Figma 图片做像素 diff"——那种方式假阳性极高（动态内容、字体渲染差异、状态变化都会误报）。**正解是结构化对比**：

```
App 端语义结构  ←─对齐─→  Figma 端语义结构
(view_hierarchy)         (Figma REST API tree)
       ↓                          ↓
   每个节点抽出 {字体、字号、字色、背景、frame、padding}
       ↓                          ↓
              字段级 diff (容差可配)
                      ↓
              可读的差异报告
```

关键是**节点对齐（node matching）**——这是质量分水岭。

### 需求 2：自主 UI 巡检
本质是 **agent 自我驱动的探索循环**：
```
配置: 路由清单 / 点击路径剧本 / 巡检规则
循环: 进入页面 → 等稳定 → 多维度检查 → 记录异常 → 下一页
检查: 渲染异常(空白/错位/裁切) + 数据异常(空数据/loading 卡死) + Figma 一致性
产出: 异常清单 + 截屏证据 + 复现步骤
```

两个需求**共用底层**：都需要"页面进入后的稳定快照 + 多维度断言 + 证据归档"。

---

## 二、架构增量（在原 8 层基础上加 3 层）

```
┌─────────────────────────────────────────────┐
│  CLI / 外部 LLM                              │
└──────────────┬──────────────────────────────┘
               │
┌──────────────▼──────────────────────────────┐
│  L5  Agent Loop                             │
├─────────────────────────────────────────────┤
│  L4  Intents      ← 新增 figma_compare /    │
│                     ui_patrol               │
├─────────────────────────────────────────────┤
│  L3  Actions      ← 新增 capture_design /   │
│                     assert_*                │
├─────────────────────────────────────────────┤
│ ★L3.5 Inspectors  ← 新层：检测器集合         │
│        (规则、视觉、Figma 对齐、稳定性判定)    │
├─────────────────────────────────────────────┤
│ ★L2.5 DesignSpec  ← 新层：Figma 抓取/缓存/   │
│        归一化                                │
├─────────────────────────────────────────────┤
│ ★L2.7 Patrol      ← 新层：剧本/路由清单      │
│        引擎（DAG 调度）                      │
├─────────────────────────────────────────────┤
│  L2  Session                                 │
├─────────────────────────────────────────────┤
│  L1  Core         ← 扩展 view 节点字段       │
└─────────────────────────────────────────────┘
```

---

## 三、新模块详细设计

### 模块 A：DesignSpec —— Figma 数据获取与归一化

```
ios_inspector_agent/design/
├── figma_client.py          # Figma REST API 封装
├── figma_parser.py          # Figma node tree → 归一化 DesignNode
├── normalizer.py            # 单位/颜色/字体名归一化
├── matcher.py               # ★ App ViewNode ↔ Figma DesignNode 对齐
├── differ.py                # 字段级 diff，输出 DiffReport
├── tolerance.py             # 容差配置（颜色 ΔE、间距 px、字号 pt）
└── cache.py                 # Figma 文件按 (file_key, version) 落本地，避免反复拉
```

#### A.1 Figma 抓取
```python
class FigmaClient:
    def __init__(self, token: str):
        self.token = token  # 走 ~/.ios-inspector/config.toml 或 env

    def fetch_file(self, file_key: str) -> dict: ...
    def fetch_node(self, file_key: str, node_id: str) -> dict: ...
    def fetch_image(self, file_key, node_ids, *, scale=2, format="png") -> dict[str, bytes]:
        """渲染图（用于视觉 fallback 和最终报告）"""
```

#### A.2 归一化的 DesignNode
**关键：App 端和 Figma 端用同一份 schema**，diff 才有意义。

```python
@dataclass(frozen=True)
class StyleSpec:
    # 字体
    font_family: str | None
    font_size: float | None              # pt
    font_weight: int | None              # 100-900
    font_color: Color | None             # RGBA
    line_height: float | None
    letter_spacing: float | None
    text_align: Literal["left","center","right","justify"] | None

    # 盒模型
    background: Color | None
    border_color: Color | None
    border_width: float | None
    corner_radius: float | None
    opacity: float | None

    # 间距（如果可推断）
    padding: tuple[float,float,float,float] | None  # T R B L

@dataclass(frozen=True)
class DesignNode:
    id: str
    name: str
    type: Literal["text","image","container","button","icon","line","unknown"]
    frame: Frame                         # 全局坐标系
    style: StyleSpec
    text_content: str | None
    children: tuple["DesignNode", ...]
    # 来源标识（debug 用）
    source: Literal["figma","app"]
    raw_ref: str                         # figma node_id 或 app address
```

`figma_parser` 把 Figma 的 `FRAME/TEXT/RECTANGLE/...` 全部转成上面这个结构；`core/client.py` 端**扩展 ViewNode 让它能产出 StyleSpec**（需要 SAInspector 服务端补 endpoint，见第六节）。

#### A.3 节点对齐 —— 整个能力的核心

四级匹配策略，**自顶向下、能省则省**：

| 优先级 | 匹配键 | 说明 |
|---|---|---|
| 1 | `accessibility_id` ↔ Figma node `name`（按约定如 `aid:purchase_btn`） | 团队若有约定，准确率最高 |
| 2 | 类型 + 文本完全匹配 | TextView with same string |
| 3 | 类型 + frame 重叠率 > 80%（IoU） | 几何匹配 |
| 4 | 类型 + 视觉相似度 fallback（裁切两侧渲染图，做 perceptual hash） | 兜底 |

```python
class NodeMatcher:
    def __init__(self, *, strategies: list[MatchStrategy], 
                 viewport: Frame, design_artboard: Frame):
        # viewport 和 artboard 不同尺寸 → 需要做坐标系变换
        ...

    def match(self, app_root: ViewNode, design_root: DesignNode) -> MatchResult:
        """返回：
        - pairs: [(app_node, design_node, confidence, strategy_used), ...]
        - app_orphans: app 有但 figma 没有的（多绘制 / 动态元素）
        - design_orphans: figma 有但 app 没有的（漏实现）
        """
```

> **架构上必须把 matcher 单拎一层**，因为团队对齐策略会演进（可能引入 OCR、引入设计稿命名规范工具），matcher 接口稳定，实现可替换。

#### A.4 Diff 与容差

```python
@dataclass
class FieldDiff:
    field: str                       # "style.font_size"
    app_value: Any
    design_value: Any
    delta: float | None              # 数值差
    severity: Literal["info","warn","error"]
    tolerance_used: str              # "default" / "strict" / "icon_size"

class Differ:
    def diff(self, pair: NodePair, tol: ToleranceConfig) -> list[FieldDiff]: ...

# 容差配置举例（YAML）
tolerances:
  default:
    font_size_pt: 0.5
    color_delta_e: 2.0           # CIEDE2000
    frame_px: 1.0
    corner_radius_px: 0.5
  strict:
    color_delta_e: 1.0
  per_node:
    "aid:price_label":
      font_size_pt: 0.0          # 价格字号必须精确
```

颜色比较用 **ΔE (CIEDE2000)** 而不是 RGB 欧氏距离——前者贴近人眼感知，业界标准。

---

### 模块 B：Inspectors —— 检测器集合

把"什么算 UI bug"从 prompt 沉淀进代码。**每个 inspector 是独立可单测的纯函数**。

```
ios_inspector_agent/inspectors/
├── base.py                  # Inspector 接口、Finding 数据结构
├── stability.py             # 等待页面稳定（动画结束、loading 完成）
├── rendering.py             # 渲染异常检测器
├── layout.py                # 布局异常检测器
├── content.py               # 内容异常检测器
├── figma_consistency.py     # 调用 design 模块做对比
└── registry.py              # 检测器注册 + 启停配置
```

#### B.1 Finding（统一异常结构）
```python
@dataclass
class Finding:
    inspector: str                     # "rendering.text_clipped"
    severity: Literal["info","warn","error","critical"]
    summary: str                       # 一句话
    detail: str                        # 详细
    affected_nodes: list[str]          # view addresses
    evidence: list[Path]               # 截屏、高亮图、diff json
    suggested_action: str | None
    fingerprint: str                   # 用于去重：同一类异常只报一次
```

#### B.2 内置检测器清单

| Inspector | 检测什么 | 实现要点 |
|---|---|---|
| `stability.is_stable` | 连续 N 帧 view tree 哈希不变；无 loading view；无运行中动画 | 巡检前置条件 |
| `rendering.text_clipped` | text view 实际内容长度 vs frame，估算是否溢出 / 截断 | App 端 endpoint 提供 `intrinsicContentSize` |
| `rendering.text_invisible` | 字色与背景 ΔE < 阈值 | 容易踩的暗黑模式 bug |
| `rendering.image_missing` | UIImageView.image == nil 但 frame 有可见尺寸 | 占位图缺失 |
| `rendering.empty_state_unintended` | 列表 view dataSource count == 0 但**未显示**空态 view | 假数据未灌入 |
| `layout.overlap` | 同层 sibling frames 相交且都不是装饰 view | 错位 |
| `layout.overflow_screen` | view frame 超出屏幕但不是 scrollView 内部 | 页面爆边 |
| `layout.zero_size` | 可见 view 但 width==0 或 height==0 | 约束 bug |
| `layout.huge_gap` | 同 stack 内 sibling 间距 > 阈值 | 漏元素 |
| `content.placeholder_leak` | 文本含 "TODO/Lorem/未配置/{xxx}/null/undefined" | mock 数据上线 |
| `content.broken_image_url` | network_log 里 image 请求 4xx/5xx | |
| `content.long_loading` | loading view 持续 > N 秒 | 接口超时未处理 |
| `figma.style_mismatch` | 调 differ，按容差判 error | 设计稿一致性 |
| `figma.missing_element` | design 有但 app 缺 | 漏实现 |
| `figma.extra_element` | app 有但 design 缺 | 多余元素或动态内容（白名单可豁免） |

#### B.3 Inspector 接口
```python
class Inspector(Protocol):
    name: str
    requires: set[Literal["view_tree","screenshot","network_log","design_spec","stability"]]

    def inspect(self, ctx: InspectionContext) -> list[Finding]: ...

class InspectionContext:
    session: InspectorSession
    snapshot: Snapshot                  # 已抓好的 view+vc+screenshot
    design_spec: DesignSpec | None      # 仅 figma 类需要
    config: InspectorConfig
```

#### B.4 编排（Pipeline）
```python
class InspectionPipeline:
    def __init__(self, inspectors: list[Inspector], dedupe=True): ...

    def run(self, ctx) -> InspectionReport:
        # 1. 等待 stability（前置）
        # 2. 并行跑所有 inspector（线程池，IO 居多）
        # 3. 按 fingerprint 去重
        # 4. 按 severity 排序
        # 5. 落盘 + 生成报告
```

---

### 模块 C：Patrol —— 巡检剧本引擎

```
ios_inspector_agent/patrol/
├── plan.py                  # PatrolPlan 数据结构 + YAML 加载
├── scenario.py              # Scenario / Step / Assertion
├── runner.py                # 调度执行
├── recovery.py              # 失败恢复策略
└── reporter.py              # 巡检报告（不同于单次 agent run）
```

#### C.1 巡检剧本 schema（YAML，给人写）

```yaml
# patrol/homepage_smoke.yaml
name: Homepage Smoke Patrol
description: 巡检主页核心模块
config:
  inspectors: [stability, rendering, layout, content, figma]
  tolerance: default
  figma_file: "abc123XYZ"
  stop_on: critical              # error / critical / never
  max_duration_minutes: 15

setup:
  - action: rebuild_and_check
    scheme: StoryAIMainland
  - action: login_test_account
    account: ${SECRET:test_account_a}

scenarios:

  - name: 主页 → 个人主页
    figma_node: "1234:5678"      # 对应 Figma 节点（可选）
    steps:
      - open_url: "//client/home"
      - wait_stable: { timeout: 5 }
      - inspect: all              # 跑全部 inspector
      - tap: { text: "我的" }
      - wait_stable: { timeout: 3 }
      - inspect:
          figma_node: "1234:5680"

  - name: 故事详情页（多 story 抽样）
    parametrize:
      story_id: ["s001", "s002", "s003"]
    steps:
      - open_url: "//client/story/detail?story_id=${story_id}"
      - wait_stable: { timeout: 8 }
      - inspect: all
      - scroll: { dy: 600 }
      - inspect: [layout, content]

  - name: 会员购买流程
    steps:
      - open_url: "//commerce/member_page"
      - wait_stable: {}
      - inspect: all
      - find_and_tap: "立即开通"
      - wait_stable: {}
      - inspect: { figma_node: "9999:0001" }

teardown:
  - logout
  - rollback_modifications
```

#### C.2 PatrolRunner
```python
class PatrolRunner:
    def __init__(self, plan: PatrolPlan, session, pipeline, recorder): ...

    def run(self) -> PatrolReport:
        with self._setup_context():
            for scenario in self.plan.scenarios:
                for params in scenario.parametrize_grid():
                    try:
                        self._run_scenario(scenario, params)
                    except UnrecoverableError as e:
                        self.recorder.log_scenario_abort(scenario, e)
                        if self.plan.stop_on_error:
                            break
                    finally:
                        self._reset_to_clean_state()    # 关键：场景间互不污染
            self._teardown()
        return self.recorder.finalize()
```

#### C.3 关键设计点

| 点 | 决策 |
|---|---|
| **每个 scenario 之间必须状态隔离** | runner 在场景结束时强制 `back to root + clear_modifications + scroll_to_top`；否则一个场景的脏状态会污染下一个 |
| **失败恢复 ≠ 跳过** | 单步失败先尝试恢复（重试 / 回到首页再 open_url），多次仍失败才标记 scenario 失败但**继续下一个** scenario |
| **参数化 (parametrize)** | 同一剧本跑多组数据（不同 story_id / 不同账号），异常按参数维度聚合 |
| **fingerprint 去重** | 同一个 bug 在 10 个 story 上都出现 → 报告里聚合显示"出现于 10/10 case"，不刷屏 |
| **巡检独立 workdir** | `runs/patrol_20260508_193800/` 下分 scenario 子目录，便于归档 |

---

### 模块 D：扩展原有层

#### D.1 Core 层扩展（`core/models.py`）

`ViewNode` 必须能产出 `StyleSpec`，否则 figma 对比无从谈起：

```python
@dataclass(frozen=True)
class ViewNode:
    address: str
    cls: str
    frame: Frame
    text: str | None
    accessibility_id: str | None
    hidden: bool
    # ★ 新增字段
    style: StyleSpec                          # 字体/颜色/边框/圆角
    intrinsic_size: Size | None               # 内容自然尺寸（用于检测裁切）
    is_scroll_view: bool
    is_loading: bool                          # 是否在 loading 状态
    children: tuple["ViewNode", ...] = ()
```

这要求 **SAInspector 服务端补一个 `/api/view_hierarchy_styled`**（详见第六节），原 `/api/view_hierarchy` 不动以保兼容。

#### D.2 Actions 新增

```
actions/
├── design.py
│   ├── CaptureDesignSpec          # 拉 Figma 节点 → DesignNode
│   ├── AlignAppToDesign           # NodeMatcher 跑一遍
│   └── DiffAppVsDesign            # 输出 DiffReport
├── assertions.py
│   ├── AssertNoFindings           # 断言 inspector 通过
│   ├── AssertScreenStable
│   └── AssertVCMatches
└── exploration.py
    ├── EnumerateRoutes            # 从配置/路由表枚举可巡检 URL
    ├── DriveScript                # 执行点击路径剧本
    └── ResetToRoot
```

#### D.3 Intents 新增

```python
class FigmaCompareIntent(Intent):
    """对比当前页面 vs Figma 节点。
       参数: figma_file, figma_node_id, tolerance
       产出: DiffReport + 标注图（在截屏上画框框）
    """

class UIPatrolIntent(Intent):
    """加载巡检剧本并执行。
       参数: plan_path 或 inline_plan
       产出: PatrolReport
    """

class ExploratoryPatrolIntent(Intent):
    """无剧本探索：从给定起点出发，BFS 点击可点元素，最大深度 N。
       参数: start_url, max_depth=2, max_pages=20, blacklist=[...]
       用途: 没有现成剧本时的快速冒烟
    """
```

---

## 四、最终目录（v2 完整版）

```
ios-inspector-agent/
├── ios_inspector_agent/
│   ├── core/                  # L1
│   ├── session/               # L2
│   ├── design/                # ★L2.5 Figma
│   ├── patrol/                # ★L2.7 剧本
│   ├── actions/               # L3
│   ├── inspectors/            # ★L3.5 检测器
│   ├── intents/               # L4
│   ├── agent/                 # L5
│   ├── safety/                # L6
│   ├── trace/                 # L7
│   ├── llm/                   # L8
│   ├── config.py
│   └── cli.py
├── plans/                     # 巡检剧本仓库（团队共享）
│   ├── homepage_smoke.yaml
│   ├── payment_critical.yaml
│   └── _shared/
│       ├── tolerances/
│       └── figma_mappings.yaml   # accessibility_id ↔ figma_node_id
├── tests/
└── docs/
    ├── writing_a_patrol.md
    └── figma_setup.md
```

---

## 五、CLI 使用形态

```bash
# Figma 单次对比
ios-inspector compare \
    --figma-file abc123 --figma-node 1234:5678 \
    --route "//client/home" \
    --tolerance strict

# 跑剧本
ios-inspector patrol plans/homepage_smoke.yaml

# 探索式巡检（无剧本）
ios-inspector patrol-explore \
    --start-url "//client/home" --max-depth 2

# 自然语言驱动（agent 模式）
ios-inspector agent "巡检主页和详情页，重点看 Figma 一致性"

# 单纯诊断
ios-inspector doctor
```

---

## 六、对 SAInspector 服务端的依赖（重要！）

整套能力依赖 App 内嵌的 SAInspector **新增/扩展若干 endpoint**：

| Endpoint | 用途 | 是否必须 |
|---|---|---|
| `GET /api/view_hierarchy_styled` | 输出含 StyleSpec 的节点树 | **必须** |
| `GET /api/view_inspect_styled` | 单节点完整样式 | 必须 |
| `GET /api/render_node` | 服务端按 address 渲染单 view 为 png（用于视觉 fallback 匹配） | 可选但强烈建议 |
| `GET /api/animation_state` | 当前是否有运行中动画 | 用于 stability 判定 |
| `GET /api/loading_state` | 自定义 loading view 注册 | 可选 |
| `GET /api/intrinsic_size?address=` | text/image 内容自然尺寸 | rendering.text_clipped 用 |

> **建议**：把 SAInspector 的 endpoint 演进单独立项，agent 这边按 capability flag 判断哪些 inspector 可用，**优雅降级**——服务端没升级时 figma 对比退化成"frame + 文本"维度，照样跑。

---

## 七、关键设计决策汇总

| 决策 | 理由 |
|---|---|
| **Figma 对比走结构化 diff，不走像素 diff** | 像素 diff 假阳性高，且无法定位"是哪个字段错了"；结构化 diff 直接告诉你"font_size 14 vs 设计稿 16" |
| **节点匹配独立分层 (matcher)** | 匹配策略会持续演进（OCR、视觉、命名约定），matcher 接口稳定、实现可替换 |
| **每个 inspector 独立可单测** | UI bug 检测规则需要持续积累，必须低成本添加新规则 |
| **巡检剧本用 YAML** | 让 QA / 设计师也能写，不需要会 Python；agent 只是执行器 |
| **fingerprint 去重** | 巡检规模大了之后，"同一 bug 在 10 个页面出现"必须聚合 |
| **场景间强制状态重置** | 否则一个场景的脏状态污染所有后续，巡检报告不可信 |
| **stability gate 前置** | 在不稳定的页面跑 inspector 全是噪音；先等稳定再检测 |
| **Capability flag** | 服务端能力是渐进升级的，客户端要能识别并降级 |
| **容差 per-node 可覆盖** | 价格、品牌色这类关键元素需严格；占位文字可宽松 |
| **设计稿和 App 用同一 schema** | 否则 diff 写起来全是 if/else |

---

## 八、推荐实施路径（M5–M8）

接续之前的 M1–M4：

| Milestone | 内容 | 价值 |
|---|---|---|
| **M5** | Core 扩展 StyleSpec + SAInspector 服务端补 `/view_hierarchy_styled` | 任何样式相关功能的前提 |
| **M6** | Inspectors 内置 8 个核心规则（rendering/layout/content） | 即使没 Figma 也能跑巡检 |
| **M7** | DesignSpec 模块 + figma intent + matcher v1（命名/文本/IoU 三策略） | Figma 对比 MVP |
| **M8** | Patrol 引擎 + YAML 剧本 + 报告聚合 | 完整巡检能力 |

每个 milestone 都能独立产出价值。M6 完成时已经能用纯规则跑出大量 bug；M7 解决"和设计稿差多少"；M8 解决"我有 100 个页面要每天检查"。

---

## 九、最终能力图谱

```
                ┌─────────────────────────────────────┐
                │          User / QA / CI             │
                └────────────────┬────────────────────┘
                                 │
                ┌────────────────▼────────────────────┐
                │  自然语言 / YAML 剧本 / 单条命令     │
                └────────────────┬────────────────────┘
                                 │
                ┌────────────────▼────────────────────┐
                │            Agent Loop                │
                └─┬──────────┬──────────┬─────────────┘
                  │          │          │
            ┌─────▼────┐ ┌───▼───┐ ┌───▼─────────┐
            │ Compare  │ │Patrol │ │ Free Agent  │
            │ Figma    │ │ Plan  │ │ Tasks       │
            └─────┬────┘ └───┬───┘ └───┬─────────┘
                  │          │          │
                  └──────────┼──────────┘
                             │
                  ┌──────────▼──────────────┐
                  │ Inspection Pipeline     │
                  │ (8+ inspectors 并行)    │
                  └──────────┬──────────────┘
                             │
                  ┌──────────▼──────────────┐
                  │ Session + Snapshot      │
                  └──────────┬──────────────┘
                             │
                  ┌──────────▼──────────────┐
                  │ SAInspector HTTP        │
                  └─────────────────────────┘
```

---
