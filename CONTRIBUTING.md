# 参与贡献

感谢你愿意改进这个项目。它是从"踩坑"里长出来的，最欢迎的贡献就是**新的坑**。

## 最有价值的 PR

| 类型 | 说明 |
|---|---|
| **选择器失效修复** | 抖音改版后 `.conversationConversationItemtitle` 之类会失效，这是最容易过期的部分 |
| **异步渲染的边界情况** | 如果你遇到「火花抓不全」但这套方案没覆盖到的场景 |
| **反检测改进** | 但要说明清楚原理，不接受"黑盒魔法" |
| **文档纠错** | 尤其是部署步骤在你环境里跑不通的地方 |

## 提 PR 前请确认

**必须**：

```bash
# 1. 不要带任何真实数据
git diff --name-only | grep -E 'state\.json|\.env|\.key|\.pem|data/'

# 2. 全局搜一遍敏感词
grep -rIn -E "1[3-9][0-9]{9}|[0-9]{17}[0-9Xx]|sk-[a-zA-Z0-9]{20,}" \
     --exclude-dir=.git --exclude-dir=.venv .
```

**应该**：

- 改动的代码能通过 `python -m py_compile <文件>`
- 涉及行为的改动，在 PR 描述里写清楚**怎么验证的**
- 新增配置项要同步更新 `.env.example` 和 README 的配置表

## 分支与提交

```
main              保持可运行
feature/<简述>    新功能
fix/<简述>        bug 修复
```

提交信息用 [Conventional Commits](https://www.conventionalcommits.org/)：

```
fix(automation): 修复火花异步渲染导致漏发
feat(console): 新增账号健康度倒计时
docs(deploy): 补充 Playwright 镜像源说明
```

`scope` 取值：`console` / `panel` / `automation` / `watcher` / `reply` / `scheduler` / `docs`

## 代码风格

- Python：4 空格，行宽 100，函数上方一句话说清"为什么这么写"而不是"做了什么"
- 前端：单文件 Vue，不引入新的构建工具
- **不要**新增第三方依赖，除非它能替掉一大段自己写的脆弱逻辑

## 不要提交的东西

```
data/           登录态、配置、运行时状态
.env            环境变量
*.key *.pem     证书私钥
.workbuddy/     本地项目数据
任何截图        可能包含好友昵称
```

## 报告问题

提 Issue 时请附上：

1. 现象（哪个环节失败）
2. 日志片段（`journalctl` 或面板日志，**先删掉昵称和账号信息**）
3. 你的环境（系统、Python 版本、是否服务器 IP）
