## 🚀 HH 自动续期（GitHub Actions）

全自动登录 + 续期。结构参考 [katabump-renew](https://github.com/saodisengyyds/katabump-renew)。

---

## 工作原理

登录方式是 **手机号 + 邮箱验证码**。

验证码是一长串 base64 令牌，解码后形如：

```
<PASSWORD>              ← 约 60 字符的 base64 串
        ↓ base64 解码
<手机号>:::<随机串>
```

脚本不关心解码内容，只负责把这一整串原样填进验证码框。

所以**不需要手动登录、不需要 cookie**，脚本全自动：

```
1. 打开 /login，填手机号，提交
2. 轮询邮箱，等主题含验证码关键词的最新邮件
3. 提取 base64 令牌，填进验证码框
4. 登录成功 → 打开 /vps-renew/
5. 解析页面上的到期日，存到 hh_state.json
6. 距到期 ≤2 天才续期，否则跳过（每天检查一次）
7. 续期后记录新的到期日，下次继续
```

---

## 🔐 Secrets 配置

仓库 → **Settings → Secrets and variables → Actions → New repository secret**

| Secret 名称 | 必填 | 说明 |
|---|---|---|
| `HH_SITE` | ✅ | 站点地址，如 `https://example.com` |
| `HH_PHONE` | ✅ | 登录手机号 |
| `IMAP_HOST` | ✅ | 邮箱 IMAP，如 `imap.gmail.com` |
| `IMAP_PORT` | ⬜ | 默认 `993` |
| `EMAIL_USER` | ✅ | 收件邮箱 |
| `EMAIL_APP_PASSWORD` | ✅ | 邮箱**应用密码** |
| `MAIL_SUBJECT` | ⬜ | 验证码邮件主题关键词，默认 `verification code` |
| `NODE_LINK` | 推荐 | 代理节点（过 Cloudflare） |
| `TG_BOT_TOKEN` | ⬜ | Telegram Bot Token |
| `TG_CHAT_ID` | ⬜ | 接收通知的 Chat ID |
| `DUMP_PAGE` | ⬜ | 设为 `1` 打印页面结构 |

---

## 📧 邮箱应用密码（Gmail）

**必须用应用密码，不能用登录密码。**

1. 开启两步验证：https://myaccount.google.com/security
2. 访问 https://myaccount.google.com/apppasswords
3. 生成 16 位密码（形如 `abcd efgh ijkl mnop`）
4. **去掉空格**填到 `EMAIL_APP_PASSWORD`

```
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
EMAIL_USER=你的邮箱
EMAIL_APP_PASSWORD=***（16位应用密码）
```

QQ 邮箱：设置 → 账户 → 开启 IMAP → 生成授权码，`IMAP_HOST=imap.qq.com`。

---

## 🕐 续期时机

```yaml
- cron: '10 1 * * *'   # UTC 01:10 = 北京 09:10，每天一次
```

脚本自己判断要不要续：

```
📅 记录到期日: 2026-11-06（还剩 5 天）
⏭️  还没到提前 2 天的窗口，跳过
```

```
📅 记录到期日: 2026-11-06（还剩 2 天）
✅ 进入续期窗口（≤2 天）
```

到期日存在 `hh_state.json`，每次运行后自动 commit 回仓库，所以状态会累积。

**手动强制续期**：Actions → Run workflow → `force` 填 `true`。

---

## 📩 通知示例

```
🟣 HH 续期通知

✅ 续期成功
📅 新到期日: 2026-11-13
⏱️ 2026-10-30 09:12:33

📄 Renewed successfully. Your VPS now expires on 2026-11-13.
```

| 图标 | 含义 |
|---|---|
| ✅ | 续期成功 |
| ⏳ | 未到续期时间（正常） |
| ❌ | 登录失败 / 找不到表单 / 页面报错 |
| ❓ | 结果不明确 |

---

## 🌐 代理节点

`NODE_LINK` 填 v2rayN 里能用的分享链接：

```
vless://uuid@server:port?security=reality&sni=...
vmess://base64...
trojan://password@server:port?sni=...
tuic://uuid:password@server:port...
hysteria2://base64@server:port...
anytls://uuid@server:port...
socks5://user:pass@server:port
```

**建议配。** 实测 GitHub runner 访问 `/vps-renew/` 会被 Cloudflare 拦（`Just a moment...`），`/login` 是通的。

---

## 🐛 选择器失效怎么办

改版后报「找不到续期表单 / 找不到提交按钮」：

把 Secret `DUMP_PAGE` 设为 `1`，重跑。日志会打印：

```
===== DUMP [login] =====
URL   : https://example.com/login
TITLE : Login
FORMS : 1
  IN  {'tag':'input','id':'phone','name':'phone','type':'tel',...}
  BTN {'tag':'button','type':'submit','text':'Send Code'}
TEXT  : Enter your phone number...
===== END =====
```

照着改 `app.py`：

| 找什么 | 改哪里 |
|---|---|
| 手机号输入框 | `login()` 里 `phone_sels` |
| 验证码输入框 | `login()` 里 `code_sels` |
| 网站地址框 | `renew()` 里 `input[name="web_address"]` |
| 协议勾选 | `renew()` 里 `input[name="agreement"]` |
| 提交按钮 | `renew()` 里 `button[name="submit_button"]` |

---

## 🔍 关键实现细节

**验证码提取**（`_extract_token`）

令牌是 40+ 位 base64。脚本会：
- 剥掉 HTML 标签
- 找所有 40+ 位的 base64 串
- 要求同时含数字和字母（排除纯数字长串）
- 尝试解码，解码后含 6+ 位数字的优先

**到期日解析**（`parse_expiry`）

支持这些格式，8 个用例全部通过：

```
✅ Your VPS expires on 2026-11-06      → 2026-11-06
✅ Expiry date: 2026/11/13             → 2026-11-13
✅ 到期时间：2026-11-20                   → 2026-11-20
✅ Next renewal 06/11/2026             → 2026-11-06
✅ Renewal: 13/11/2026                 → 2026-11-13
✅ Expires: 2026-11-06 23:59:59        → 2026-11-06
✅ No date here                        → None
✅ Expired on 2020-01-01               → None（过去日期过滤）
```

---

## 📁 文件

| 文件 | 说明 |
|---|---|
| `app.py` | 主脚本 |
| `.github/workflows/renew.yml` | Actions 工作流 |
| `hh_state.json` | 到期日状态（自动生成并提交） |
| `app-legacy.js` | 早期 Puppeteer 版，留档 |

---

## ⚠️ 注意

- 自动化可能违反服务条款，风险自负
- 每天跑一次足够，别高频
- 邮箱应用密码等同邮箱访问权限，只存 Secrets
- 失败时自动上传截图
- 工作流只保留最近 5 条运行记录
