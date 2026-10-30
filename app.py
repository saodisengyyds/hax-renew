#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HH 自动续期

登录：手机号 + 邮箱验证码
续期：到期前 2 天自动执行

环境变量
--------
必填
  HH_SITE               站点地址，如 https://example.com
  HH_PHONE              手机号
  IMAP_HOST             邮箱 IMAP，如 imap.gmail.com
  IMAP_PORT             默认 993
  EMAIL_USER            收件邮箱
  EMAIL_APP_PASSWORD    邮箱应用密码

可选
  MAIL_SUBJECT          验证码邮件的主题关键词，默认 "verification code"
  NODE_LINK             代理节点
  IS_PROXY / PROXY_SERVER
  TG_BOT_TOKEN / TG_CHAT_ID
  DUMP_PAGE=1           打印页面结构
  FORCE_RENEW=1         忽略到期日，强制续期
"""

import os
import re
import sys
import json
import time
import base64
import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

import requests
from seleniumbase import SB

# ==================== 配置 ====================

SITE = (os.environ.get("HH_SITE") or "").strip().rstrip("/")
PHONE = (os.environ.get("HH_PHONE") or "").strip()

IMAP_HOST = os.environ.get("IMAP_HOST") or ""
IMAP_PORT = int(os.environ.get("IMAP_PORT") or "993")
EMAIL_USER = os.environ.get("EMAIL_USER") or ""
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD") or ""

MAIL_SUBJECT = os.environ.get("MAIL_SUBJECT") or "verification code"

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""
TG_CHAT_ID = os.environ.get("TG_CHAT_ID") or ""

DUMP_PAGE = os.environ.get("DUMP_PAGE", "0").lower() in ("1", "true", "yes")
FORCE_RENEW = os.environ.get("FORCE_RENEW", "0").lower() in ("1", "true", "yes")

LOGIN_PATH = "/login"
RENEW_PATH = "/vps-renew/"
SITE_HOST = re.sub(r"^https?://", "", SITE).split("/")[0] if SITE else ""

STATE_FILE = "hh_state.json"      # 记录到期日
RENEW_AHEAD_DAYS = 2              # 提前几天续期

CST = timezone(timedelta(hours=8))


def now_cst():
    return datetime.now(CST)


def login_url():
    return SITE + LOGIN_PATH


def renew_url():
    return SITE + RENEW_PATH


# ==================== Telegram ====================

def send_tg(status_icon: str, status_text: str, detail: str = ""):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ℹ️  未配置 TG，跳过推送")
        return
    text = (
        f"🟣 HH 续期通知\n\n"
        f"{status_icon} {status_text}\n"
        f"⏱️ {now_cst().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    if detail:
        text += f"\n\n📄 {detail[:400]}"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text},
            timeout=15,
        )
        print("📩 TG 已发送" if r.status_code == 200
              else f"⚠️  TG 失败: {r.status_code}")
    except Exception as e:
        print(f"⚠️  TG 异常: {e}")


# ==================== 状态（到期日） ====================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    try:
        json.dump(state, open(STATE_FILE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"💾 已保存状态: {state}")
    except Exception as e:
        print(f"⚠️  保存状态失败: {e}")


def parse_expiry(text: str):
    """从页面文本里解析到期日，返回 date 或 None"""
    if not text:
        return None

    pats = [
        r"(?:expir\w*|renew\w*|到期|过期)[^\d]{0,30}(\d{4})[-/](\d{1,2})[-/](\d{1,2})",
        r"(?:expir\w*|renew\w*|到期|过期)[^\d]{0,30}(\d{1,2})[-/](\d{1,2})[-/](\d{4})",
        r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})",
    ]

    for i, pat in enumerate(pats):
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        try:
            if i == 1:
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            dt = datetime(y, mo, d).date()
            today = now_cst().date()
            if today <= dt <= today + timedelta(days=730):
                return dt
        except Exception:
            continue
    return None


# ==================== 邮箱验证码 ====================

def _decode_mime(s):
    if not s:
        return ""
    out = []
    for txt, enc in decode_header(s):
        if isinstance(txt, bytes):
            try:
                out.append(txt.decode(enc or "utf-8", errors="ignore"))
            except Exception:
                out.append(txt.decode("utf-8", errors="ignore"))
        else:
            out.append(txt)
    return "".join(out)


# 验证码是一长串 base64（约 60 字符），解码后形如 手机号:::nonce
_TOKEN_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


def _extract_token(text: str):
    """从邮件正文里提取验证令牌"""
    if not text:
        return None

    # 去掉 HTML 标签和常见包裹符号
    clean = re.sub(r"<[^>]+>", " ", text)

    cands = _TOKEN_RE.findall(clean)
    best = None
    for c in cands:
        # 排除纯数字、纯字母的偶然长串
        if not re.search(r"[0-9]", c) or not re.search(r"[A-Za-z]", c):
            continue
        # 尝试解码，成功的更可信
        try:
            dec = base64.b64decode(c + "=" * (-len(c) % 4)).decode("utf-8")
            if re.search(r"\d{6,}", dec):
                return c
            if best is None:
                best = c
        except Exception:
            if best is None:
                best = c
    return best


def fetch_code(max_wait: int = 180, interval: int = 8):
    """轮询邮箱，取最新的验证码"""
    if not (IMAP_HOST and EMAIL_USER and EMAIL_APP_PASSWORD):
        print("❌ 未配置邮箱，无法取验证码")
        return None

    print(f"📬 轮询邮箱 {IMAP_HOST}，等待主题含「{MAIL_SUBJECT}」的邮件…")
    deadline = time.time() + max_wait
    seen = set()

    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            M.login(EMAIL_USER, EMAIL_APP_PASSWORD)
            M.select("INBOX")

            typ, data = M.search(None, "ALL")
            if typ != "OK":
                M.logout()
                time.sleep(interval)
                continue

            for uid in reversed(data[0].split()[-25:]):
                if uid in seen:
                    continue
                seen.add(uid)

                typ, msg_data = M.fetch(uid, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue

                msg = email.message_from_bytes(msg_data[0][1])
                subject = _decode_mime(msg.get("Subject", ""))

                # 按主题匹配
                if MAIL_SUBJECT and MAIL_SUBJECT.lower() not in subject.lower():
                    continue

                # 只认 15 分钟内的
                try:
                    dh = msg.get("Date")
                    if dh:
                        dt = parsedate_to_datetime(dh)
                        if dt.tzinfo:
                            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                        if datetime.utcnow() - dt > timedelta(minutes=15):
                            continue
                except Exception:
                    pass

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() in ("text/plain", "text/html"):
                            try:
                                p = part.get_payload(decode=True)
                                if p:
                                    body += p.decode(
                                        part.get_content_charset() or "utf-8",
                                        errors="ignore")
                            except Exception:
                                pass
                else:
                    try:
                        p = msg.get_payload(decode=True)
                        if p:
                            body = p.decode(
                                msg.get_content_charset() or "utf-8",
                                errors="ignore")
                    except Exception:
                        pass

                token = _extract_token(body)
                if token:
                    print(f"✅ 取到验证码: {token[:16]}…（{len(token)} 字符）")
                    print(f"   主题: {subject}")
                    M.logout()
                    return token

            M.logout()
        except Exception as e:
            print(f"⚠️  邮箱异常: {e}")

        print(f"⏳ 暂无邮件，{interval}s 后重试…")
        time.sleep(interval)

    print("❌ 等待验证码超时")
    return None


# ==================== 页面工具 ====================

def dump_page(sb, tag):
    if not DUMP_PAGE:
        return
    print(f"\n===== DUMP [{tag}] =====")
    try:
        print(f"URL   : {sb.get_current_url()}")
        print(f"TITLE : {sb.get_title()}")
        info = sb.execute_script(r"""
        (function(){
          function d(el){return {tag:el.tagName.toLowerCase(),id:el.id||'',
            name:el.getAttribute('name')||'',type:el.getAttribute('type')||'',
            ph:el.getAttribute('placeholder')||'',
            text:(el.innerText||el.value||'').trim().slice(0,40)};}
          var o={inputs:[],buttons:[],links:[],forms:0,bodyLen:0,bodyText:''};
          document.querySelectorAll('input,textarea,select').forEach(function(e){
            if(o.inputs.length<25)o.inputs.push(d(e));});
          document.querySelectorAll('button,input[type=submit],a').forEach(function(e){
            if(o.buttons.length<25)o.buttons.push(d(e));});
          document.querySelectorAll('a[href]').forEach(function(e){
            if(o.links.length<20)o.links.push({t:(e.innerText||'').trim().slice(0,30),h:e.getAttribute('href')});});
          o.forms=document.querySelectorAll('form').length;
          var b=document.body;
          o.bodyLen=b?(b.innerText||'').length:0;
          o.bodyText=b?(b.innerText||'').replace(/[\s\u00a0]+/g,' ').slice(0,800):'';
          return o;})()
        """)

        if not isinstance(info, dict):
            print("  ⚠️  execute_script 返回", type(info).__name__, "，改用 Selenium 取值")
            try:
                print("  BODY  :", (sb.get_text("body") or "").replace("\n", " | ")[:1200])
                print("  SOURCE:", (sb.get_page_source() or "")[:2000])
            except Exception as e2:
                print("  取值也失败:", e2)
            print("===== END =====\n")
            return

        # 兜底：即使 info 拿到了，正文太短也打印源码
        if info.get("bodyLen", 0) < 60:
            print("  ⚠️  正文过短，打印源码片段：")
            try:
                print("  SOURCE:", (sb.get_page_source() or "")[:2000])
            except Exception:
                pass

        # 无论哪种情况，都单独抓一次表单区域
        try:
            forminfo = sb.execute_script(r"""
            (function(){
              var out={forms:[],inputs:[],iframes:[],scripts:[],bodyTail:''};
              document.querySelectorAll('form').forEach(function(f){
                out.forms.push({action:f.getAttribute('action')||'',
                                method:f.getAttribute('method')||'',
                                id:f.id||'',cls:f.className||'',
                                html:f.outerHTML.slice(0,600)});
              });
              document.querySelectorAll('input,textarea,select,button').forEach(function(e){
                out.inputs.push({tag:e.tagName.toLowerCase(),type:e.getAttribute('type')||'',
                  name:e.getAttribute('name')||'',id:e.id||'',
                  ph:e.getAttribute('placeholder')||'',
                  visible: e.offsetParent!==null});
              });
              document.querySelectorAll('iframe').forEach(function(f){
                out.iframes.push(f.src||'(no src)');
              });
              document.querySelectorAll('script[src]').forEach(function(sc){
                var s=sc.getAttribute('src')||'';
                if(/login|auth|main|app|index/i.test(s)) out.scripts.push(s);
              });
              var b=document.body;
              if(b) out.bodyTail=(b.innerText||'').slice(-600);
              return out;})()
            """)
            if isinstance(forminfo, dict):
                print("  FORMS 数:", len(forminfo.get("forms", [])))
                for f in forminfo.get("forms", []):
                    print("   FORM:", f)
                print("  INPUTS 数:", len(forminfo.get("inputs", [])))
                for i in forminfo.get("inputs", [])[:15]:
                    print("   IN  :", i)
                print("  IFRAMES:", forminfo.get("iframes", []))
                print("  SCRIPTS:", forminfo.get("scripts", []))
                print("  TAIL  :", forminfo.get("bodyTail", "")[:400])
        except Exception as e:
            print("  表单抓取失败:", e)

        print("FORMS :", info.get("forms"), "| bodyLen:", info.get("bodyLen"))
        for i in info.get("inputs", []):
            print("  IN ", i)
        for b in info.get("buttons", []):
            print("  BTN", b)
        for l in info.get("links", []):
            print("  A  ", l)
        print("TEXT  :", info.get("bodyText", "")[:700])
    except Exception as e:
        print("dump 失败:", e)
    print("===== END =====\n")


def _wait_ready(sb, timeout=45):
    """等到页面不是 Cloudflare 挑战页，且正文有内容"""
    print("⏳ 等待页面…")
    for i in range(timeout):
        try:
            title = (sb.get_title() or "").lower()
            body = (sb.get_text("body") or "").strip()
            blocked = ("just a moment" in title
                       or "checking your browser" in title
                       or "attention required" in title
                       or "请稍候" in title)
            if not blocked and len(body) > 60:
                print(f"✅ 已加载（{i+1}s）")
                return True
        except Exception:
            pass
        time.sleep(1)
    print("⚠️  可能未完全加载")
    return False


AD_WALL_KWS = [
    "view a short ad", "short ad", "site-wide access",
    "watch ad", "sponsored", "continue to site",
    "看广告", "广告", "访问权限",
]


def _body_text(sb):
    try:
        return (sb.get_text("body") or "")
    except Exception:
        return ""


def _has_login_form(sb):
    """页面上有没有可用的输入框（JS 拿不到就用 WebDriver 兜底）"""
    try:
        r = sb.execute_script("""
        (function(){
          var sels=['input[name="phone"]','input[id="phone"]','input[type="tel"]',
                    'input[name="username"]','input[id="text"]','input[type="text"]',
                    'input[type="email"]','input[type="password"]'];
          for(var i=0;i<sels.length;i++){
            var el=document.querySelector(sels[i]);
            if(el && el.offsetParent!==null) return true;
          }
          return false;})()
        """)
        if r:
            return True
    except Exception:
        pass

    # JS 失败时用 WebDriver 直接找
    try:
        for sel in ["input", "textarea"]:
            for el in sb.find_elements(sel):
                try:
                    if el.is_displayed() and el.is_enabled():
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def _pass_ad_wall(sb, rounds: int = 3) -> bool:
    """
    广告墙：页面只有「View a short ad」之类的按钮，没有登录表单。
    点掉后等页面出现真实表单。
    """
    any_clicked = False

    for r in range(rounds):
        txt = _body_text(sb).lower()
        if not txt:
            time.sleep(2)
            txt = _body_text(sb).lower()

        has_form = _has_login_form(sb)
        has_kw = any(k in txt for k in AD_WALL_KWS)

        if not has_kw and has_form:
            return any_clicked

        if not has_kw and not has_form:
            # 既没关键词也没表单 —— 可能是 CF 或空页，交给上层处理
            return any_clicked

        print(f"📺 广告墙（第 {r+1} 轮），尝试点击…")

        clicked = False

        # 1) 按文本找可点元素（button / a / span / div / input[type=submit]）
        try:
            clicked = bool(sb.execute_script(r"""
            (function(){
              var kws=['view a short ad','short ad','site-wide access',
                       'watch ad','sponsored','continue to site','看广告'];
              var all=document.querySelectorAll('button,a,span,div,input[type=submit]');
              for(var i=0;i<all.length;i++){
                var el=all[i];
                var t=(el.innerText||el.value||'').toLowerCase().trim();
                if(!t) continue;
                for(var k=0;k<kws.length;k++){
                  if(t.indexOf(kws[k])>=0){
                    try{ el.scrollIntoView({block:'center'}); }catch(e){}
                    el.click();
                    var ev=new MouseEvent('click',{bubbles:true,cancelable:true,view:window});
                    el.dispatchEvent(ev);
                    return true;
                  }
                }
              }
              return false;})()
            """))
        except Exception as e:
            print(f"  ⚠️  JS 点击失败: {str(e)[:80]}")

        # 2) 兜底：用 Selenium 逐个元素点
        if not clicked:
            try:
                for sel in ["button", "a", "input[type=submit]"]:
                    for el in sb.find_elements(sel):
                        try:
                            t = (el.text or "").strip().lower()
                            if t and any(k in t for k in AD_WALL_KWS):
                                el.click()
                                clicked = True
                                print(f"  🖱️  {t[:40]}")
                                break
                        except Exception:
                            continue
                    if clicked:
                        break
            except Exception:
                pass

        if clicked:
            any_clicked = True
            print("  ⏳ 等待广告跳转…")
            # 广告可能开新标签
            try:
                handles = sb.driver.window_handles
                if len(handles) > 1:
                    print(f"  🔀 检测到 {len(handles)} 个标签页，切回第一个")
                    sb.driver.switch_to.window(handles[0])
                    for h in handles[1:]:
                        try:
                            sb.driver.switch_to.window(h)
                            sb.driver.close()
                        except Exception:
                            pass
                    sb.driver.switch_to.window(handles[0])
            except Exception:
                pass

            time.sleep(10)
            # 等表单出现
            for _ in range(20):
                if _has_login_form(sb):
                    print("  ✅ 表单已出现")
                    return True
                time.sleep(1)
        else:
            print("  ⚠️  没找到可点元素")
            time.sleep(3)

    return any_clicked


def _fill(sb, selectors, value, label):
    # 1) 按选择器
    for sel in selectors:
        try:
            el = sb.find_element(sel, timeout=3)
            if not el:
                continue
            if not el.is_displayed():
                continue
            try:
                el.clear()
            except Exception:
                pass
            el.click()
            time.sleep(0.2)
            el.send_keys(value)
            print(f"✏️  {label} ({sel})")
            return True
        except Exception:
            continue

    # 2) 兜底：遍历所有可见输入框
    print(f"  ⚠️  选择器都没命中，尝试遍历输入框填 {label}")
    try:
        for sel in ["input", "textarea"]:
            for el in sb.find_elements(sel):
                try:
                    if not (el.is_displayed() and el.is_enabled()):
                        continue
                    t = (el.get_attribute("type") or "").lower()
                    if t in ("hidden", "submit", "button", "checkbox", "radio"):
                        continue
                    el.clear()
                    el.click()
                    time.sleep(0.2)
                    el.send_keys(value)
                    print(f"✏️  {label} (兜底: {sel}, name={el.get_attribute('name')})")
                    return True
                except Exception:
                    continue
    except Exception:
        pass

    print(f"❌ 找不到 {label}")
    return False


def _is_logged_in(sb):
    try:
        url = (sb.get_current_url() or "").lower()
        if "/login" in url:
            return False
        body = (sb.get_text("body") or "").lower()
        if re.search(r"logout|退出|sign out", body):
            return True
        if sb.execute_script(
            "return !!document.querySelector('input[name=\"web_address\"],#web_address')"
        ):
            return True
        return False
    except Exception:
        return False


# ==================== 登录 ====================

def login(sb):
    print(f"\n🌐 打开登录页: {login_url()}")
    try:
        sb.uc_open_with_reconnect(login_url(), reconnect_time=6)
    except Exception:
        sb.open(login_url())
    time.sleep(5)

    _wait_ready(sb, 40)
    _pass_ad_wall(sb, rounds=3)

    if _is_logged_in(sb):
        print("✅ 已有登录态")
        return True

    # 广告墙没点掉时，这里会是空页面
    if not _has_login_form(sb):
        print("⚠️  仍然没有登录表单，再尝试一次广告墙…")
        dump_page(sb, "before-adwall-retry")
        _pass_ad_wall(sb, rounds=3)

    dump_page(sb, "login")

    # ---- 填手机号 ----
    phone_sels = [
        'input[name="phone"]', 'input[id="phone"]',
        'input[type="tel"]', 'input[name="username"]',
        'input[id="text"]', 'input[type="text"]',
    ]
    if not _fill(sb, phone_sels, PHONE, "手机号"):
        dump_page(sb, "no-phone-input")
        return False
    time.sleep(1)

    # ---- 提交手机号 ----
    print("🖱️  提交手机号…")
    sb.execute_script("""
    (function(){
      var bs = document.querySelectorAll('button,input[type=submit]');
      for (var i=0;i<bs.length;i++){
        var t = (bs[i].innerText||bs[i].value||'').toLowerCase();
        if (/send|code|next|continue|login|submit|验证码|发送|下一步|登录/.test(t)){
          bs[i].click(); return true;
        }
      }
      var f = document.querySelector('form');
      if (f) { f.submit(); return true; }
      return false;
    })()
    """)
    time.sleep(6)
    _wait_ready(sb, 20)

    # ---- 拿验证码 ----
    token = fetch_code(max_wait=180, interval=8)
    if not token:
        send_tg("❌", "取不到验证码", "检查 IMAP 配置或邮件主题关键词")
        return False

    # ---- 填验证码 ----
    code_sels = [
        'input[name="code"]', 'input[id="code"]',
        'input[name="token"]', 'input[id="token"]',
        'input[name*="verify"]', 'input[id*="verify"]',
        'input[name*="otp"]', 'input[id*="otp"]',
        'input[type="text"]',
    ]
    if not _fill(sb, code_sels, token, "验证码"):
        dump_page(sb, "no-code-input")
        return False
    time.sleep(1)

    print("🖱️  提交验证码…")
    sb.execute_script("""
    (function(){
      var bs = document.querySelectorAll('button,input[type=submit]');
      for (var i=0;i<bs.length;i++){
        var t = (bs[i].innerText||bs[i].value||'').toLowerCase();
        if (/submit|verify|login|continue|确认|登录|验证/.test(t)){
          bs[i].click(); return true;
        }
      }
      var f = document.querySelector('form');
      if (f) { f.submit(); return true; }
      return false;
    })()
    """)

    print("⏳ 等待登录结果…")
    for i in range(25):
        time.sleep(1)
        _pass_ad_wall(sb)
        if _is_logged_in(sb):
            print(f"✅ 登录成功（{i+1}s）")
            return True

    dump_page(sb, "login-failed")
    print("❌ 登录失败")
    send_tg("❌", "登录失败", "请查看 Actions 日志")
    return False


# ==================== 续期 ====================

def renew(sb):
    print("\n" + "#" * 26)
    print("   续期")
    print("#" * 26)

    print(f"\n🖥️  {renew_url()}")
    try:
        sb.uc_open_with_reconnect(renew_url(), reconnect_time=6)
    except Exception:
        sb.open(renew_url())
    time.sleep(5)

    _wait_ready(sb, 40)
    _pass_ad_wall(sb)

    if "/login" in (sb.get_current_url() or "").lower():
        print("ℹ️  被重定向到登录页，先登录")
        if not login(sb):
            return False, None
        sb.open(renew_url())
        time.sleep(5)
        _wait_ready(sb, 40)

    dump_page(sb, "renew")

    body = sb.get_text("body") or ""
    low = body.lower()

    if re.search(r"can'?t renew|cannot renew|not (yet )?time|无法续期|未到|too early", low):
        print("⏳ 还没到续期时间")
        d = parse_expiry(body)
        send_tg("⏳", "未到续期时间", body[:300])
        return True, d

    if not sb.execute_script(
        "return !!document.querySelector('input[name=\"web_address\"],#web_address')"
    ):
        print("❌ 找不到续期表单")
        send_tg("❌", "找不到续期表单", body[:300])
        return False, None

    # 网站地址
    print("✏️  填网站地址…")
    sb.execute_script("""
    (function(){
      var el = document.querySelector('input[name="web_address"],#web_address');
      if (!el) return false;
      el.value='%s';
      el.dispatchEvent(new Event('input',{bubbles:true}));
      el.dispatchEvent(new Event('change',{bubbles:true}));
      return true;})()
    """ % SITE_HOST)
    time.sleep(1)

    # 协议
    print("☑️  勾协议…")
    sb.execute_script("""
    (function(){
      var ag = document.querySelector('input[name="agreement"]');
      if (ag && !ag.checked){
        ag.checked=true;
        ag.dispatchEvent(new Event('change',{bubbles:true}));
      }})()
    """)
    time.sleep(0.5)

    # 验证码（如果页面要求）
    need_code = sb.execute_script("""
    (function(){
      var sels=['input[name*="code"]','input[id*="code"]','input[name*="otp"]',
                'input[id*="otp"]','input[name*="verify"]','input[id*="verify"]'];
      for(var i=0;i<sels.length;i++){
        var el=document.querySelector(sels[i]);
        if(el && el.offsetParent!==null) return sels[i];
      }
      return null;})()
    """)

    if need_code:
        print(f"🔐 需要验证码: {need_code}")
        token = fetch_code(max_wait=180, interval=8)
        if token:
            _fill(sb, [need_code], token, "验证码")
            time.sleep(1)

    # 提交
    print("🖱️  提交续期…")
    submitted = False
    try:
        btn = sb.find_element('button[name="submit_button"]', timeout=5)
        if btn:
            sb.execute_script("""
            (function(){
              var b=document.querySelector('button[name="submit_button"]');
              if(b) b.scrollIntoView({behavior:'smooth',block:'center'});})()
            """)
            time.sleep(0.8)
            btn.click()
            submitted = True
    except Exception:
        pass

    if not submitted:
        submitted = sb.execute_script("""
        (function(){
          var bs=document.querySelectorAll('button,input[type=submit]');
          for(var i=0;i<bs.length;i++){
            var t=(bs[i].innerText||bs[i].value||'').toLowerCase();
            if(/renew|extend|续期/.test(t)){bs[i].click();return true;}
          }
          return false;})()
        """)

    if not submitted:
        print("❌ 找不到提交按钮")
        send_tg("❌", "找不到续期按钮", body[:300])
        return False, None

    print("⏳ 等待结果…")
    time.sleep(8)
    _pass_ad_wall(sb)

    return _check_result(sb)


def _check_result(sb):
    print("\n📋 检查结果…")
    body = ""
    for _ in range(4):
        try:
            body = sb.get_text("body") or ""
        except Exception:
            body = ""
        if body:
            break
        time.sleep(2)

    low = body.lower()
    expiry = parse_expiry(body)

    if re.search(r"renewed|success|extended|已续期|成功|延长", low):
        msg = "续期成功"
        if expiry:
            msg += f"\n📅 新到期日: {expiry}"
        print(f"✅ {msg}")
        send_tg("✅", msg, body[:300])
        return True, expiry

    if re.search(r"can'?t renew|cannot renew|not (yet )?time|未到|too early", low):
        print("⏳ 未到续期时间")
        send_tg("⏳", "未到续期时间", body[:300])
        return True, expiry

    if re.search(r"error|failed|失败|invalid|无效|expired|过期", low):
        print("❌ 续期失败")
        send_tg("❌", "续期失败", body[:300])
        return False, expiry

    print("❓ 结果不明确")
    dump_page(sb, "result")
    send_tg("❓", "结果不明确，请手动确认", body[:300])
    return False, expiry


# ==================== 入口 ====================

def main():
    print("#" * 26)
    print("   HH 自动续期")
    print("#" * 26)

    missing = []
    if not SITE:
        missing.append("HH_SITE")
    if not PHONE:
        missing.append("HH_PHONE")
    if missing:
        print("❌ 缺少配置: " + " / ".join(missing))
        sys.exit(2)

    print(f"🌐 站点: {SITE}")

    state = load_state()
    expiry_str = state.get("expires")
    today = now_cst().date()

    # ---- 判断是否该续期 ----
    if expiry_str and not FORCE_RENEW:
        try:
            exp = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            days_left = (exp - today).days
            print(f"📅 记录到期日: {exp}（还剩 {days_left} 天）")
            if days_left > RENEW_AHEAD_DAYS:
                print(f"⏭️  还没到提前 {RENEW_AHEAD_DAYS} 天的窗口，跳过")
                print("   （要强制续期：设 FORCE_RENEW=1）")
                sys.exit(0)
            print(f"✅ 进入续期窗口（≤{RENEW_AHEAD_DAYS} 天）")
        except Exception as e:
            print(f"⚠️  状态文件异常，继续尝试续期: {e}")
    elif FORCE_RENEW:
        print("⚡ FORCE_RENEW=1，忽略到期日")
    else:
        print("ℹ️  没有到期日记录，本次先续期并记录")

    is_proxy = os.environ.get("IS_PROXY", "false").lower() == "true"
    proxy_str = (os.environ.get("PROXY_SERVER") or "").strip() or "http://127.0.0.1:1081"
    kw = {"uc": True, "headless": False}
    if is_proxy:
        print(f"🔗 代理: {proxy_str}")
        kw["proxy"] = proxy_str
    else:
        print("🌐 直连")

    ok = False
    new_expiry = None

    with SB(**kw) as sb:
        try:
            sb.open("https://api.ip.sb/ip")
            print(f"📍 出口 IP: {(sb.get_text('body') or '').strip()}")
        except Exception:
            pass

        try:
            if not login(sb):
                sys.exit(5)
            ok, new_expiry = renew(sb)
        except Exception as e:
            print(f"❌ 异常: {e}")
            import traceback
            traceback.print_exc()
            send_tg("❌", "运行异常", str(e)[:300])

    # ---- 记录到期日 ----
    if new_expiry:
        state["expires"] = new_expiry.strftime("%Y-%m-%d")
        state["updated"] = now_cst().strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
    elif ok and expiry_str:
        # 续期成功但没解析到新日期，按 +7 天估算
        try:
            exp = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            state["expires"] = (exp + timedelta(days=7)).strftime("%Y-%m-%d")
            state["updated"] = now_cst().strftime("%Y-%m-%d %H:%M:%S")
            save_state(state)
            print(f"⚠️  未解析到新到期日，按 +7 天估算: {state['expires']}")
        except Exception:
            pass

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
