"""Dual-backend outage tracking and redacted status messages."""
import ipaddress
import re
import time
import uuid

from .usage import lines as usage_lines

IPV4 = re.compile(r'(?<!\d)(?:\d{1,3}\.){3}(\d{1,3})(?!\d)')
IPV6 = re.compile(r'(?<![0-9A-Fa-f:])[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]+(?:%[\w.-]+)?')


def redact(text):
    def hide_ipv6(match):
        address = match.group().rstrip('.')
        try:
            ipaddress.IPv6Address(address)
        except ValueError:
            return match.group()
        return '[IP_REDACTED]' + match.group()[len(address):]
    text = IPV6.sub(hide_ipv6, text)
    return IPV4.sub(lambda m: '*.*.*.' + m.group(1), text)


def event_content(message, now):
    stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))
    return redact(message) + '\n事件时间：' + stamp


def routing_text(labels, snapshot, health):
    if snapshot.get('routing_mode') == 'session_affinity':
        available = [labels[p] for p in labels if health.get(p) is True]
        return '会话均衡（' + '、'.join(available) + '）' if available else '无可用后端'
    active_port = str(snapshot.get('active_port'))
    return labels[active_port] if health.get(active_port) else (
        '等待切换至健康后端' if any(health.values()) else '无可用后端')


class Tracker:
    def __init__(self, config, state=None):
        self.config = config
        self.state = state if state is not None else {
            'version': 1, 'nodes': {}, 'both': {}, 'outbox': [], 'last_sample': None,
        }
        for port in config['nodes']:
            self.state['nodes'].setdefault(port, {})

    def enqueue(self, kind, message, now):
        item = {'id': uuid.uuid4().hex, 'kind': kind, 'content': event_content(message, now),
                'epoch': now, 'status': 'pending', 'attempts': 0, 'next_try': now}
        self.state['outbox'].append(item)
        return item

    def observe(self, snapshot, now):
        try:
            sample_time = float(snapshot['epoch'])
            fresh = 0 <= now - sample_time <= self.config['freshness_seconds']
            raw = {str(row['port']): row['healthy'] for row in snapshot['backends']}
            observed = {p: raw.get(p) for p in self.config['nodes']}
            fresh = fresh and all(type(observed[p]) is bool for p in self.config['nodes'])
        except (KeyError, TypeError, ValueError):
            fresh = False
        if not fresh:
            self.state['observation'] = 'STALE_OR_MISSING'
            # Do not turn an old snapshot into a machine outage or recovery.
            for node in [*self.state['nodes'].values(), self.state['both']]:
                node['bad_since'] = None
                node['good_since'] = None
            return []
        previous = self.state.get('last_sample')
        previous_snapshot = self.state.get('last_snapshot_epoch')
        if (previous_snapshot is None or (previous is not None and (
                now < previous or now - previous > self.config['freshness_seconds']))):
            for node in [*self.state['nodes'].values(), self.state['both']]:
                node['bad_since'] = None
                node['good_since'] = None
        self.state.update(last_sample=now, observation='FRESH')
        if previous_snapshot is not None and sample_time <= previous_snapshot:
            return []
        self.state['last_snapshot_epoch'] = sample_time
        ready_bad, ready_good = [], []
        down = self.config['down_seconds']
        recovery = self.config['recovery_seconds']
        for port, node in self.state['nodes'].items():
            if observed[port]:
                node['bad_since'] = None
                if node.get('good_since') is None:
                    node['good_since'] = sample_time
                if node.get('alerted') and sample_time - node['good_since'] >= recovery:
                    ready_good.append(port)
            else:
                node['good_since'] = None
                if node.get('bad_since') is None:
                    node['bad_since'] = sample_time
                if not node.get('alerted') and sample_time - node['bad_since'] >= down:
                    ready_bad.append(port)
        both = self.state['both']
        all_down = not any(observed.values())
        if all_down:
            both['good_since'] = None
            if both.get('bad_since') is None:
                both['bad_since'] = sample_time
        else:
            both['bad_since'] = None
            # A single brief healthy response is not a service recovery.
            stable_up = any(observed[p] and n.get('good_since') is not None
                            and sample_time - n['good_since'] >= recovery
                            for p, n in self.state['nodes'].items())
        labels = self.config['nodes']
        active = routing_text(labels, snapshot, observed)
        events = []
        if all_down and not both.get('alerted') and sample_time - both['bad_since'] >= down:
            both['alerted'] = True
            events.append(self.enqueue('BOTH_DOWN',
                f'【推理服务紧急告警】后端 {"、".join(labels.values())} 均持续不可用至少 {down} 秒。当前推理服务不可用，请检查模型、SSH 隧道和网络。', now))
            for port in ready_bad:
                self.state['nodes'][port]['alerted'] = True
        else:
            for port in ready_bad:
                self.state['nodes'][port]['alerted'] = True
                events.append(self.enqueue('NODE_DOWN',
                    f'【推理服务告警】后端 {labels[port]} 持续不可用至少 {down} 秒。当前承载：{active}。双机冗余已丢失。', now))
        if not all_down and both.get('alerted') and stable_up:
            both['alerted'] = False
            for port in ready_good:
                self.state['nodes'][port]['alerted'] = False
            restored = '、'.join(labels[p] for p in observed if observed[p]
                                and self.state['nodes'][p].get('good_since') is not None
                                and sample_time - self.state['nodes'][p]['good_since'] >= recovery)
            redundant = all(observed[p] and self.state['nodes'][p].get('good_since') is not None
                            and sample_time - self.state['nodes'][p]['good_since'] >= recovery for p in observed)
            note = '双机冗余已恢复。' if redundant else '仍有后端未恢复，冗余尚未恢复。'
            events.append(self.enqueue('SERVICE_RECOVERED',
                f'【推理服务恢复】后端 {restored} 已稳定恢复，推理服务恢复可用。当前承载：{active}。{note}', now))
        elif ready_good:
            for port in ready_good:
                self.state['nodes'][port]['alerted'] = False
            redundant = all(observed[p] and n.get('good_since') is not None
                            and sample_time - n['good_since'] >= recovery
                            for p, n in self.state['nodes'].items())
            note = '双机冗余已恢复。' if redundant else '仍有后端尚未稳定恢复。'
            events.append(self.enqueue('NODE_RECOVERED',
                f'【推理服务恢复】后端 {"、".join(labels[p] for p in ready_good)} 已稳定恢复。当前承载：{active}。{note}', now))
        return events


def status_text(config, snapshot, now):
    labels = config['nodes']
    try:
        fresh = 0 <= now - float(snapshot['epoch']) <= config['freshness_seconds']
        health = {str(row['port']): row['healthy'] for row in snapshot['backends']}
        fresh = fresh and all(type(health.get(p)) is bool for p in labels)
    except (KeyError, TypeError, ValueError):
        fresh, health = False, {}
    instances = '、'.join(f'{label}（' + (
        ('可用' if health[p] else '不可用') if fresh else '状态未知') + '）'
        for p, label in labels.items())
    note = '' if fresh else '\n健康快照缺失或过期，不能确认当前可用性。'
    text = f'{len(labels)} 个 vLLM 实例已配置部署，分别是 {instances}。{note}'
    if fresh and snapshot.get('routing_mode') == 'session_affinity':
        text += '\n当前承载：' + routing_text(labels, snapshot, health) + '；故障迁移后不自动回迁。'
    if config.get('usage'):
        usage = config.get('_usage_snapshot', {})
        for port, label in labels.items():
            text += '\n\n' + label + '\n' + '\n'.join(usage_lines(usage.get(port), now))
        text += ('\n\n入含历史与图片 token；缓已计入入。均=窗口输出/窗口秒数；'
                 '生成=已结束请求的解码 token/解码秒数之和。近3/7天为滚动72/168小时。'
                 '\n按引擎前端观测记账（含中止前已观测输出），秒级归属；异常退出尾部可能缺失。')
    return redact(text)
