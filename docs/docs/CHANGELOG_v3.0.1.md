# v3.0.1 런타임 오류 수정

- `/messages/`의 `Invalid filter: 'status_class'` 오류 수정
- `message_list.html`에서 `core_extras` 템플릿 태그 라이브러리 명시적 로드
- 배포 시작 단계에 `python manage.py validate_templates` 추가
- 모든 HTML 템플릿을 실제 Django 엔진으로 컴파일하는 회귀 테스트 추가
- 템플릿 오류가 있으면 Gunicorn 실행 전에 배포가 실패하도록 변경
