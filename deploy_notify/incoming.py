"""Read only the configured group's recent messages through the installed CLI."""
import json
from pathlib import Path
import re
import subprocess

from .tracker import status_text


def is_status_command(content, bot_name, mentioned=False):
    # Match one complete command, never arbitrary chat text or instructions.
    if not isinstance(content, str):
        return False
    if mentioned and re.fullmatch(r'\s*状态\s*[？?]?\s*', content):
        return True
    return re.fullmatch(r'\s*@' + re.escape(bot_name) + r'\s+状态\s*[？?]?\s*', content) is not None


class WelinkHistory:
    def __init__(self, settings, receiver):
        self.settings, self.receiver = settings, receiver

    def fetch(self):
        executable = str(Path(self.settings['executable']).resolve())
        result = subprocess.run(
            [executable, 'im', 'query-history-message',
             '--group-id', self.receiver, '--query-count', '100'],
            executable=executable, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise ValueError('HISTORY_UNAVAILABLE')
        data = json.loads(result.stdout.decode('utf-8-sig'))
        if str(data.get('resultCode')) != '0':
            raise ValueError('HISTORY_REJECTED')
        messages = data['respData']['chatInfo']
        if not isinstance(messages, list):
            raise ValueError('HISTORY_INVALID')
        return messages


def consume(tracker, messages, settings, snapshot, now):
    cursor = tracker.state.get('message_cursor')
    receiver = tracker.config['backend']['receiver']
    rows = sorted((m for m in messages if type(m.get('msgId')) is int
                   and str(m.get('groupId')) == receiver), key=lambda m: m['msgId'])
    if cursor is None:
        # A new installation starts at the tail; do not answer old group messages.
        tracker.state['message_cursor'] = max((m['msgId'] for m in rows), default=0)
        tracker.state['incoming'] = 'READY'
        return
    for row in rows:
        if row['msgId'] <= cursor:
            continue
        tracker.state['message_cursor'] = row['msgId']
        if row.get('sender') == settings['bot_uid']:
            text = row.get('content', '')
            if isinstance(text, str):
                for event in tracker.state['outbox']:
                    # The service appends its attribution after the original content.
                    if event['status'] in ('accepted', 'uncertain') and (
                            text == event['content'] or text.startswith(event['content'] + '\n')):
                        event.update(status='delivered', receipt_message_id=row['msgId'], completed_epoch=now)
                        tracker.state['delivery'] = 'DELIVERED_GROUP_RECEIPT'
            continue
        mentions = row.get('atAccountList', [])
        if isinstance(mentions, str):
            mentions = re.split(r'[,;\s]+', mentions)
        mentioned = isinstance(mentions, list) and settings['bot_uid'] in mentions
        if not is_status_command(row.get('content', ''), settings['bot_name'], mentioned):
            continue
        # Old commands missed during downtime should not flood the group on restart.
        stamp = row.get('serverSendTime', 0)
        if type(stamp) not in (int, float):
            continue
        stamp /= 1000
        if not 0 <= now - stamp <= 120:
            continue
        tracker.enqueue('STATUS_REPLY', status_text(tracker.config, snapshot, now), now)
    tracker.state['incoming'] = 'READY'
