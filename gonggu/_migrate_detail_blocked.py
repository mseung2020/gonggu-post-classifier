#!/usr/bin/env python3
"""일회성 스키마 변경: detail_status ENUM에 'blocked' 값 추가(2026-08-12).

enrich_detail을 fast/uc 두 패스로 가르면서(자세한 배경은 queries/add_detail_blocked_status.sql
상단 주석) fast 패스가 안티봇에 막힌 상품을 남길 새 상태 'blocked'가 필요하다. detail_status는
ENUM이라 값을 넓히려면 ALTER가 필요하다 — 이 스크립트가 post/video 양쪽 테이블에 그 ALTER를
적용한다.

기존 데이터/값은 그대로 두고 허용값만 넓히는 변경이라 안전하다. 이미 'blocked'가 있는 ENUM에
같은 ALTER를 다시 걸어도 결과가 같아(idempotent) 여러 번 실행해도 무방하다.

사용법(저장소 루트에서):
    python3 -m gonggu._migrate_detail_blocked
"""
from gonggu.common import connect_dst

TABLES = ('gonggu_post_product_detail', 'gonggu_video_product_detail')
_ENUM = "ENUM('pending', 'done', 'error', 'gone', 'blocked') NOT NULL DEFAULT 'pending'"
_COMMENT = ("처리 상태. error=일시 실패(fast 재시도), gone=페이지 영구 소멸(재시도 안 함), "
            "blocked=fast(무인) 경로 차단 → uc 패스가 처리")


def main():
    conn = connect_dst()
    try:
        for t in TABLES:
            sql = (f"ALTER TABLE {t} MODIFY COLUMN detail_status {_ENUM} "
                   f"COMMENT '{_COMMENT}'")
            with conn.cursor() as cur:
                cur.execute(sql)
            print(f"  ✓ {t}.detail_status 에 'blocked' 추가")
        conn.commit()
        print("완료 — 이제 fast/uc 2단 백필을 돌릴 수 있습니다.")
    finally:
        conn.close()


if __name__ == '__main__':
    main()
