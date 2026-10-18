"""Classify observed pages without treating CDN headers as proof of a challenge."""
import json
import re
from pathlib import Path
from urllib.parse import urlparse

LABELS = {
    'cloudflare_challenge': 'Cloudflare 的整页安全检查（已确认）',
    'cloudflare_likely': '疑似 Cloudflare 安全检查（尚未确认）',
    'cloudflare_widget': '网站页面内的 Cloudflare 验证组件',
    'login': 'ActiveSG / Singpass 登录页面',
    'unknown_security': '安全检查页面，来源尚未确认',
    'access_denied': '访问被拒绝，拦截来源尚未确认',
    'blank': '页面为空白，不能判定为安全验证',
    'page_loaded': '页面已有内容，未发现明确安全检查标记',
    'unavailable': '未取得页面证据',
}


def safe_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return 'about:blank' if url == 'about:blank' else '未取得网址'
    # Strip credentials, query parameters and fragments, including login codes.
    return (parsed.scheme + '://' + parsed.hostname + parsed.path)[:500]


def classify(url, title, text, document=None, markers=None):
    document, markers = document or {}, markers or {}
    headers = {k.lower(): str(v)[:100] for k, v in document.get('headers', {}).items()
               if k.lower() in ('cf-mitigated', 'cf-ray', 'server')}
    status = document.get('status')
    evidence = []
    security = bool(re.search(
        r'performing security verification|verify (?:you are|that you)|enable javascript and cookies|checking your browser',
        text, re.I) or re.search(r'just a moment|security verification', title, re.I))
    host = urlparse(url).hostname or ''
    path = urlparse(url).path
    if headers.get('cf-mitigated', '').lower() == 'challenge':
        category = 'cloudflare_challenge'
        evidence.append('主页面响应包含 cf-mitigated: challenge')
    elif security and markers.get('challenge_options'):
        category = 'cloudflare_challenge'
        evidence.append('检查页面包含 Cloudflare _cf_chl_opt 标记')
    elif host == 'singpass.gov.sg' or host.endswith('.singpass.gov.sg') or re.search(r'/(?:login|sign-in)(?:/|$)', path, re.I):
        category = 'login'
        evidence.append('实际跳转到登录网址')
    elif security and markers.get('widget'):
        category = 'cloudflare_widget'
        evidence.append('检查页面包含 challenges.cloudflare.com 验证框')
    elif security and (markers.get('challenge_script') or re.search(r'cloudflare', title + '\n' + text, re.I)):
        category = 'cloudflare_likely'
        evidence.append('检查文字与 Cloudflare 页面标记同时出现，但未取得确认标记')
    elif security:
        category = 'unknown_security'
        evidence.append('发现检查文字，未发现能确认提供方的标记')
    elif status in (401, 403, 429):
        category = 'access_denied'
        evidence.append('主页面响应 HTTP ' + str(status))
    elif not text.strip():
        category = 'blank'
        evidence.append('当前页面没有可见正文')
    else:
        category = 'page_loaded'
        evidence.append('没有明确的整页安全检查或登录标记')
    # CF-Ray and Server: cloudflare alone indicate CDN delivery, not a challenge.
    return {'category': category, 'label': LABELS[category], 'url': safe_url(url),
            'title': title[:160], 'http_status': status, 'headers': headers, 'evidence': evidence}


def load_failure(directory):
    try:
        value = json.loads((Path(directory) / 'last-query-failure.json').read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def format_failure(value, include_url=False):
    if not value:
        return '尚未保存查询失败证据。部署此版本后，请先进行一次查询。'
    lines = ['最近一次失败：' + value.get('captured_at', '未知时间'),
             '判断：' + value.get('label', LABELS['unavailable']),
             '页面标题：' + (value.get('title') or '空白 / 未取得'),
             '主页面 HTTP：' + str(value.get('http_status') or '未取得')]
    lines.extend('证据：' + item for item in value.get('evidence', []))
    if value.get('headers', {}).get('cf-ray'):
        lines.append('Cloudflare 请求编号：' + value['headers']['cf-ray'])
    step = value.get('step')
    if isinstance(step, dict):
        lines.append('操作阶段：' + str(step.get('stage', '未知'))[:100])
        lines.append('等待步骤：' + str(step.get('operation', '未知'))[:100])
        if step.get('target'):
            lines.append('操作目标：' + str(step['target'])[:200])
        if step.get('date'):
            lines.append('查询日期：' + str(step['date'])[:20])
        lines.append('该阶段已耗时：' + str(step.get('elapsed_seconds', '未知')) + ' 秒')
        if step.get('timeout_seconds') is not None:
            lines.append('此步骤等待上限：' + str(step['timeout_seconds']) + ' 秒')
        if 'search_scroll_steps' in step:
            lines.append('滚动查找：' + str(step['search_scroll_steps']) + ' 次；累计 ' +
                         str(step.get('search_scroll_distance', 0)) + ' 像素')
            confirmed = step.get('search_bottom_confirmed', step.get('search_at_bottom', False))
            lines.append('列表是否已滚动到底：' + ('是（已等待底部稳定）' if confirmed else '尚未确认'))
        if step.get('activity_group'):
            lines.append('运动项目分组：' + str(step['activity_group'])[:100])
            lines.append('分组展开：' + ('已完成' if step.get('activity_group_expanded') else '尚未确认'))
        if step.get('activity_options'):
            lines.append('分组内选项：' + '、'.join(str(name)[:100] for name in step['activity_options'][:12]))
        if step.get('activity_selected'):
            lines.append('选择的子选项：' + str(step['activity_selected'])[:100])
        if 'target_scrolled' in step:
            lines.append('滚动到目标按钮：' + ('已完成' if step['target_scrolled'] else '尚未完成'))
        if 'target_in_view' in step:
            lines.append('目标按钮是否进入画面：' + ('是' if step['target_in_view'] else '否'))
        if 'clicked' in step:
            lines.append('按钮点击：' + ('已完成' if step['clicked'] else '尚未完成'))
        if step.get('timeout_reason'):
            lines.append('超时线索：' + str(step['timeout_reason'])[:100])
        if 'browser_seconds_remaining' in step:
            lines.append('远程浏览器剩余时间：' + str(step['browser_seconds_remaining']) + ' 秒')
        page = step.get('page', {})
        if page:
            lines.append('页面控件：日期按钮 ' + str(page.get('date_buttons', 0)) +
                         '；时段控件 ' + str(page.get('slot_controls', 0)) +
                         '；可见弹窗 ' + str(page.get('dialogs', 0)))
            if page.get('headings'):
                lines.append('页面标题文字：' + '、'.join(str(n)[:80] for n in page['headings'][:6]))
    lookup = value.get('lookup')
    if isinstance(lookup, dict):
        label = '场馆' if lookup.get('kind') == 'venue' else '运动项目'
        lines.append('查找阶段：' + label + '选择')
        lines.append('目标名称：' + str(lookup.get('requested', ''))[:200])
        candidates = lookup.get('candidate_names', [])
        lines.append('页面读取到的名称：' + ('、'.join(str(n)[:100] for n in candidates[:8]) if candidates else '没有读取到活动或场馆链接，可能仍未加载或官网结构改变'))
    slots = value.get('slots')
    if isinstance(slots, dict):
        lines.append('读取阶段：时段分组；日期：' + str(slots.get('date', '未知日期'))[:20])
        labels = slots.get('labels', [])
        headings = slots.get('headings', [])
        lines.append('读到的分组文字：' + ('、'.join(str(n)[:80] for n in labels[:12]) if labels else '未识别到即时预约或抽签标题'))
        lines.append('读到的页面标题文字：' + ('、'.join(str(n)[:80] for n in headings[:12]) if headings else '没有标题标签'))
        lines.append('可见时段控件：' + str(slots.get('visible', 0)) +
                     '；即时预约：' + str(slots.get('instant', 0)) +
                     '；抽签：' + str(slots.get('ballot', 0)) +
                     '；分组未确认：' + str(slots.get('unknown', 0)))
        if slots.get('loading'):
            lines.append('页面仍有可见的 Loading 提示')
        if slots.get('stale_instant'):
            lines.append('日期未一致或标识无法读取的即时预约控件：' + str(slots['stale_instant']))
    if include_url:
        lines.append('页面来源：' + value.get('url', '未取得'))
    lines.append('说明：' + value.get('error', ''))
    lines.append('这是失败时保存的证据；本指令不会重新访问官网。')
    return '\n'.join(lines)
