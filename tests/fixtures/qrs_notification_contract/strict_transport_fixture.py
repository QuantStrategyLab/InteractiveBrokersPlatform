import re
import urllib.parse
import urllib.request

def can_combine_reminder(*, text, chat_id):
    # Plain text, conservative UTF-16 limit. Preserve the original digest intact.
    return isinstance(text, str) and len(text.encode('utf-16-le')) // 2 <= 4096 and isinstance(chat_id, str) and bool(re.fullmatch(r'-?[1-9][0-9]*|@[A-Za-z0-9_]{5,32}', chat_id))


def send_strict_telegram(*, token, chat_id, text):
    """Strict receipt for the combined digest; no retries, redirects or raw errors."""
    try:
        request = urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage',
            data=urllib.parse.urlencode({'chat_id': chat_id, 'text': text, 'disable_web_page_preview': 'true'}).encode(),
            method='POST', headers={'Content-Type': 'application/x-www-form-urlencoded'})
        value = _json(request)
        result = value.get('result')
        if value.get('ok') is not True or not isinstance(result, dict): return False
        message_id = result.get('message_id'); chat = result.get('chat')
        if type(message_id) is not int or message_id <= 0 or not isinstance(chat, dict) or type(chat.get('id')) is not int: return False
        if chat_id.startswith('@'):
            return isinstance(chat.get('username'), str) and ('@' + chat['username']).casefold() == chat_id.casefold()
        return str(chat['id']) == str(chat_id)
    except Exception: return False
