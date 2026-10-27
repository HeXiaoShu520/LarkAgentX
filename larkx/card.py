"""开放平台卡片通道。

逆向网页版协议的 PutMessageRequest 里没有任何卡片字段(card_utils 2070 对个人号恒返 1001),
所以真卡片只能走开放平台 im/v1/messages + msg_type=interactive,由本机已登录的 lark-cli 代发。
跨租户 p2p(如外部联系人)会被飞书以 230038 拒绝,调用方应据此降级回富文本 POST。
"""
import base64
import json
import os
import subprocess
import tempfile

DEFAULT_SIGN = '✦ 临江仙'


def build_reply_card(text, sign=DEFAULT_SIGN):
    """正文 + 尾部灰色小字署名。

    schema 2.0 里 note/plain_text 元素都被服务端拒掉(200861/200621),
    只有 markdown 配 text_size=notation 能把字变小。
    """
    elements = [{'tag': 'markdown', 'content': text or ' '}]
    if sign:
        elements.append({'tag': 'markdown', 'content': f"<font color='grey'>{sign}</font>", 'text_size': 'notation'})
    return {'schema': '2.0', 'config': {'update_multi': True}, 'body': {'elements': elements}}


class CardSendError(RuntimeError):
    """发卡片失败;str(e) 里带飞书错误码,便于上层判断是否降级。"""


def run_lark_cli(script):
    # 必须用 pwsh(7.x) 而不是 powershell(5.1):5.1 把含空格的变量传给 native 程序时
    # 不会补引号,JSON 会被拆成一堆位置参数;7.x 会正确加引号
    # 走 -EncodedCommand:脚本里有引号和空格,直接拼进命令行会被二次拆分
    # 前缀设 OutputEncoding,否则按本地代码页输出,中文正文会乱码
    script = '[Console]::OutputEncoding = [Text.Encoding]::UTF8; ' + script
    encoded = base64.b64encode(script.encode('utf-16-le')).decode()
    return subprocess.run(['pwsh', '-NoProfile', '-EncodedCommand', encoded],
                          capture_output=True, text=True, encoding='utf-8', errors='replace')


_run_lark_cli = run_lark_cli


def _pick_json(*chunks):
    """从输出里捞一个可解析的 JSON 对象。
    lark-cli 失败时把 JSON 写在 stderr,PowerShell 还会在前面加上 "node.exe : " 之类前缀,
    所以不能假设输出是纯 JSON;逐个 '{' 起点向后裁剪试解析,先试最外层整块。
    """
    for chunk in chunks:
        if not chunk:
            continue
        for start in (i for i, ch in enumerate(chunk) if ch == '{'):
            for end in range(len(chunk) - 1, start, -1):
                if chunk[end] != '}':
                    continue
                try:
                    return json.loads(chunk[start:end + 1])
                except ValueError:
                    continue
    return None


def send_card(chat_id, text, sign=DEFAULT_SIGN):
    """以用户身份给 chat_id(oc_xxx) 发卡片,返回 message_id。"""
    fd, path = tempfile.mkstemp(suffix='.json', prefix='larkx_card_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(build_reply_card(text, sign), f, ensure_ascii=False)
        # 2>&1 让 lark-cli 的失败 JSON 也进管道:它出错时把 {"ok":false,...} 写 stderr,
        # 只收 stdout 会看不到错误码
        script = (
            f'$c = Get-Content -Raw -Encoding UTF8 "{path}"; '
            f'lark-cli im +messages-send --as user --chat-id {chat_id} '
            f'--msg-type interactive --content $c 2>&1 | Out-String'
        )
        proc = _run_lark_cli(script)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    out = (proc.stdout or '') + (proc.stderr or '')
    payload = _pick_json(proc.stdout or '', proc.stderr or '')
    if payload is None:
        raise CardSendError(f'lark-cli 无可用输出(exit={proc.returncode}): {out.strip()[:200]}')
    if not payload.get('ok'):
        err = payload.get('error') or {}
        raise CardSendError(f"{err.get('code')} {err.get('message') or err.get('subtype') or ''}".strip())
    return (payload.get('data') or {}).get('message_id') or ''
