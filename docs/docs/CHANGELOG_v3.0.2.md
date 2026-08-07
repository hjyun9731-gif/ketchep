# v3.0.2

- 발송닷컴 공식 API 사양으로 실제 SMS/LMS 연동
- API 키 방식이 아니라 발송닷컴 계정·암호 방식으로 수정
- 운영 URL `https://balsong.com/Linkage/API/` 적용
- 수신자 전체를 한 번의 `Destination` 패키지로 발송
- 90Bytes 기준 SMS/LMS 자동 선택
- 예약발송을 발송닷컴 `Send_Date`로 접수
- 접수번호 `Job_No` 저장 및 중복 접수 방지
- 접수완료와 통신사 전송성공 상태 분리
- `Report_Detail` 결과 동기화
- 예약수정 `Reserve_Edit`, 예약취소 `Cancel` 연동
- 발신번호 목록·잔액을 이용한 연결 확인 버튼 추가
- Railway 변수명을 `BALSONG_*`로 정리하고 기존 `BALSEONG_*`도 호환
- 기본값을 시험모드로 변경하여 실수로 실제 발송되는 상황 방지
