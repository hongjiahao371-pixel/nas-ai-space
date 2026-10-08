"""Optional durable webhook outbox. No channel configured means no outbound messages."""
from __future__ import annotations
import json,time
from urllib.parse import urlsplit
import httpx
from app.database import utc_now


class NotificationService:
    def __init__(self,database,settings):
        self.database=database;self.url=settings.notification_webhook_url

    def enqueue(self,key,event,title,body):
        if not self.url:return
        payload={'event':event,'title':title[:120],'body':body[:500],'created_at':utc_now(),'source':'NAS AI Space'}
        try:
            self.database.execute("INSERT OR IGNORE INTO external_notifications(event_key,payload,created_at) VALUES (?,?,?)",(key,json.dumps(payload,ensure_ascii=False),utc_now()))
        except Exception:
            import logging
            logging.getLogger(__name__).warning('通知入队失败；原任务状态已保留')

    def flush(self):
        if not self.url:return {'configured':False,'sent':0}
        url=urlsplit(self.url)
        if url.scheme not in {'http','https'} or not url.hostname or url.username or url.password:
            raise ValueError('通知渠道地址无效')
        rows=self.database.fetchall("SELECT * FROM external_notifications WHERE status='pending' AND next_retry<=? ORDER BY id LIMIT 5",(time.time(),))
        sent=0
        with httpx.Client(timeout=5,follow_redirects=False) as client:
            for row in rows:
                attempts=row['attempts']+1
                try:
                    response=client.post(self.url,json=json.loads(row['payload']),headers={'Idempotency-Key':row['event_key']})
                    response.raise_for_status()
                    self.database.execute("UPDATE external_notifications SET status='sent',attempts=?,error='' WHERE id=?",(attempts,row['id']));sent+=1
                except Exception as exc:
                    # Error text never includes the destination URL (which may contain credentials).
                    self.database.execute("UPDATE external_notifications SET attempts=?,next_retry=?,status=?,error=? WHERE id=?",
                      (attempts,time.time()+min(3600,30*2**min(attempts,7)),'failed' if attempts>=8 else 'pending',type(exc).__name__,row['id']))
        return {'configured':True,'sent':sent}

    def status(self):
        return {'configured':bool(self.url),'counts':self.database.fetchall('SELECT status,COUNT(*) AS count FROM external_notifications GROUP BY status')}
