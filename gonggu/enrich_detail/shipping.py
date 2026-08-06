"""배송비 원문 파서 — gonggu_scraper의 검증된 _parse_shipping 원리를 카피.

Cafe24/고도몰류 '기본 정보' 테이블의 배송비 칸은 "3,000원 (100,000원 이상 무료)",
"1원 이상 ~ 50,000원 미만 3,000원 / 50,000원 이상 0원"(구간표), "4만원 이상 무료배송"처럼
형태가 다양하다. 반환: (요약문, 기본배송비(원), 무료임계액(원)) — 모르면 None.

원본 코드의 정규식 버그 교훈도 그대로 계승: 구간표의 '미만'은 "(?:미만)?"으로 묶어야
단어 전체가 optional이 된다("미만?"은 '만'만 optional이라 마지막 구간을 못 잡았었다).
"""
import re

_INT = re.compile(r'[\d,]+')

_SHIP_TIER = re.compile(
    r'([\d,]+)\s*원\s*이상\s*~?\s*([\d,]+)?\s*원?\s*(?:미만)?\s*([\d,]+)\s*원')


def _int(s):
    digits = re.sub(r'[^\d]', '', s or '')
    return int(digits) if digits else None


def krw_amount(text):
    """'4만원'/'3만5천원'/'30,000원' 등을 정수(원)로. 못 읽으면 None."""
    m = re.search(r'(\d[\d,]*)\s*만\s*(\d[\d,]*)?\s*천?\s*원', text)
    if m:
        man = int(m.group(1).replace(',', ''))
        cheon = int(m.group(2).replace(',', '')) if m.group(2) else 0
        return man * 10000 + cheon * 1000
    m = re.search(r'(\d[\d,]*)\s*천\s*원', text)
    if m:
        return int(m.group(1).replace(',', '')) * 1000
    m = re.search(r'([\d,]+)\s*원', text)
    return int(m.group(1).replace(',', '')) if m else None


def parse_shipping(text):
    """배송비 원문 → (요약문, 기본배송비, 무료임계액)."""
    if not text:
        return None, None, None

    tiers = [(_int(lo), _int(hi), _int(fee)) for lo, hi, fee in _SHIP_TIER.findall(text)]
    if tiers:
        base = next((fee for _, _, fee in tiers if fee), 0)
        free_over = next((lo for lo, _, fee in tiers if fee == 0 and lo), None)
        if base == 0:
            return '무료배송', 0, None
        if free_over:
            return f'{base:,}원 ({free_over:,}원 이상 무료)', base, free_over
        return f'{base:,}원', base, None

    fm = re.search(r'([\d,]+(?:\s*만\s*(?:[\d,]+\s*천)?|\s*천)?\s*원)\s*이상[^0-9]*무료', text)
    free_over = krw_amount(fm.group(1)) if fm else None

    bm = re.search(r'기본\s*배송비\s*[:：]?\s*([\d,]+(?:\s*만\s*(?:[\d,]+\s*천)?|\s*천)?\s*원)', text)
    if bm:
        fee = krw_amount(bm.group(1))
        if fee == 0:
            return '무료배송', 0, None
        if free_over:
            return f'{fee:,}원 ({free_over:,}원 이상 무료)', fee, free_over
        return f'{fee:,}원', fee, None

    if '무료' in text and not re.search(r'\d', text.split('무료')[0]):
        return '무료배송', 0, None

    m = _INT.search(text)
    fee = _int(m.group(0)) if m else None
    if fee is None:
        return (text.strip() or None), None, free_over
    if fee == 0:
        return '무료배송', 0, None
    if free_over:
        return f'{fee:,}원 ({free_over:,}원 이상 무료)', fee, free_over
    return f'{fee:,}원', fee, None
