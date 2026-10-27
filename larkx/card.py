"""开放平台卡片与媒体通道。

逆向网页版协议的 PutMessageRequest 里没有任何卡片字段(card_utils 2070 对个人号恒返 1001),
图片也只能用逆向侧自己的 key,所以真卡片和图片都走开放平台 im/v1/messages,由本机已登录的
lark-cli 代发。跨租户 p2p(如外部联系人)会被飞书以 230038 拒绝,调用方应据此降级回富文本 POST。
"""
import base64
import json
import os
import subprocess
import tempfile

from loguru import logger

DEFAULT_SIGN = '✦ 临江仙'


class CardSendError(RuntimeError):
    """发卡片/图片失败;str(e) 里带飞书错误码,便于上层判断是否降级。"""


def build_reply_card(text, sign=DEFAULT_SIGN, image=''):
    """正文 + 可选内嵌图片 + 尾部灰色小字署名。

    schema 2.0 里 note/plain_text 元素都被服务端拒掉(200861/200621),
    只有 markdown 配 text_size=notation 能把字变小。
    image 传 image_key(img_xxx) 或可访问的图片 URL;本地文件先过 upload_image。
    """
    content = text or ''
    if image:
        # 图并进正文同一个 markdown 元素,否则元素间默认间距会在图和字之间多空一段
        content = f'{content}\n\n![image]({image})' if content else f'![image]({image})'
    elements = [{'tag': 'markdown', 'content': content or ' '}]
    if sign:
        elements.append({'tag': 'markdown', 'content': f"<font color='grey'>{sign}</font>", 'text_size': 'notation'})
    return {'schema': '2.0', 'config': {'update_multi': True}, 'body': {'elements': elements}}


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


def _payload(proc):
    """统一解析 lark-cli 的返回;失败按飞书错误码抛 CardSendError。"""
    out = (proc.stdout or '') + (proc.stderr or '')
    payload = _pick_json(proc.stdout or '', proc.stderr or '')
    if payload is None:
        raise CardSendError(f'lark-cli 无可用输出(exit={proc.returncode}): {out.strip()[:200]}')
    if not payload.get('ok'):
        err = payload.get('error') or {}
        raise CardSendError(f"{err.get('code')} {err.get('message') or err.get('subtype') or ''}".strip())
    return payload


def _locate(media):
    """把媒体输入拆成 (工作目录, 交给 lark-cli 的值)。

    lark-cli 只收 cwd 相对路径,绝对路径和带 .. 的一律拒绝,所以本地文件不传路径,
    改成执行前 Set-Location 到它所在目录、只传文件名。img_xxx 和 URL 原样透传。
    """
    if media.startswith('img_'):
        return '', media
    if media.startswith(('http://', 'https://')):
        return '', media
    path = os.path.abspath(media)
    if not os.path.isfile(path):
        raise CardSendError(f'本地文件不存在: {media}')
    return os.path.dirname(path), os.path.basename(path)


def _send(chat_id, flags, workdir='', prelude=''):
    """跑一次 im +messages-send。flags 是 --image/--msg-type + --content 之类的后半段。"""
    cd = f'Set-Location -LiteralPath "{workdir}"; ' if workdir else ''
    # 2>&1 让 lark-cli 的失败 JSON 也进管道:它出错时把 {"ok":false,...} 写 stderr,
    # 只收 stdout 会看不到错误码
    script = f'{prelude}{cd}lark-cli im +messages-send --as user --chat-id {chat_id} {flags} 2>&1 | Out-String'
    return _payload(_run_lark_cli(script))


def upload_image(media):
    """把本地图片/动图传到开放平台换 image_key;已是 img_xxx 则原样返回。

    卡片 markdown 只认 image_key,直接把本地路径塞进 ![x](路径) 不会被上传。
    """
    if media.startswith('img_'):
        return media
    workdir, name = _locate(media)
    cd = f'Set-Location -LiteralPath "{workdir}"; ' if workdir else ''
    # --data 里是 JSON,用单引号包住最省事:pwsh 不对单引号内容做任何展开
    script = (f"{cd}lark-cli im images create --as user --data '{{\"image_type\":\"message\"}}' "
              f"--file '{name}' 2>&1 | Out-String")
    payload = _payload(_run_lark_cli(script))
    key = (payload.get('data') or {}).get('image_key') or ''
    if not key:
        raise CardSendError('上传图片未返回 image_key')
    return key


def send_image(chat_id, media):
    """发一条纯图片/GIF 消息(不带任何文字),返回 message_id。

    media 支持本地路径(相对或绝对)、http(s) URL、已上传的 img_xxx。
    动图不会被压成静态图,服务端按原文件存,客户端按动图播放。
    """
    workdir, name = _locate(media)
    payload = _send(chat_id, f'--image "{name}"', workdir)
    return (payload.get('data') or {}).get('message_id') or ''


def send_card(chat_id, text, sign=DEFAULT_SIGN, image=''):
    """以用户身份给 chat_id(oc_xxx) 发卡片,image 非空时内嵌一张图片/动图,返回 message_id。"""
    key = ''
    if image:
        try:
            key = upload_image(image)
        except CardSendError as e:
            # 图跟不上不该连正文一起丢:退成纯文字卡片,但把原因留在日志里
            logger.warning(f'图片上传失败,卡片改为不带图: {e}')
    fd, path = tempfile.mkstemp(suffix='.json', prefix='larkx_card_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(build_reply_card(text, sign, key), f, ensure_ascii=False)
        prelude = f'$c = Get-Content -Raw -Encoding UTF8 "{path}"; '
        payload = _send(chat_id, '--msg-type interactive --content $c', '', prelude)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return (payload.get('data') or {}).get('message_id') or ''
