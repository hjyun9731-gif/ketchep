# 발송닷컴 API 연결

## 1. Railway 환경변수

Railway 프로젝트의 `Variables`에 다음 값을 등록합니다.

```text
BALSONG_API_URL=https://balsong.com/Linkage/API/
BALSONG_USER_ID=발송닷컴 아이디
BALSONG_USER_PW=발송닷컴 비밀번호
BALSONG_CALLBACK=발송닷컴에 등록된 발신번호
BALSONG_DRY_RUN=1
```

아이디와 비밀번호는 GitHub 코드나 `.env` 파일에 커밋하지 않습니다.

## 2. 연결 확인

배포 후 프로그램의 `문자` 화면에서 `발송닷컴 연결 확인`을 누릅니다.

정상일 때 다음을 확인합니다.

- 아이디·비밀번호 인증 성공
- 등록된 발신번호 목록
- Railway에 입력한 발신번호의 등록 여부
- 발송닷컴 선불잔액

## 3. 시험발송

처음에는 반드시 `BALSONG_DRY_RUN=1` 상태를 유지합니다. 이 상태에서는 프로그램 내부 처리만 하고 실제 문자 API 발송은 하지 않습니다.

화면과 대상자·문구를 확인한 뒤 본인 전화번호 한 명만 대상으로 시험할 준비가 끝났을 때 Railway 값을 다음처럼 바꿉니다.

```text
BALSONG_DRY_RUN=0
```

그 뒤 본인 번호 1건, 내부 담당자 2~3건 순서로 시험하고 전체발송을 진행합니다.

## 4. 프로그램 동작

- 90Bytes 이하: SMS
- 90Bytes 초과~2,000Bytes 이하: LMS
- 수신자 전체를 `Destination` JSON에 담아 API를 한 번만 호출
- 수신자별 이름·미수금·기한은 `Msg_Text`로 개별 전송
- 즉시발송: `Send_Date` 미입력
- 예약발송: `Send_Date=YYYY-MM-DD HH:MM`
- 발송 응답의 `Job_No`를 프로그램에 저장
- 발송결과 확인 시 `Report_Detail`로 통신사 성공·실패를 갱신
- 예약수정: `Reserve_Edit`
- 예약취소: `Cancel`

발송닷컴 접수 성공은 통신사 전송 성공과 다릅니다. API 접수 직후에는 `접수완료`, 결과조회 후에만 `전송성공` 또는 `실패`로 표시합니다.

## 5. 주의사항

- 수신자별 반복 API 호출을 하지 않습니다.
- 1초에 3회 이상 요청하지 않습니다.
- EUC-KR에서 지원되지 않는 문자가 있으면 발송 전에 차단합니다.
- LMS 2,000Bytes 초과 문구는 발송하지 않습니다.
- 같은 발송건은 `Job_No`가 저장된 뒤 다시 접수하지 않습니다.
