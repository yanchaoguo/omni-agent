# -*- coding: utf-8 -*-
"""连接器(外部应用): Webhook / 飞书 / 企业微信 / 邮件(SMTP) 推送交付物"""
import json
import smtplib
import time
import urllib.request
from email.header import Header
from email.mime.text import MIMEText

from .config import UA


class Connectors:
    def __init__(self, settings):
        self.cfg = settings.get("connectors", {})

    def available(self):
        return list(self.cfg.keys())

    @staticmethod
    def _post_json(url, payload):
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": UA},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:300]

    def push(self, channel, title, content):
        cfg = self.cfg.get(channel)
        if not cfg:
            return f"[错误] 连接器 '{channel}' 未配置(可用: {self.available() or '无'})"
        try:
            text = f"{title}\n{content}"[:3800]
            if channel == "feishu":
                st, body = self._post_json(cfg["url"], {"msg_type": "text",
                                                        "content": {"text": text}})
            elif channel == "wecom":
                st, body = self._post_json(cfg["url"], {"msgtype": "text",
                                                        "text": {"content": text}})
            elif channel == "webhook":
                st, body = self._post_json(cfg["url"], {"title": title, "content": content,
                                                        "source": "omni-agent",
                                                        "ts": int(time.time())})
            elif channel == "email":
                msg = MIMEText(content, "plain", "utf-8")
                msg["Subject"] = Header(title, "utf-8")
                msg["From"] = cfg["user"]
                msg["To"] = cfg["to"]
                port = int(cfg.get("smtp_port", 465))
                cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
                with cls(cfg["smtp_host"], port, timeout=20) as s:
                    if port != 465:
                        s.starttls()
                    s.login(cfg["user"], cfg["password"])
                    s.sendmail(cfg["user"], [cfg["to"]], msg.as_string())
                st, body = 250, "sent"
            else:
                return f"[错误] 未知连接器类型: {channel}"
            return f"[成功] 已通过 {channel} 推送 (status={st}) {body}"
        except Exception as e:
            return f"[失败] {channel} 推送异常: {type(e).__name__}: {e}"
