# iOS Inspector Agent — 完整 Harness 架构

下面给一套**可直接落地、与具体 LLM provider 解耦**的 agent 框架。设计目标：让 LLM 只负责"决定下一步意图"，所有**状态管理、验证、安全、回滚、trace** 由 harness 用代码保证。

---

## 0. 顶层目录结构

```
ios-inspector-agent/
├── pyproject.toml
├── SKILL.md                          # 给外层 LLM 的入口说明（保留 skill 形态）
├── README.md
│
├── ios_inspector_agent/
│   ├── __init__.py
│   │
│   ├── core/                         # ─── L1: 与 App 通信的纯函数层 ───
│   │   ├── client.py                 # InspectorClient：HTTP 调用，返回 dataclass
│   │   ├── models.py                 # ViewNode / VCNode / TapResult / AppState ...
│   │   ├── errors.py                 # 错误分类体系
│   │   ├── transport.py              # urllib 封装：超时/退避/幂等性标注
│   │   └── xcode.py                  # xcodebuild + AppleScript 兜底
│   │
│   ├── session/                      # ─── L2: 会话/世界状态层 ───
│   │   ├── session.py                # InspectorSession：缓存、diff、artifact 管理
│   │   ├── snapshot.py               # 捕获 vc+view+screenshot 三元组
│   │   ├── cache.py                  # TTL 缓存
│   │   └── workdir.py                # 每次 run 的工作目录、artifact 落盘
│   │
│   ├── actions/                      # ─── L3: 带语义+验证的动作层 ───
│   │   ├── base.py                   # Action 基类、@verify 装饰器、幂等性标记
│   │   ├── inspect.py                # find_view / describe_screen / wait_for
│   │   ├── interact.py               # tap / scroll / input / open_url
│   │   ├── lifecycle.py              # build / run / wait_app / stop
│   │   ├── ranking.py                # 候选 view 打分排序
│   │   └── undo.py                   # 修改栈 / 回滚
│   │
│   ├── intents/                      # ─── L4: 高层意图（recipe）───
│   │   ├── base.py                   # Intent 基类
│   │   ├── find_and_tap.py           # "找到 X 并点击"
│   │   ├── verify_screen.py          # "校验当前页面是 X"
│   │   ├── rebuild_and_check.py      # "改完代码 → build → verify build log → run → 验证"
│   │   ├── extract_state.py          # "抓取 app_state + AB + flags"
│   │   └── registry.py               # intent 注册中心
│   │
│   ├── agent/                        # ─── L5: agent 主循环 ───
│   │   ├── loop.py                   # think → act → observe → reflect
│   │   ├── planner.py                # LLM-driven 规划器
│   │   ├── tools.py                  # 把 actions 暴露为 LLM tool schema
│   │   ├── memory.py                 # 短期 scratchpad + 长期 知识沉淀
│   │   └── policy.py                 # 安全/预算/权限决策
│   │
│   ├── safety/                       # ─── L6: 横切安全 ───
│   │   ├── guard.py                  # host 白名单 / 危险 route 拦截
│   │   ├── audit.py                  # 审计日志（敏感数据访问）
│   │   ├── rate_limit.py             # 总动作数 / 单类动作上限
│   │   └── confirm.py                # 高危动作的人工确认门
│   │
│   ├── trace/                        # ─── L7: 可观测性 ───
│   │   ├── recorder.py               # 每步落 JSONL
│   │   ├── reporter.py               # 生成 markdown 报告（含截屏）
│   │   └── timeline.py               # before/after 截屏对比
│   │
│   ├── llm/                          # ─── L8: LLM 适配层（provider-agnostic）───
│   │   ├── base.py                   # LLMClient 抽象接口
│   │   ├── anthropic.py
│   │   ├── openai.py
│   │   └── prompts/                  # 系统 prompt 模板
│   │       ├── system.md
│   │       ├── plan.md
│   │       └── reflect.md
│   │
│   ├── config.py                     # 配置加载 / 环境变量 / 默认值
│   └── cli.py                        # 入口 CLI
│
├── tests/
│   ├── unit/                         # mock HTTP 的纯单测
│   ├── integration/                  # 跑真 simulator 的端到端
│   └── fixtures/                     # 录制的 HTTP response
│
└── docs/
    ├── architecture.md
    ├── adding_an_action.md
    └── adding_an_intent.md
```

---

## 1. 分层契约（关键！）

```
┌─────────────────────────────────────────────┐
│  CLI / 外部 LLM (Claude Code, Mira, etc.)   │
└──────────────┬──────────────────────────────┘
               │ tool calls (JSON schema)
┌──────────────▼──────────────────────────────┐
│  L5  Agent Loop  (think → act → observe)    │  ← 决策
├─────────────────────────────────────────────┤
│  L4  Intents     (recipe，多步组合)          │  ← 计划
├─────────────────────────────────────────────┤
│  L3  Actions     (单步语义+verify)           │  ← 执行
├─────────────────────────────────────────────┤
│  L2  Session     (缓存/diff/artifact)        │  ← 状态
├─────────────────────────────────────────────┤
│  L1  Core        (HTTP 调用，无副作用)        │  ← 通信
└──────────────┬──────────────────────────────┘
               │ HTTP
        SAInspectorHTTPServer (in App)
```

**单向依赖**：上层只能引用直接下层；safety/trace/llm 是横切，可被任何层调用。

---

## 2. 每层关键代码骨架

### L1 — Core（与 App 通信）

**`core/errors.py`**
```python
class InspectorError(Exception):
    code: str = "E_UNKNOWN"
    retriable: bool = False

class Unreachable(InspectorError):     code, retriable = "E_UNREACHABLE", True
class Timeout(InspectorError):         code, retriable = "E_TIMEOUT", True
class TargetNotFound(InspectorError):  code = "E_TARGET_NOT_FOUND"
class TargetAmbiguous(InspectorError): code = "E_AMBIGUOUS"      # 带 candidates
class InvalidArgument(InspectorError): code = "E_INVALID_ARG"
class BuildFailed(InspectorError):     code = "E_BUILD_FAILED"
class PermissionDenied(InspectorError):code = "E_PERMISSION"
class AppNotRunning(InspectorError):   code = "E_APP_NOT_RUNNING", True
```

**`core/models.py`**
```python
@dataclass(frozen=True)
class Frame:
    x: float; y: float; width: float; height: float

@dataclass(frozen=True)
class ViewNode:
    address: str
    cls: str
    frame: Frame
    text: str | None
    accessibility_id: str | None
    hidden: bool
    children: tuple["ViewNode", ...] = ()

@dataclass(frozen=True)
class VCNode:
    address: str
    cls: str
    title: str | None
    presented: "VCNode | None" = None
    children: tuple["VCNode", ...] = ()

@dataclass(frozen=True)
class TapResult:
    target_address: str
    method: Literal["public_api", "gesture_reflection", "coordinate"]
    handled_by: str | None
```

**`core/client.py`**（关键：返回 dataclass，抛 typed exception，不打印不退出）
```python
class InspectorClient:
    def __init__(self, host="localhost", port=8765, timeout=10):
        self._t = Transport(host, port, timeout)

    def ping(self) -> dict: return self._t.get("/api/ping")

    def view_hierarchy(self, depth=8, include_hidden=False) -> ViewNode:
        raw = self._t.get("/api/view_hierarchy",
                          {"depth": depth, "include_hidden": include_hidden})
        return ViewNode.from_dict(raw)

    def view_search(self, *, cls=None, text=None,
                    accessibility_id=None, tag=None) -> list[ViewNode]: ...

    def tap(self, *, address=None, x=None, y=None) -> TapResult: ...
    def view_modify(self, address, prop, value) -> ModifyResult: ...
    # ... 其余 endpoint 同样改造
```

**`core/transport.py`**（重试策略由幂等性决定）
```python
class Transport:
    SAFE_METHODS = {"GET"}  # 只对幂等请求重试

    def get(self, path, params=None, *, retries=3): ...
    def post(self, path, body=None, *, idempotent=False, retries=0): ...
```

**`core/xcode.py`**
```python
class XcodeController:
    def build(self, scheme, destination) -> BuildResult:
        # 优先 xcodebuild CLI（结构化输出）
        # 失败时降级到 AppleScript + 后续 build_log_summary 验证
        ...
    def run(self, ...) -> RunResult: ...
    def parse_build_log(self, *, scheme, files=None) -> BuildLogSummary: ...
```

---

### L2 — Session（世界状态）

**`session/session.py`**
```python
class InspectorSession:
    def __init__(self, client: InspectorClient, workdir: Path):
        self.client = client
        self.workdir = workdir          # 每次 run 一个目录
        self._cache = TTLCache(default_ttl=2.0)
        self._undo_stack: list[Modification] = []
        self._screen_size: tuple[int,int] | None = None

    # — 状态查询（带缓存）—
    def vc_hierarchy(self, *, fresh=False) -> VCNode: ...
    def view_hierarchy(self, depth=8, *, fresh=False) -> ViewNode: ...
    def screenshot(self, *, label="snap") -> Path:
        # 自动归档到 workdir/screens/{ts}_{label}.jpg
        ...

    # — 高级查询 —
    def find(self, *, text=None, cls=None, aid=None,
             prefer_visible=True) -> list[ViewNode]: ...

    def diff_vc(self, before: VCNode, after: VCNode) -> VCDiff: ...

    # — 修改栈 —
    def modify(self, address, prop, value):
        original = self._snapshot_property(address, prop)
        self.client.view_modify(address, prop, value)
        self._undo_stack.append(Modification(address, prop, original))

    def rollback_all(self): ...    # agent 退出/异常时调用

    # — 生命周期 —
    def __enter__(self): return self
    def __exit__(self, *_):
        self.rollback_all()
```

---

### L3 — Actions（带验证的最小语义单元）

**`actions/base.py`**
```python
class ActionResult:
    ok: bool
    data: Any
    artifacts: list[Path]   # 截屏、log
    duration_ms: float
    notes: str

class Action:
    name: str
    idempotent: bool = False
    requires_app_running: bool = True

    def run(self, session: InspectorSession, **kwargs) -> ActionResult: ...

# 验证装饰器：动作执行前后自动 snapshot，并比对
def verify(post: Callable[[Snapshot, Snapshot], bool], 
           on_fail: Literal["raise", "mark"] = "raise"):
    def decorator(fn): ...
```

**`actions/interact.py`**
```python
class TapAction(Action):
    name = "tap"
    idempotent = False    # 不能自动重试

    @verify(post=lambda b, a: b.vc != a.vc or b.screen_hash != a.screen_hash)
    def run(self, session, *, address=None, text=None, x=None, y=None):
        # 1. 解析目标：如果传 text，先 find + rank 选出最佳候选
        # 2. 截屏 before
        # 3. client.tap(...)
        # 4. 截屏 after
        # 5. verify 装饰器自动比对
        ...
```

**`actions/inspect.py`**
```python
class FindViewAction(Action):
    """语义化查找：text/aid/cls 任一组合 → 排序后的候选列表
       在 session 缓存里查 → 没有再请求 → 仍没有则升级 hierarchy depth
    """
    idempotent = True
    ...

class WaitForAction(Action):
    """轮询直到某个条件成立（页面切换/element 出现/element 消失）"""
    ...
```

---

### L4 — Intents（多步 recipe，可被 LLM 直接调度）

**`intents/base.py`**
```python
class Intent:
    name: str
    description: str        # 给 LLM 看的说明
    args_schema: dict       # JSON schema
    
    def execute(self, session, **kwargs) -> IntentResult: ...
```

**`intents/find_and_tap.py`**
```python
class FindAndTap(Intent):
    name = "find_and_tap"
    description = "Find a UI element by text/aid/class and tap it. Verifies that VC or screen changed."

    def execute(self, session, *, query: str, expect_change=True):
        candidates = FindViewAction().run(session, text=query).data
        if not candidates:
            # 升级策略：增大 depth → 改用截屏 + 视觉定位
            return IntentResult.failure("no_match", suggestions=[...])
        if len(candidates) > 1 and not self._is_dominant(candidates):
            return IntentResult.ambiguous(candidates)
        result = TapAction().run(session, address=candidates[0].address)
        if expect_change and not result.ok:
            return IntentResult.failure("no_state_change_after_tap")
        return IntentResult.ok(result)
```

**`intents/rebuild_and_check.py`**（替代原来"strict verification"散文）
```python
class RebuildAndCheck(Intent):
    """改完代码后的标准化验证流：
       1. xcodebuild build
       2. 解析 build log，对 touched files 做 error 过滤
       3. 若有 error → 直接返回（不要 run）
       4. xcodebuild run / 或 AppleScript run
       5. wait inspector
       6. 截屏 + vc dump
    """
    def execute(self, session, *, scheme, touched_files): ...
```

---

### L5 — Agent Loop

**`agent/loop.py`**
```python
class AgentLoop:
    def __init__(self, llm: LLMClient, session: InspectorSession,
                 policy: Policy, recorder: Recorder):
        self.llm = llm
        self.session = session
        self.policy = policy
        self.recorder = recorder
        self.scratchpad = Memory()

    def run(self, user_goal: str, *, max_steps=20) -> AgentReport:
        self.scratchpad.set_goal(user_goal)
        for step in range(max_steps):
            self.policy.check_budget(step)

            # 1. think — LLM 决定下一步
            decision = self.llm.plan(
                goal=user_goal,
                history=self.scratchpad.history(),
                available_tools=ToolRegistry.schemas(),
                last_observation=self.scratchpad.last(),
            )
            if decision.kind == "finish":
                break

            # 2. safety gate
            self.policy.authorize(decision)   # 可能抛 NeedConfirmation

            # 3. act
            with self.recorder.step(decision) as step_rec:
                try:
                    obs = ToolRegistry.dispatch(decision, self.session)
                    step_rec.observe(obs)
                except InspectorError as e:
                    obs = Observation.from_error(e)
                    step_rec.error(e)

            # 4. reflect
            self.scratchpad.append(decision, obs)
            if obs.is_terminal_failure():
                break

        return self.recorder.finalize(self.scratchpad)
```

**`agent/tools.py`** — 把 Action/Intent 自动转成 LLM tool schema
```python
class ToolRegistry:
    _tools: dict[str, Action | Intent] = {}

    @classmethod
    def register(cls, tool): ...

    @classmethod
    def schemas(cls) -> list[dict]:
        # 输出 OpenAI / Anthropic 兼容的 tool schema
        return [t.to_schema() for t in cls._tools.values()]

    @classmethod
    def dispatch(cls, decision, session) -> Observation: ...
```

**`agent/policy.py`**
```python
class Policy:
    max_steps: int = 20
    max_taps: int = 30
    max_modifications: int = 10
    require_confirm_for: set[str] = {"open_url", "view_modify"}
    confirm_predicate: Callable                 # 注入：CLI=同步问、API=raise

    def authorize(self, decision: Decision):
        if decision.tool in self.require_confirm_for:
            if not self.confirm_predicate(decision):
                raise PermissionDenied(...)
        ...
```

---

### L6 — Safety

**`safety/guard.py`**
```python
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}

DANGEROUS_ROUTE_PATTERNS = [
    r"wipe", r"clear_cache", r"logout", r"delete_account",
    r"internal_debug", r"hard_reset",
]

def validate_host(host: str): ...
def classify_route(url: str) -> RouteRisk: ...
```

**`safety/audit.py`**
```python
class AuditLog:
    """所有访问敏感数据源的动作都记日志：
       keychain_keys, user_defaults (key 含 token/secret/password),
       network_log (含 Authorization header)
    """
    path = Path("~/.ios-inspector/audit.jsonl").expanduser()
    def record(self, who, what, args, result_summary): ...
```

---

### L7 — Trace

**`trace/recorder.py`**
```python
class Recorder:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.trace_path = workdir / "trace.jsonl"
        self.steps: list[StepRecord] = []

    @contextmanager
    def step(self, decision):
        rec = StepRecord(decision)
        try:
            yield rec
        finally:
            rec.finalize()
            self.steps.append(rec)
            self._append(rec)

    def finalize(self, scratchpad) -> AgentReport:
        # 生成 report.md，含每步 before/after 截屏 inline、关键 view 高亮
        ...
```

**`trace/reporter.py`**
```python
class Reporter:
    def render_markdown(self, steps: list[StepRecord]) -> str:
        """
        # iOS Inspector Run Report
        Goal: ...
        ## Step 1 [find_and_tap query=立即购买]
        - Candidates: 12 → ranked → picked 0x... [@(20,300,80,40)]
        - Method: public_api
        - Verified: vc unchanged, but screen_hash changed → success
        ![before](screens/01_before.jpg) ![after](screens/01_after.jpg)
        ...
        """
```

---

### L8 — LLM 适配

**`llm/base.py`**
```python
class LLMClient(ABC):
    @abstractmethod
    def plan(self, *, goal, history, available_tools, last_observation) -> Decision: ...

class Decision:
    kind: Literal["tool_call", "finish", "ask_user"]
    tool: str | None
    arguments: dict
    rationale: str   # 用于 trace
```

**Anthropic / OpenAI 各实现一份**，调用 native tool-use API。Prompt 模板放 `prompts/system.md`，把 SKILL.md 里那些"recommended order"和"reporting discipline"沉淀进去。

---

## 3. 关键设计决策（为什么这么分层）

| 决策 | 理由 |
|---|---|
| **Core 层不打印不退出** | 否则 agent 永远拿不到结构化错误 → 无法决策 |
| **Session 必须 with-block 使用** | 异常时自动 rollback view_modify，不会留脏状态 |
| **Action 标 idempotent 标志** | Transport 重试只对幂等 GET 生效；tap/post 不能重试 |
| **@verify 装饰器内置 before/after 比对** | 把"reporting discipline"从 prompt 变成代码 |
| **Intent 是一等公民，不只是 prompt 模板** | LLM 可直接调用 `find_and_tap`，少一层规划失误 |
| **Policy 注入 confirm_predicate** | 同一份代码同时支持 CLI 交互、CI 全自动、API 异步审批 |
| **LLMClient 抽象** | 不绑定 provider；测试时 mock 一个 ScriptedLLM 即可端到端跑通 |
| **Recorder 是 contextmanager** | 异常也能落 trace，调试最关键 |
| **Workdir per-run** | trace + 截屏 + audit 都在同一目录；CI 可整目录 archive |

---

## 4. 配置 & 入口

**`config.py`**
```python
@dataclass
class Config:
    inspector_host: str = "localhost"
    inspector_port: int = 8765
    xcode_scheme: str | None = None
    xcode_destination: str | None = None
    workdir_root: Path = Path("~/.ios-inspector/runs").expanduser()
    llm_provider: Literal["anthropic", "openai", "scripted"] = "anthropic"
    llm_model: str = "claude-sonnet-4"
    max_steps: int = 20
    require_confirm: set[str] = field(default_factory=lambda: {"open_url", "view_modify"})

    @classmethod
    def load(cls) -> "Config":
        # env > ~/.ios-inspector/config.toml > defaults
```

**`cli.py`**
```bash
# 单步工具（保留原 skill 风格，CI 友好）
ios-inspector tap --text "立即购买"
ios-inspector snapshot --output ./snap

# Agent 模式
ios-inspector agent "找到首页购买按钮并点进去，确认进入会员页"
ios-inspector agent --intent rebuild_and_check --scheme StoryAIMainland \
                   --files PaymentVC.swift

# 诊断
ios-inspector doctor
```

---

## 5. 测试策略

| 层 | 测试方式 |
|---|---|
| Core | 录制 HTTP fixtures + replay；100% 单测覆盖 |
| Session | mock client，验证缓存命中、rollback、diff 正确性 |
| Action | mock session，验证 verify 装饰器在异常路径下也能 raise/mark |
| Intent | 真 simulator 起 SAInspector，跑 happy path + 至少 2 条失败路径 |
| Agent | `ScriptedLLM` 喂预设 decision 序列，端到端测 loop/policy/trace |

---

## 6. 实施顺序（建议 4 个 milestone）

| M | 内容 | 产出 |
|---|---|---|
| **M1** | L1 Core + L2 Session + CLI（替换原 inspector_http.py） | 已经比原 skill 强：结构化错误、缓存、自动归档 |
| **M2** | L3 Actions + L7 Trace | 单步动作带验证 + 自动报告 |
| **M3** | L4 Intents + L8 LLM + L5 Loop | 真正的 agent，可跑自然语言任务 |
| **M4** | L6 Safety + Policy + Audit | 上生产前的护栏 |

每个 milestone 都能独立交付价值——M1 完成时已经可以替换现有 skill 给团队用了。

---

## 7. 与现有 skill 的迁移路径

1. 保留 `SKILL.md` 但内容改为指向 `ios-inspector agent` CLI
2. `scripts/inspector_http.py` 用 `ios_inspector_agent.cli` 重新实现（向后兼容子命令名）
3. 删除 `scripts/lldb/`、`assets/swift-server/`
4. `xcode_control.sh` 用 Python 重写进 `core/xcode.py`，shell 脚本仅留极薄一层 wrapper（可选）

---