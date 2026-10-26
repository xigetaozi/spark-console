# 核心实现思路与踩坑记录

> 这份文档记录的是**问题怎么被发现、为什么这么解**，而不是 API 说明。如果你要魔改，先读这里。

---

## 一、整体状态模型

系统把数据分成两块，**刻意不放在一起**：

| 文件 | 内容 | 谁写 |
|---|---|---|
| `data/config.json` | 用户配置：名单、文案、定时、间隔 | 面板（用户操作） |
| `data/runtime.json` | 运行状态：是否在跑、上次结果、联系人缓存 | 程序 |

**为什么要分开**：早期两者混在一个文件里，导致「程序在跑时保存配置」会覆盖运行状态，反过来又会被运行状态覆盖用户配置。分开后各自有独立的写锁，互不干扰。

`runtime.json` 里有个坑：`running` 标志被持久化，进程被 `kill -9` 时 `finally` 来不及执行，重启后它就永远是 `true`——面板显示"运行中"，按钮一直 disabled，用户看到的是"点了没反应"。

**结论**：任何持久化的"瞬态标志"，启动时必须强制清一次。

```python
# app.py 启动时
if load_runtime().get("running"):
    set_running(False)
    logger.info("启动时清理残留的运行标志")
```

---

## 二、抖音网页版的 DOM 特点

观察结论（会随版本变化，但模式稳定）：

| 元素 | 选择器 | 备注 |
|---|---|---|
| 会话行标题 | `.conversationConversationItemtitle` | 名字或抖音号 |
| 火花标签 | `.commonStreaknormalText` | **异步渲染**，可能晚几百毫秒 |
| 会话行容器 | `[class*="conversationConversationItem"]` | ⚠️ 标题自身类名也含这个串 |
| 输入框 | `div[contenteditable="true"]` | 聊天区唯一可编辑节点 |

**第一个陷阱**：想用 `titleEl.closest('[class*="conversationConversationItem"]')` 定位行容器，结果是**标题元素自己**——因为 `conversationConversationItemtitle` 包含这个子串。查找范围等于零。

**正确做法**：向上爬，直到"父级出现第二个标题"为止，这个边界正好是单行：

```javascript
let box = t.parentElement;
while (box && box.parentElement &&
       box.parentElement.querySelectorAll('.conversationConversationItemtitle').length <= 1) {
    box = box.parentElement;
}
```

---

## 三、异步渲染：整条链路踩了三次坑

这是本项目最难的部分。

### 现象

抓 20 个会话，能正确拿到火花状态的只有 8~14 个，其余 `streak` 为空 → 被"只发有火花的"过滤器判为无火花 → **漏发真实好友**。

### 错误尝试 1：向上多找几层

以为火花标签在更外层，于是从父级向上找 4 层。

结果：**找到了，但找到了错的那一行**。整个列表容器里每行都有火花标签，`querySelector` 返回第一个，于是所有行都读到同一个值（全部变成 102 / 26）。比漏发更糟——会**发错人**。

> 教训：跨容器查找必然串行。查找范围必须严格限定在单行内。

### 错误尝试 2：按"数据没变化"停止滚动

第二阶段补数据时，加了"连续两轮没变化就停"的优化。

结果：虚拟列表滚过的新行本来就没有新数据，第一屏扫完 `changed=false`，第二轮就退出了，**三轮都在同一屏打转，2.6 秒结束**。

> 教训：虚拟列表的"无变化"不代表"扫完了"，要用**滚动进度**而非数据变化作为终止条件。

### 最终方案：两阶段 + 固定步数 + 按名合并

```
阶段一（收集名单）
  向下滚，每次快照所有可见行，按 name 合并进 collected
  —— 目标是"知道有哪些人"

阶段二（补火花）
  回顶部，最多扫 3 轮，每轮 28 步：
    1. wait_for_selector('[class*="treak"]', 600ms)   ← 屏内等渲染
    2. evaluate(extract)                              ← 取当前屏
    3. 按 name 合并：只补空值，不覆盖已有值
    4. wheel(480)、wait(500ms)                        ← 下移一屏
  整轮结束若"会话数 + 火花数"都没增长 → 提前退出
```

三个关键点：

**① 屏内等待**。每屏取值前先 `wait_for_selector` 给异步渲染留时间。没有这一步，取到的全是空值。

**② 按名合并，只补空**。早期是 `[x for x in data if x not in collected]` —— 按整条记录判重。第一轮抓到的空 streak 会被永久固化，后面拿到真值也进不去。

```python
elif not old["streak"] and new["streak"]:
    old["streak"] = new["streak"]   # 只补空，不覆盖
```

**③ 缓存回退**。即使这样，仍可能有个别行这轮没渲染出来。所以发送前过滤时会拿"上一轮成功抓取"做兜底：

```python
def filter_by_spark(targets, contacts, previous=None):
    # 本轮抓不到火花，但上一轮确认过有 → 沿用上次判定
    if t in prev_ok: keep.append(t)
```

原则是：**宁可用旧数据，不可漏发真实好友；但从未确认过有火花的，一律跳过**（不对无火花的人发消息）。

---

## 四、元素"存在但不可见"

现象：

```
Locator.click: Element is not visible
  - locator resolved to <div class="conversationConversationItemtitle">张三</div>
  - attempting click action
```

元素**解析成功了**，但虚拟列表里它在视口外，被判定为不可见，点击直接失败。`force=True` 也没用——它只跳过可操作性检查，仍然需要一个有效 bounding box。

三级兜底：

```python
try: el.scroll_into_view_if_needed(timeout=3000)
except: pass
try:
    el.click(timeout=6000)          # 正常点击（触发完整事件链）
except:
    el.evaluate("(e) => e.click()") # JS 点击：不要求可见，只要能拿到节点
```

JS 点击的副作用是绕过了一些浏览器默认行为检查，所以只在常规点击失败后才用。

---

## 五、反检测

机房 IP 天然的劣势，能做的只有降低"一眼就是脚本"的特征：

```python
browser.new_context(
    viewport={"width": 1366, "height": 900},
    locale="zh-CN",
    timezone_id="Asia/Shanghai",
    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) ... Chrome/xxx",
    extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
)
page.add_init_script("""
  Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
  Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});
  Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
  window.chrome = window.chrome || {runtime: {}};
""")
```

启动参数加 `--disable-blink-features=AutomationControlled`。

**效果与边界**：滑块验证的出现率明显下降，但**不能消除**。而且出现过另一种拦截：点击「登录」后**页面完全不弹登录框**（没有任何 dialog/canvas 元素）——这是 IP 级的风控，纯前端伪装解决不了。所以扫码登录设计成"本机出码、服务器复用登录态"。

---

## 六、登录态的生命周期

```
本机扫码（有桌面） → storage_state() → state.json
        ↓ 上传
服务器 data/state.json  → 每个任务复用一个 context
        ↓ 每次任务前检查
检查点：sessionid cookie 是否存在 / 页面是否被踢回登录页
        ↓ 写入 runtime
state_expire_at / state_expire_days → 面板倒计时告警
```

抖音的 `sessionid` 有效期通常 2~3 个月。面板提前 7 天告警，避免"某天突然全部失败"。

**安全**：`state.json` 等同于账号密码。`chmod 600`、不进版本库、不通过聊天工具传输。

---

## 七、调度与并发

### 互斥

```python
if not run_lock.acquire(blocking=False):
    raise HTTPException(409, "已有任务在运行")
```

用 `threading.Lock` 的 `blocking=False` 做"尝试获取"，比 `if running:` 检查更可靠——避免了检查与设置之间的竞态。

### 抖动（Delay）

定时任务不是准点执行，而是 `random.uniform(0, jitter*60)` 秒后再开始。目的是让每天的发送时间不固定，降低被识别为机器行为的概率。

**踩过的坑**：

```python
jitter = max(0, int(cfg.get("jitter_minutes", 30) or 30))   # ❌
```

`0 or 30` 得 30 —— 用户把抖动设成 0（想准点发），实际被强制改成 30 分钟，而且**日志里写的是"抖动窗口 30 分钟"，用户以为是自己的错觉**。

```python
raw = cfg.get("jitter_minutes", 30)
jitter = 30 if raw is None else max(0, int(raw))   # ✅
```

> 教训：`or` 做默认值，遇到 `0` / `""` / `[]` 会静默走默认分支。数值配置一律用 `is None` 判断。

### 两套间隔

| 场景 | 配置 | 典型值 |
|---|---|---|
| 定时任务 | `send_gap_min/max` | 180~300 秒 |
| 手动「立即发送」 | `manual_gap_min/max` | 15~35 秒 |

定时任务慢是为了安全（模拟真人），手动快是为了调试体验。

---

## 八、多实例隔离

用 systemd 模板实例，一个模板跑 N 个账号：

```ini
# /etc/systemd/system/douyin-spark@.service
[Service]
EnvironmentFile=/etc/douyin-spark/%i.env
WorkingDirectory=/opt/douyin-spark
ExecStart=/opt/douyin-spark/.venv/bin/python app.py
```

每个实例通过环境变量隔离：

```
/etc/douyin-spark/a.env   PORT=8000  SPARK_DATA_DIR=/opt/douyin-spark-data/a
/etc/douyin-spark/b.env   PORT=8001  SPARK_DATA_DIR=/opt/douyin-spark-data/b
```

`DATA_DIR` 由 `core/config.py` 读取，所有数据文件（config/runtime/state/logs）都挂在它下面。这样代码只有一份，数据完全隔离。

---

## 九、控制台的令牌注入

面板本身有 `AUTH_TOKEN` 鉴权，但用户不想在 iframe 里再输一次令牌。

做法：控制台代理 `/p/{acc}/*` → 转发到 `127.0.0.1:{port}/*`，同时**在请求头注入该账号的令牌**：

```python
hdrs = {k: v for k, v in hdrs.items() if k.lower() != "x-auth-token"}
hdrs["X-Auth-Token"] = token
```

面板前端把令牌存在 `sessionStorage`，控制台加载 iframe 前用 `postMessage` 或 URL 参数注入一次。这样：
- 浏览器里**不落盘**令牌
- 8000/8001 端口**只监听 127.0.0.1**，公网只能通过控制台访问

> 踩坑：面板登录态用的是 `sessionStorage`，而 iframe 是**跨文档**的，父页面写的值子页面读不到。最后改成由面板自己在 `sessionStorage` 里写一个占位值，配合控制台注入的 header，绕过令牌弹窗。

---

## 十、可复用的抽象

如果要把这套东西用到别的网页自动化上，可以抽出来的三件套：

1. **稳定定位器**：单行容器边界 + 多候选字段（标题 → title 属性 → img alt → aria-label）
2. **异步列表抓取器**：两阶段 + 屏内等待 + 按名合并 + 多轮补齐
3. **健壮点击器**：scroll → click → JS click 三级兜底

这三样跟具体平台无关，只是本项目用抖音当样本实现了一遍。
