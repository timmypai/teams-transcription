"""Versioned offline criterion for fixed synthetic facts, not a general semantic judge."""
import re
import unicodedata
from fractions import Fraction

CRITERION_VERSION = 'synthetic_current_fact_semantic_v1'
_NUMERAL = r'(?:[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?[萬千百]?|[零一二兩三四五六七八九十百千萬億]+)'
_MONEY = re.compile(_NUMERAL + '元')
_BUDGET = re.compile(r'(?:會議)?預算(?:總額)?(?:是|為|有|共)?(新台幣)?(' + _NUMERAL + ')元')
_TIME = re.compile(r'(?:會議|開會)(?:時間)?(?:是|為|在)?([今明後天週星期一二三四五六日上下午夜早晚零七八九十百點分0-9年月:/-]+)')
_PREFIX = re.compile(r'(?:好的?|收到|了解|答案(?:是|如下)|'
    r'(?:根據|依據)(?:你|您)?(?:目前|最新)?(?:提供的)?(?:會議)?(?:紀錄|記錄|資料|原文)|'
    r'我(?:先)?(?:只|僅)?(?:逐字)?(?:摘錄|引用)(?:你|您)?(?:目前|最新)?(?:提供的)?(?:會議)?(?:原文|紀錄|資料)|'
    r'不(?:加|加入)(?:其他)?(?:說明|解釋))[,，:：;；。]*')


def _amount(text):
    if len(text) > 64:
        return None
    if text[0].isdigit():
        multiplier = {'萬':10000,'千':1000,'百':100}.get(text[-1],1)
        return Fraction((text[:-1] if multiplier != 1 else text).replace(',','')) * multiplier
    digits = dict(zip('零一二兩三四五六七八九', (0,1,2,2,3,4,5,6,7,8,9)))
    units = {'十':10,'百':100,'千':1000,'萬':10000,'億':100000000}
    total = section = digit = 0
    for char in text:
        if char in digits:
            digit = digits[char]
        elif units[char] < 10000:
            section += (digit or 1) * units[char]; digit = 0
        else:
            total += (section + digit) * units[char]; section = digit = 0
    value = total + section + digit
    def canonical(number):
        if number == 0:
            return '零'
        if number >= 10000:
            high,low = divmod(number,10000)
            return canonical(high)+'萬'+(('零' if low<1000 else '')+canonical(low) if low else '')
        out,zero = '',False
        for unit,label in ((1000,'千'),(100,'百'),(10,'十'),(1,'')):
            count,number = divmod(number,unit)
            if count:
                if zero:out += '零'
                out += ('零一二三四五六七八九'[count] if count!=1 or unit!=10 or out else '')+label
                zero = False
            elif out and number:
                zero = True
        return out
    # Ambiguous/malformed numeral strings never become an inferred correct value.
    if value >= 100000000 or text.replace('兩','二') != canonical(value):
        return None
    return Fraction(value)


def _normalize(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))


def _fact(text):
    text = text.strip('「」『』“”"。.!！')
    match = _BUDGET.fullmatch(text)
    if match:
        amount = _amount(match[2])
        return ('budget', amount, match[1]) if amount is not None else None
    match = _TIME.fullmatch(text)
    if match:
        return ('time', match[1].replace('星期','週'))
    return None


def assess_content(utterances, source, *, expected_version, delivered_version):
    """No content leaves this function; unsupported language/forms remain unknown.

    This recognizes a deliberately small grammar of complete current budget/time
    facts with nonfactual lead-ins. It never treats an expected substring as proof.
    Versions are local source delivery metadata, not a model consumption ACK.
    """
    result = {'criterion_version':CRITERION_VERSION, 'status':'unknown',
        'reason':'unsupported_answer_form', 'fact_type':None,
        'model_consumption_verified':False}
    if (type(expected_version) is not int or type(delivered_version) is not int
            or expected_version < 1 or expected_version != delivered_version):
        return {**result,'status':'fail','reason':'source_version_mismatch'}
    if (not isinstance(source,str) or len(source)>2048 or not isinstance(utterances,list)
            or not 1<=len(utterances)<=8 or any(not isinstance(s,str) for s in utterances)
            or sum(map(len,utterances))>4096):
        return {**result,'reason':'unsupported_evidence'}
    normalized_source = _normalize(source)
    text = _normalize(''.join(utterances))
    if len(normalized_source) > 2048 or len(text) > 4096:
        return {**result,'reason':'unsupported_evidence'}
    expected = _fact(normalized_source)
    if expected is None:
        return {**result,'reason':'unsupported_source_form'}
    result['fact_type'] = expected[0]
    if len(_MONEY.findall(text)) > 1:
        return {**result,'status':'fail','reason':'multiple_fact_values'}
    for _ in range(8):
        match = _PREFIX.match(text)
        if match is None:
            break
        text = text[match.end():]
    observed = _fact(text)
    if observed is None:
        return result
    if observed != expected:
        return {**result,'status':'fail','reason':'wrong_fact_value'}
    return {**result,'status':'pass','reason':'complete_current_fact_equivalent'}
