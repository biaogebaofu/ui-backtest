"""Chinese reading labels only; never normalize or overwrite machine settings."""
import json
from pathlib import Path


DISPLAY_LABELS = json.loads(Path(__file__).with_suffix('.json').read_text('utf-8'))


def mode_label(value, kind):
    if value is None or value == '':
        return '未记录（查原结果）'
    return DISPLAY_LABELS[kind].get(str(value), f'未知模式：{value}（查原记录）')


def candidate_display_payload(payload):
    """Create an Excel-only projection; original dictionaries/JSON stay untouched."""
    source_headers = payload['表头']
    headers = [h for h in source_headers if h != '成本模式']
    front = headers.index('策略指纹') + 1 if '策略指纹' in headers else 0
    headers[front:front] = ['入场次数规则', '成本模式']
    if '成本模式' in source_headers:
        headers.append('成本模式代码')
    result = dict(payload, 表头=headers)
    for key in ('研究候选', '观察候选', '实盘候选'):
        result[key] = [dict(row, **{'入场次数规则': mode_label(row.get('入场触发口径'), 'entry'),
                      '成本模式': mode_label(row.get('成本模式'), 'cost'),
                      '成本模式代码': row.get('成本模式')}) for row in payload.get(key, [])]
    return result
