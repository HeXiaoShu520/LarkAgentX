"""数字 chat_id -> 开放平台 oc_xxx 的映射。

发卡片只认开放平台会话 id(oc_xxx),而逆向协议收到的 chat_id 是 64 位数字,
两边没有任何字段能直接对上(proto 里没有 open_chat_id)。
这里用会话名做桥:

1. 先按名字在 lark-cli 的会话列表里找候选;
2. 名字唯一就直接绑定;
3. 重名(同一个人可能有几个同名群)时,拿刚收到的消息正文去候选会话的最近消息里核对,
   核对命中才绑定;
4. 绑不上返回空串,由调用方降级成富文本发送。

绑定结果落本地 json,数字 chat_id 本身不变,所以绑定一次可以长期复用。
"""
import json
import os
import time

from .card import _pick_json, run_lark_cli

CACHE_FILE = '.larkx_cardmap.json'
LIST_TTL = 600
NEEDLE_LEN = 20


def _read(path):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, data):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _fetch_chats():
    """拉全量会话列表;lark-cli 不可用或报错时返回 None。
    --types 的值必须用单引号包住:pwsh 会把逗号当数组分隔符,裸写会被拆成 "p2p group" 而报错。
    """
    proc = run_lark_cli("lark-cli im +chat-list --as user --types 'p2p,group' --page-all --page-size 100 2>&1 | Out-String")
    payload = _pick_json(proc.stdout or '', proc.stderr or '')
    if not payload or not payload.get('ok'):
        return None
    chats = (payload.get('data') or {}).get('chats') or []
    items = []
    for c in chats:
        cid = c.get('chat_id') or ''
        if cid:
            items.append({'chat_id': cid, 'name': (c.get('name') or '').strip(), 'mode': c.get('chat_mode') or '', 'external': bool(c.get('external'))})
    return items


def _chat_list(cache):
    """会话列表带 TTL 的缓存:群名不常变,不该每条消息都去拉一遍。"""
    cached = cache.get('_chats') or {}
    if time.time() - (cached.get('ts') or 0) < LIST_TTL and cached.get('items'):
        return cached['items']
    items = _fetch_chats()
    if items is None:
        return cached.get('items') or []
    cache['_chats'] = {'ts': time.time(), 'items': items}
    return items


def _needle(text):
    """从消息正文里取一段够独特的前缀用来核对会话;太短容易误命中。"""
    kept = ''.join(ch for ch in (text or '') if ch.isalnum() or '\u4e00' <= ch <= '\u9fff')
    return kept[:NEEDLE_LEN] if len(kept) >= 6 else ''


def _chat_has_text(oc_chat_id, needle):
    proc = run_lark_cli(f'lark-cli im +chat-messages-list --as user --chat-id {oc_chat_id} --page-size 20 --order desc 2>&1 | Out-String')
    text = proc.stdout or ''
    # lark-cli 输出是 JSON,中文可能被转成 \uXXXX,两种写法都要试
    return needle in text or json.dumps(needle)[1:-1] in text


def resolve_oc(chat_id, chat_name, sample='', root='.'):
    """把逆向协议的数字 chat_id 换成 oc_xxx;换不出来返回空串。"""
    chat_id = str(chat_id or '')
    if not chat_id:
        return ''
    path = os.path.join(root, CACHE_FILE)
    cache = _read(path)
    known = (cache.get('map') or {}).get(chat_id)
    if known:
        return known
    name = (chat_name or '').strip()
    if not name:
        return ''
    # 跨租户会话(external)直接排除:开放平台对它一律 230027/230038,
    # 先试一次卡片只是白等几秒,不如直接走降级
    cands = [c for c in _chat_list(cache) if c['name'] == name and not c.get('external')]
    if len(cands) > 1:
        needle = _needle(sample)
        cands = [c for c in cands if needle and _chat_has_text(c['chat_id'], needle)]
    if len(cands) == 1:
        cache.setdefault('map', {})[chat_id] = cands[0]['chat_id']
    _write(path, cache)
    return cands[0]['chat_id'] if len(cands) == 1 else ''


def forget(chat_id, root='.'):
    """丢掉一条绑定,下次重新解析。"""
    path = os.path.join(root, CACHE_FILE)
    cache = _read(path)
    if (cache.get('map') or {}).pop(str(chat_id), None) is not None:
        _write(path, cache)
