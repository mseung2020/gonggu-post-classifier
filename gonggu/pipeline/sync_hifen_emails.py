#!/usr/bin/env python3
"""데일리 파이프라인(resolve_links가 링크인바이오 허브를 파싱하는 김에 곁다리로 모아둔
인스타그램 계정 이메일, gonggu/common.HIFEN_EMAIL_FILE)을 hifen DB의
instagram_user.email 컬럼에 반영한다.

⚠ hifen(SRC)은 지금까지 이 저장소 전체에서 읽기 전용으로만 써왔다(common.connect_src
docstring 참고) — 이 명령만 예외적으로 UPDATE를 하고, 대상도 email 컬럼 하나뿐이다.
dev_gongguking에는 이메일 컬럼이 없고 앞으로도 안 만든다 — 그쪽 테이블/파일은 이 명령이
전혀 건드리지 않는다.

여러 이메일이 발견된 계정은 ','로 이어붙여 하나의 문자열로 저장한다(과거 진단 스크립트
_extract_inpock_emails.py의 ';' 구분자에서 변경, 2026-08-11 — 앞으로는 이 구분자로 통일).

체크포인트 파일 없이 매번 HIFEN_EMAIL_FILE 전체를 hifen과 다시 비교한다 — UPDATE 문이
기존 값과 똑같은 값을 넣으면 MySQL은 rowcount를 0으로 돌려주므로(실제로 바뀐 행만 셈),
그 rowcount를 그대로 "이번에 새로 반영된 것"의 기준으로 쓴다. 그래서 몇 번을 다시 돌려도
안전하고(idempotent), 매번 정확히 실제 변화량만 보여준다.

사용법(저장소 루트에서): python3 -m gonggu.sync_hifen_emails
"""
from gonggu.common import HIFEN_EMAIL_FILE, acquire_lock, connect_src, load_jsonl


def main():
    acquire_lock('sync_hifen_emails')
    records = load_jsonl(HIFEN_EMAIL_FILE)  # user_id -> {..., emails: [...]} , key당 마지막 줄이 최신
    targets = [(user_id, rec['emails']) for user_id, rec in records.items() if rec.get('emails')]
    print(f'이메일이 발견된 인스타그램 계정 {len(targets)}개 확인 → hifen.instagram_user 반영 시도')

    if not targets:
        return

    conn = connect_src()
    updated_users = updated_emails = 0
    try:
        with conn.cursor() as cur:
            for user_id, emails in targets:
                cur.execute('UPDATE instagram_user SET email = %s WHERE user_id = %s',
                            (','.join(emails), user_id))
                if cur.rowcount:
                    updated_users += 1
                    updated_emails += len(emails)
        conn.commit()
    finally:
        conn.close()

    print(f'완료 — 새로 반영된 계정 {updated_users}개(이메일 총 {updated_emails}개). '
          f'(이미 hifen에 같은 값이 있던 계정은 rowcount=0이라 세지 않음)')


if __name__ == '__main__':
    main()
