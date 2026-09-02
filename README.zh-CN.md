# entropy-arb

**[English documentation / 英文文档 → README.md](README.md)**

开源双交易所永续合约套利机器人。其中一条腿永远是 **Entropy**（Hyperliquid 上的
`io` builder dex）；另一条腿（对冲腿）三选一：

| `--hedge` | 交易所 | 计价货币 | 吃单费 | 协议 |
|---|---|---|---|---|
| `lighter` | Lighter 主网 | USDC | 0 bps | zkLighter ws（增量订单簿，异步结算） |
| `lighter-rh` | Lighter Robinhood 链 | **USDG** | 0 bps | zkLighter ws |
| `tradexyz` | Hyperliquid trade.xyz dex | USDC | ~1 bps | HL l2Book，IOC 同步结算 |

> **推荐链接** —— 通过以下链接注册即可支持本项目：
> - Entropy — Tier 4 推荐，100% 返佣：<https://entropy.io/?r=miluer>
> - Lighter：<https://app.lighter.xyz/?referral=MILUER&source=none>

## 本 Fork 的改进

- **单实例锁** —— 重复启动会干净退出；崩溃残留的锁文件可自动识别并恢复（兼容 Windows）。
- **`--preflight` 启动预检** —— 密钥、市场状态、保证金与上限（支持 Hyperliquid
  **unifiedAccount**：spot 里的 USDC 直接计为可用保证金）、交易所实价手续费、
  采集数据新鲜度与中枢漂移，共 12 项检查。
- **分时段阈值** —— `by_session` 支持盘中独立中枢与带宽（如美股时段放宽带、
  中枢下移），周末自动回退全局带。
- **手续费/资金费率自动发现** —— 运行时从交易所拉取实际费率（配置值仅作兜底），TUI 可见。
- **TUI 仪表盘** —— 任意终端高度精确铺满无空白、超长日志换行不截断、会话栏实时显示
  交易时段与 1 小时中枢偏移，状态行含账本 `pnl 已实现/浮动/当日/手续费`（`--cn` 切换中文）。
- **数据驱动阈值** —— `auto_midline` / `auto_band` 每分钟用稳定窗口的分位数重新
  锚定中枢与带宽；中枢缓慢漂移时冻结调参、武装**漂移锁**（禁止逆着漂移方向开新仓，
  平仓不受限），冻结持续超过 `auto_frozen_fallback_min` 分钟后改从最近 60 分钟
  重锚，而不是抱着过时种子继续交易。
- **执行安全闸门** —— `min_net_edge_bps` 净期望地板（费前预期低于地板不开仓，
  平仓永不拦截）、`max_top_premium_bps` 幻象天花板（大到只成交一条腿的"价差"是
  旧盘口不是机会）、`min_cross_rounds` 薄簿闸门（深度穿不过 N 个最小单不开仓）。
- **Maker 阶梯**（`maker_enabled`）—— 被天花板拦下的肥价差改为在滞后那一边以
  **被动挂单**等待回踩：真错位会以 maker 价成交并立刻补对冲腿；快速行情则永不成交、
  超时撤单零成本。第二腿在第一条腿成交前绝不触碰——无裸腿窗口。
- **风控与出场** —— 按均价法的已实现/未实现盈亏账本（手续费与对冲出血在付出的
  瞬间入账）、UTC 日亏损断路器（`daily_max_loss_usd`，触发即双边 reduce-only
  强平并停机）、与入场带解耦的出场（`reversion_close_bps` / `timeout_close_min`：
  溢价回中枢或持仓超时即按"仅费"门槛强制放行平仓，锚点漂移永不搁浅库存）。
- **韧性** —— Hyperliquid `/info` 串行限速+429 罚时+同请求合并缓存、行情喂线
  静默看门狗（30 秒无帧强制重连）、共享 nonce 分配器、交易所故障暂停/探测、
  对账绝不覆盖新鲜成交、Lighter 结算先等账户流就绪再发单（杜绝假"未确认"重发）。
- **数据工具链** —— 分钟级采集器、分时段分析器（`tools/analyze.py`）、带宽回测器
  （`tools/backtest.py`，可镜像实盘闸门回放）、资金费/基差判定（`tools/funding_check.py`）、
  漂移监控模块，119 个 pytest 用例全覆盖。
- **Windows 启动器** —— `run-live.ps1`：预检通过才启动 + 崩溃自动重启，退出码干净传递。

当同一品种在一边贵、另一边便宜时，机器人同时在贵的一边卖出、便宜的一边买入
（默认双边吃单；开启 maker 阶梯后，过肥的价差改为被动挂单等回踩），持有
delta 中性仓位，等溢价回归后反向平仓。所有交易决策使用的
价格都来自**将要实际成交的那个交易所的真实订单簿**——Hyperliquid 的盘口来自
官方 websocket（`wss://api.hyperliquid.xyz/ws`），Lighter 的盘口来自 Lighter
官方 websocket。

机器人运行期间（即使没有密钥、没有开策略）会自动把两边盘口记录成**分钟级
CSV 数据**，配套的分析工具可以直接把这些数据变成策略所需的三个核心参数。

## 信号逻辑

整个信号就是 `config.yaml` 里三个数字，由你根据采集的数据自己设定：

```
premium_bps =（Entropy 价格 / 对冲腿价格 − 1）× 10 000

                          ┌──────────────  卖出 Entropy + 买入对冲腿
midline + upper  ───────────────────────────────────────────────────
                                       ▲
midline          ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┼ ─ ─   溢价的长期中枢
                                       ▼
midline − lower  ───────────────────────────────────────────────────
                          └──────────────  买入 Entropy + 卖出对冲腿
```

- `midline_bps` —— 溢价的常态水平。跨所溢价几乎从不以零为中心（预言机不同、
  计价货币不同、新上市溢价等），零中心的带只会朝一个方向开仓、打满仓位上限、
  永远无法平仓。请实际测量溢价所在的位置，然后填入。
- `upper_bps` / `lower_bps` —— 中枢上下两侧的入场带宽。

两个方向的门槛都作用于**可实际成交的价格**（Entropy 买一 对 对冲腿卖一，
反之亦然），并且是**扣除双边吃单手续费之后的净门槛**——引擎会在阈值之上
另行叠加手续费。因此一次完整往返扣费后**净赚 ≥ upper + lower bps**，这是
结构上保证的。

有一点必须理解：当 `midline_bps: 5` 时，买入 Entropy 的门槛是
`lower − midline`，可能为**负数**。这是有意为之——如果 Entropy 长期贵 5 bps，
那么在溢价为 0 时买入它，相对其自身均衡水平就是便宜了 5 bps，这笔交易正是
此前在 `midline + upper` 处卖出的获利平仓。这同时意味着**中枢填错就是亏钱
策略**：若真实溢价中枢是 0 而你填了 5，机器人会整天以公允价买入 Entropy。
先测量、再交易——数据采集器和分析工具就是为此而生。

## 快速开始

```bash
git clone https://github.com/Miluer-tcq/entropy-arb.git && cd entropy-arb
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # 数据采集只需要这些

cp config.example.yaml config.yaml       # 策略配置（阈值、规模、风控）
cp .env.example .env                     # 密钥——交易必填
```

交易哪个市场**不在**配置文件中——每次启动时用命令行参数显式指定：
`--symbol`（两个交易所共同交易的品种）和 `--hedge`（三选一：
`lighter`、`lighter-rh`、`tradexyz`；Entropy 永远是
另一条腿）。

本机器人**没有模拟盘**——要么采集数据（`--record-only`），要么实盘交易。
请用采集的数据和最小的仓位上限来验证策略，而不是模拟成交。

**第一步：先采集数据**（不需要任何密钥）：

```bash
python3 main.py --record-only --symbol SNDK --hedge lighter-rh
```

至少运行几个小时（最好一整天——溢价存在日内规律），数据写入
`logs/minutes.csv`。

**第二步：分析数据、设定阈值：**

```bash
python3 tools/analyze.py
```

它会输出溢价分布、各档带宽的历史触发频率，以及可直接粘贴进
`config.yaml` 的 `thresholds:` 配置块。

**第三步：实盘** —— 填写 `.env`，安装签名 SDK，仓位上限从刚好满足
交易所最小名义的水平开始：

```bash
pip install -r requirements-live.txt
python3 main.py --symbol SNDK --hedge lighter-rh
```

不带 `--record-only` 运行时，只要两边行情就绪且溢价越过带宽，就会立即
发送真实订单。

**仪表盘。** 在终端运行时会显示实时 Rich 仪表盘：两边盘口（含数据龄/点差）、
持仓与上限、账户权益与本次会话盈亏、两个方向的可成交溢价对比完整门槛
（已含手续费与库存加价，● 表示已武装）、数据采集进度、最近成交，以及日志
尾部（完整日志写入 `logging.file`，默认 `logs/engine.log`）。`--record-only`
模式同样可用。加 `--cn` 参数可使仪表盘全部以中文显示。`--no-dashboard`
可切换为纯日志输出（nohup/systemd 等非终端环境会自动退回纯日志），也可
设置 `logging.dashboard: false`。

## 数据采集与分析

采集器在所有模式下自动运行（`recorder.enabled: true`）：每秒采样一次两边
的真实盘口，每分钟写一行：

| 列 | 含义 |
|---|---|
| `minute_ts`, `time_utc` | 分钟起点（epoch 秒 / ISO UTC） |
| `entropy_bid/ask`, `hedge_bid/ask` | 该分钟最后一次有效盘口 |
| `premium_open/high/low/close/mean/std_bps` | Entropy 相对对冲腿的中间价溢价 |
| `sell_edge_mean/max_bps` | 卖出 Entropy 方向的可成交溢价（Entropy 买一 / 对冲腿卖一 − 1） |
| `buy_edge_mean/max_bps` | 买入 Entropy 方向的可成交溢价（对冲腿买一 / Entropy 卖一 − 1） |
| `samples` | 该分钟约 60 秒中两边盘口同时有效的秒数 |

采集的 edge 为费前口径；分析工具在统计触发频率前会先扣除 `--fees-bps`
（请传入**两边吃单费之和**——零费交易所默认 0.0，对冲腿为 `tradexyz` 时
约为 1.0），因此其表格与建议值可直接填入配置。`--hours 24`
可只分析最近数据；溢价中枢会漂移，请定期重新分析并更新 `config.yaml`。

## 配置说明

策略在 `config.yaml`（严格校验——未知键名直接报错），密钥在 `.env`。
交易市场由命令行指定（`--symbol`、`--hedge`）。完整的双语注释参考：
[config.example.yaml](config.example.yaml)。核心项：

| 键 | 含义 | 默认值 |
|---|---|---|
| `thresholds.midline_bps` | 溢价中枢（必须实测！） | — |
| `thresholds.upper_bps` / `lower_bps` | 入场带宽（> 0） | — |
| `entropy.dex` | Entropy 在 Hyperliquid 上的 dex 名 | `io` |
| `*.taker_fee_bps` | 各所吃单费 | 0.0（tradexyz 对冲腿：1.0） |
| `*.max_position_usd` | 各所持仓上限 | 1000 |
| `*.max_orders_per_min` | 各所每分钟下单预算（滑动 60 秒） | 120；Lighter 对冲腿 30 |
| `sizing.take_fraction` | 吃掉可套利深度的比例 | 0.5 |
| `sizing.max_order_notional_usd` | 单笔名义上限 | 500 |
| `inventory.scale_bps` / `floor_frac` | 库存阶梯（仓位超过上限的 `floor_frac` 后额外加价） | 10 / 0.5 |
| `execution.premium_persist_sec` | 信号需持续多久才触发 | 0.3 |
| `execution.daily_max_loss_usd` | UTC 日亏损（已实现+浮动）达到即双边强平并停机（0 = 关闭） | 0 |
| `execution.reversion_close_bps` / `timeout_close_min` | 解耦出场：溢价回到中枢 ±bps 内、或持仓超过 N 分钟，即强制放行平仓 | 0 / 0 |
| `execution.min_cross_rounds` | 可穿深度不足 N 个交易所最小单时拒绝开仓（平仓豁免） | 0 |
| `execution.*` | 滑点保护、超时、对账周期等 | 见配置文件 |
| `recorder.*` | 分钟数据采集器 | 开启，`logs/minutes.csv` |
| `logging.dashboard` / `logging.file` | 终端仪表盘；开启时日志写入文件 | 开启，`logs/engine.log` |

数据驱动阈值与安全闸门键（均在 `thresholds:` 下，默认全部关闭/保守）：

| 键 | 含义 | 默认值 |
|---|---|---|
| `auto_midline` | 每分钟用稳定窗口中位数重锚 `midline_bps` | false |
| `auto_midline_clamp_bps` | 距手动锚点的最大偏移（手动值仍是先验） | 3.0 |
| `auto_band` / `auto_band_trigger_pct` | 自动设带宽，使每侧大约在该 % 的分钟数上触发 | false / 10.0 |
| `auto_band_floor_bps` / `auto_band_ceiling_bps` | 带宽上下限 | 2.0 / 8.0 |
| `auto_frozen_fallback_min` | 漂移冻结超过该分钟后，改从最近 60 分钟重锚 | 30 |
| `min_net_edge_bps` | 开仓的费后净期望硬地板（平仓不受限） | 0 |
| `max_top_premium_bps` | 幻象天花板：超过此宽度的"价差"是旧盘口，不是机会 | 0 |
| `maker_enabled` / `maker_wait_sec` | 天花板拦截改为在滞后所被动挂单等待，成交才补对冲腿 | false / 3.0 |

## 密钥配置（`.env`，仅实盘需要）

- **Entropy / tradexyz（Hyperliquid）** —— 在
  <https://app.hyperliquid.xyz/API> 创建 API（agent）钱包。`HL_PRIVATE_KEY`
  填 **agent 钱包私钥**，`HL_ACCOUNT_ADDRESS` 填主账户地址。当
  `--hedge tradexyz` 时两条腿默认共用该账户（内部自动共享 nonce 序列）；
  如需分开，设置 `HL_PRIVATE_KEY_XYZ` / `HL_ACCOUNT_ADDRESS_XYZ`。注意给
  所交易的各 dex 分别充入保证金。
- **Lighter** —— `LIGHTER_ACCOUNT_INDEX`、`LIGHTER_API_KEY_INDEX`、
  `LIGHTER_API_PRIVATE_KEY`，必须注册在与启动参数 `--hedge` **相同的部署**上
  （主网与 Robinhood 链是两套独立的账户和密钥——参见
  [lighter-python](https://github.com/elliottech/lighter-python)）。

## 执行机制

- 两条腿默认**同时发出吃单**：Lighter 用带均价保护的市价单，在鉴权 websocket
  上异步确认成交；Hyperliquid 用 IOC 限价单同步结算（结果未知时轮询
  orderStatus 兜底）。
- **安全闸门**（信号到下单之间）：`min_net_edge_bps` 净期望地板（只挡开仓、
  平仓永远放行）、`max_top_premium_bps` 幻象天花板、`min_cross_rounds` 薄簿闸门。
- **Maker 阶梯**（`maker_enabled`）：被天花板拦下的肥价差不再丢弃，而是改在
  滞后的那一边以**被动挂单**挂在过时报价上等回踩——真错位会以 maker 价成交并
  立即补对冲腿；快速行情则永远不成交、超时撤单零成本。第二腿在第一条腿成交
  前绝不触碰——无裸腿窗口。
- **持续性闸门**（`premium_persist_sec`）：信号先"武装"，持续存在才触发，
  过滤单 tick 的假信号。
- **库存阶梯**：仓位超过上限的 `floor_frac` 后，同方向加仓需要线性递增的
  额外溢价，满仓时最高加 `scale_bps`。
- **解耦出场**（`reversion_close_bps` / `timeout_close_min`）：平仓不再等入场
  带——溢价回到中枢、或持仓超时，就以"仅费"门槛强制放行平仓，锚点漂移绝不会
  把库存搁浅在场上。
- **净敞口对冲**：两腿成交不对等时立即用 reduce-only 单（带滑点保护）
  削减敞口，并每 `reconcile_sec` 与链上仓位对账。
- **故障隔离**：被限频的交易所短暂暂停；交易所不可达（如例行维护）时暂停
  交易并每 `venue_probe_sec` 探测直至恢复；连续 `max_consecutive_errors`
  次执行异常则整体停机；`daily_max_loss_usd` 触发 UTC 日亏损断路器——
  双边 reduce-only 强平后停机。
- **仅实盘**：没有模拟成交模式。`--record-only` 是唯一无风险的运行方式，
  其余都是真金白银。

## 目录结构

```
main.py                  入口（--record-only，默认即实盘）
entropy_arb/config.py    YAML + .env 配置契约与校验
entropy_arb/book.py      订单簿 + 含手续费的套利规模计算
entropy_arb/feeds.py     官方 HL ws + zkLighter ws 行情（含静默看门狗）
entropy_arb/venue_hl.py  Hyperliquid dex 适配器（Entropy、tradexyz）+ maker 挂单
entropy_arb/venue_lighter.py  zkLighter 适配器（主网、Robinhood 链）
entropy_arb/engine.py    双交易所策略主循环、闸门、maker 阶梯、风控
entropy_arb/ledger.py    按均价法的双所盈亏账本（已实现 + 手续费）
entropy_arb/monitor.py   稳定窗口、漂移报告、自动中枢/带宽目标
entropy_arb/preflight.py --preflight 启动预检
entropy_arb/dashboard.py Rich 终端仪表盘
entropy_arb/recorder.py  分钟级盘口数据采集
tools/analyze.py         minutes.csv -> 阈值建议
tools/backtest.py        带宽回放（可镜像实盘闸门）
tools/funding_check.py   判定溢价是资金费 carry 还是结构性基差
tools/session_stats.py   分时段溢价统计
tests/                   python3 -m pytest tests/
```

## 已知风险

- **中枢填错就是亏钱策略。** 溢价中枢会漂移，请定期重新测量并保持
  `config.yaml` 与市场同步（或开启 `auto_midline`/`auto_band` 并留意漂移锁日志）。
- **USDG 基差**（`lighter-rh`）：对冲腿以 USDG 计价，持续溢价中有
  一部分是稳定币本身的基差；midline 吸收其水平，但 USDG 的*变动*是真实盈亏。
- **资金费**：两个交易所、两套独立的资金费率，持仓成本未建模——仓位上限
  请设小一些。（在 entropy-io:SNDK vs Lighter 上的实测：资金费仅贡献约
  0.6 bps/天，而基差在 −4 bps 量级——这是流动性基差，不是 carry。换市场
  前请先跑 `tools/funding_check.py` 再下结论。）
- **薄盘口**：Entropy 深度可能很小；`take_fraction` 与名义上限控制单笔规模，
  但部分成交后对冲腿的滑点是真实存在的。
- **交易时段**：股票类永续（如 SNDK）盘后各所预言机行为不同，建议加宽带宽
  或避开盘后。
- **单腿风险**：一条腿成交后另一条可能失败。机器人会自动对冲并对账，但
  仍需人工关注。

风险自负。本软件直接操作真实资金，本文档不构成任何投资建议。请从最小的
仓位上限开始。

## 开源协议

[MIT](LICENSE)
