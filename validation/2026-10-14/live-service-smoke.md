# 실제 WSL 서비스 연동 점검 (2026-10-14)

환경: Ubuntu 24.04 WSL2, kernel `6.6.87.2-microsoft-standard-WSL2+`, Control Room 소스 `fbc8c97`, 설치 수집기 `MGlogWH 0.4.0` (`manage.py` SHA-256 `f035849f9898dae610ff2196cdc89d566155bd095db1803a2b34b46c981ce923`). 두 서비스를 설치·실행한 뒤 `sudo python3 scripts/live-integration-smoke.py --execute`를 실행했다.

로컬 토큰 로그인, legacy→`event_rules_v1` 저장, `pending_apply=true`, `/api/apply`를 통한 실제 `mglogwh.service` 재시작, 새 run ID·설정 SHA-256·`RUNNING`, 고유 테스트 경로의 규칙 ID 248 Critical 이벤트, `/api/files`의 실제 저장 경로를 확인했다. 시험 run은 `dce9981f-28de-4ad6-9d41-2788ae9a4a2e`, 적용 설정 해시는 `27d5f209571034ed1afc1e24004d89d749b23726d08502f343593ca7c105d914`이다. 원래 설정으로 재시작한 run은 `55800199-b1cc-43c6-a07d-dcc1c7b320d8`이며 설정 바이트 복원과 `RUNNING`을 확인했다. 스크립트 결과는 `PASS`였다.

종료된 시험 run의 raw와 인덱스는 312/312건, 테스트 경로는 양쪽에서 4/4건 동일 payload였고 `recover=RECOVERED`였다. 직접 대조 JSON과 재현 검증기는 [MGlogWH 수집기 저장소](https://github.com/habang2222/MGlogWH/tree/main/validation/2026-10-14)에 있다. 시험 후 두 서비스는 중지·비활성 상태로 두었으며 설치 전 설정과 현재 설정의 바이트 일치를 확인했다. 설치 전 코드·설정 백업은 WSL `/root/mglogwh-pre-v040-20261014/`에 남겨 두었다.

이 점검은 실제 HTTP API와 systemd·BPF·raw·인덱스를 거쳤다. 브라우저에서 로그인 후 모든 화면 요소를 조작하는 시험이나 독립 Ubuntu 서버 시험은 포함하지 않았다.
