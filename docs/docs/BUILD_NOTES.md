# 빌드 메모

- 소스 버전: 1.0.0-mvp
- 개발 스택: Django 5.2 LTS, PostgreSQL, Gunicorn, WhiteNoise
- 배포 대상: Railway
- 기본 계정: admin / admin1234
- Python 구문 검사, URL-뷰 연결 검사, 템플릿 URL 이름 검사, 셸 스크립트 구문 검사 완료
- 현재 제작 환경에는 Django 패키지를 설치할 수 없어 실제 서버 기동과 Django TestCase 실행은 배포 환경에서 수행해야 함
- 첫 Railway 배포 후 `docs/ACCEPTANCE_TESTS.md` 순서대로 확인 권장
